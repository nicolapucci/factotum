-- The knowledge service: collections of documents, read into pieces that are searched.
--
-- Every table carries the tenant it belongs to, and every query of the service filters by it.
-- Row-level security (for a deployment shared by tenants) is a later migration on these columns.

CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS unaccent;

-- Text search configurations. Postgres ships one for each language; these are the same with the
-- accents ignored where the language has them, so that "citta" finds "città": speech-to-text and
-- keyboards drop accents as often as they keep them. Languages whose letters would change by it
-- (Turkish has a dotless i) or that have no such marks to fold are copied as they are.
DO $$
DECLARE
    as_they_are text[] := ARRAY['arabic', 'greek', 'hindi', 'nepali', 'russian', 'tamil', 'turkish'];
    folded text[] := ARRAY[
        'catalan', 'danish', 'dutch', 'english', 'finnish', 'french', 'german', 'hungarian',
        'indonesian', 'italian', 'norwegian', 'portuguese', 'romanian', 'spanish', 'swedish'
    ];
    language text;
BEGIN
    FOREACH language IN ARRAY as_they_are LOOP
        EXECUTE format(
            'CREATE TEXT SEARCH CONFIGURATION knowledge.%I (COPY = pg_catalog.%I)', language, language
        );
    END LOOP;
    FOREACH language IN ARRAY folded LOOP
        EXECUTE format(
            'CREATE TEXT SEARCH CONFIGURATION knowledge.%I (COPY = pg_catalog.%I)', language, language
        );
        EXECUTE format(
            'ALTER TEXT SEARCH CONFIGURATION knowledge.%I '
            'ALTER MAPPING FOR hword, hword_part, word WITH public.unaccent, pg_catalog.%I',
            language, language || '_stem'
        );
    END LOOP;
END
$$;

-- For a document in no language the service knows: words as they are, in lower case.
CREATE TEXT SEARCH CONFIGURATION knowledge.simple (COPY = pg_catalog.simple);
ALTER TEXT SEARCH CONFIGURATION knowledge.simple
    ALTER MAPPING FOR hword, hword_part, word WITH public.unaccent, pg_catalog.simple;


CREATE TABLE knowledge.collections (
    id uuid PRIMARY KEY,
    tenant_id uuid NOT NULL,
    owner_kind text NOT NULL CHECK (owner_kind IN ('user', 'tenant', 'device')),
    owner_id uuid NOT NULL,
    name text NOT NULL,
    description text NOT NULL DEFAULT '',
    -- The embedding space its vectors were made in: an opaque id and the size of the vectors.
    space_id text NOT NULL,
    dimensions integer NOT NULL CHECK (dimensions BETWEEN 1 AND 16000),
    status text NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'needs_reindex', 'reindexing')),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

-- One owner does not keep two collections of the same name.
CREATE UNIQUE INDEX collections_owner_name
    ON knowledge.collections (tenant_id, owner_kind, owner_id, lower(name));
CREATE INDEX collections_tenant ON knowledge.collections (tenant_id, created_at);


CREATE TABLE knowledge.documents (
    id uuid PRIMARY KEY,
    seq bigint GENERATED ALWAYS AS IDENTITY,
    tenant_id uuid NOT NULL,
    collection_id uuid NOT NULL REFERENCES knowledge.collections (id) ON DELETE CASCADE,
    title text NOT NULL,
    source text,
    media_type text NOT NULL,
    size_bytes bigint NOT NULL CHECK (size_bytes >= 0),
    content_sha256 text NOT NULL,
    language text,
    language_declared boolean NOT NULL DEFAULT false,
    status text NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'processing', 'ready', 'failed')),
    error_code text,
    error_message text,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (collection_id, content_sha256)
);

CREATE INDEX documents_collection ON knowledge.documents (collection_id, seq DESC);

-- The file as it was uploaded, apart from the row it belongs to so that reading a list of
-- documents never drags it along.
CREATE TABLE knowledge.document_data (
    document_id uuid PRIMARY KEY REFERENCES knowledge.documents (id) ON DELETE CASCADE,
    tenant_id uuid NOT NULL,
    data bytea NOT NULL
);


-- The section around a piece: what comes back with it, for a reader that needs more than the match.
CREATE TABLE knowledge.parents (
    id uuid PRIMARY KEY,
    tenant_id uuid NOT NULL,
    document_id uuid NOT NULL REFERENCES knowledge.documents (id) ON DELETE CASCADE,
    ordinal integer NOT NULL,
    text text NOT NULL,
    headings jsonb NOT NULL DEFAULT '[]',
    page integer
);

CREATE INDEX parents_document ON knowledge.parents (document_id);


-- What is searched. ``embedding`` has no size of its own because every collection has the size of
-- its embedding model: the index that makes a large collection fast is made for that size, one
-- collection at a time (see the store).
CREATE TABLE knowledge.chunks (
    id uuid PRIMARY KEY,
    tenant_id uuid NOT NULL,
    collection_id uuid NOT NULL,
    document_id uuid NOT NULL REFERENCES knowledge.documents (id) ON DELETE CASCADE,
    parent_id uuid NOT NULL REFERENCES knowledge.parents (id) ON DELETE CASCADE,
    ordinal integer NOT NULL,
    text text NOT NULL,
    headings jsonb NOT NULL DEFAULT '[]',
    page integer,
    -- The text search configuration the words were read with: the language of the document.
    fts_config text NOT NULL,
    tsv tsvector NOT NULL,
    embedding vector,
    UNIQUE (document_id, ordinal)
);

CREATE INDEX chunks_collection ON knowledge.chunks (collection_id);
CREATE INDEX chunks_parent ON knowledge.chunks (parent_id);
CREATE INDEX chunks_tsv ON knowledge.chunks USING gin (tsv);
-- What still has to be embedded, found without reading the collection.
CREATE INDEX chunks_pending ON knowledge.chunks (collection_id) WHERE embedding IS NULL;


-- Work to do. A worker claims a job for a while (its lease); one that stops answering loses it.
CREATE TABLE knowledge.jobs (
    id uuid PRIMARY KEY,
    seq bigint GENERATED ALWAYS AS IDENTITY,
    tenant_id uuid NOT NULL,
    kind text NOT NULL CHECK (kind IN ('ingest', 'reindex')),
    collection_id uuid NOT NULL REFERENCES knowledge.collections (id) ON DELETE CASCADE,
    document_id uuid REFERENCES knowledge.documents (id) ON DELETE CASCADE,
    state text NOT NULL DEFAULT 'queued' CHECK (state IN ('queued', 'running', 'failed')),
    attempts integer NOT NULL DEFAULT 0,
    due_at timestamptz NOT NULL DEFAULT now(),
    lease_until timestamptz,
    locked_by text,
    last_error text,
    CHECK ((kind = 'ingest') = (document_id IS NOT NULL))
);

CREATE INDEX jobs_due ON knowledge.jobs (due_at, seq) WHERE state <> 'failed';
-- A document is read by one job at a time.
CREATE UNIQUE INDEX jobs_one_ingest ON knowledge.jobs (document_id)
    WHERE kind = 'ingest' AND state <> 'failed';
-- A collection is re-embedded by one job at a time.
CREATE UNIQUE INDEX jobs_one_reindex ON knowledge.jobs (collection_id)
    WHERE kind = 'reindex' AND state <> 'failed';
