-- The conversation service: what was said, saved so that it can be looked up when a message refers
-- to it (ADR 0012).
--
-- Every table carries the tenant it belongs to, and every query of the service filters by it.
-- Row-level security (for a deployment shared by tenants) is a later migration on these columns.
-- What was said belongs to an *owner*, the subject a turn acted for: a user, or the tenant for a
-- shared device.

CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS unaccent;

-- The words of what was said, as they are, in lower case and with the accents ignored: the log is in
-- whatever language its owner speaks, and it may be in several.
CREATE TEXT SEARCH CONFIGURATION conversation.words (COPY = pg_catalog.simple);
ALTER TEXT SEARCH CONFIGURATION conversation.words
    ALTER MAPPING FOR hword, hword_part, word WITH public.unaccent, pg_catalog.simple;

CREATE TABLE conversation.conversations (
    id uuid PRIMARY KEY,
    tenant_id uuid NOT NULL,
    owner_kind text NOT NULL CHECK (owner_kind IN ('user', 'tenant')),
    owner_id uuid NOT NULL,
    device_id uuid,
    started_at timestamptz NOT NULL,
    last_at timestamptz NOT NULL
);
CREATE INDEX conversations_owner ON conversation.conversations (tenant_id, owner_kind, owner_id, last_at DESC);
CREATE INDEX conversations_last ON conversation.conversations (last_at);

CREATE TABLE conversation.exchanges (
    id uuid PRIMARY KEY,
    tenant_id uuid NOT NULL,
    conversation_id uuid NOT NULL REFERENCES conversation.conversations (id) ON DELETE CASCADE,
    owner_kind text NOT NULL,
    owner_id uuid NOT NULL,
    seq integer NOT NULL,
    created_at timestamptz NOT NULL,
    question text NOT NULL,
    answer text NOT NULL,
    -- The passages the answer cited, without their text: where they came from.
    citations jsonb NOT NULL DEFAULT '[]',
    -- Its turn had somebody else's words in it (a passage of a document), so its answer may carry them.
    untrusted boolean NOT NULL DEFAULT false,
    tsv tsvector NOT NULL,
    -- Made after the turn, in the background. No size in the column: it is the one of the model,
    -- and the space says which model that was.
    embedding vector,
    space_id text,
    -- Left for later: the owner has no embedding model, or it did not answer.
    embed_after timestamptz,
    UNIQUE (conversation_id, seq)
);
CREATE INDEX exchanges_owner_time ON conversation.exchanges (tenant_id, owner_kind, owner_id, created_at DESC);
CREATE INDEX exchanges_tsv ON conversation.exchanges USING gin (tsv);
CREATE INDEX exchanges_pending ON conversation.exchanges (created_at) WHERE embedding IS NULL;
