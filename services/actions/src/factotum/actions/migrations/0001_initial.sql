-- The actions service: skills, in versions that never change; what they say about themselves, to
-- find the ones a message may be asking for; the confirmations that wait for a yes or a no; and the
-- audit trail (ADR 0014).
--
-- Every table carries the tenant it belongs to, and every query of the service filters by it.
-- Row-level security (for a deployment shared by tenants) is a later migration on these columns.
-- A skill belongs to an *owner*, the subject that installed it: a user, or the tenant.

CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE actions.skills (
    id uuid PRIMARY KEY,
    tenant_id uuid NOT NULL,
    owner_kind text NOT NULL CHECK (owner_kind IN ('user', 'tenant')),
    owner_id uuid NOT NULL,
    name text NOT NULL,
    created_at timestamptz NOT NULL,
    -- The version that runs: one that somebody reviewed and enabled. Nothing runs while it is null.
    enabled_version integer,
    UNIQUE (tenant_id, owner_kind, owner_id, name)
);
CREATE INDEX skills_owner ON actions.skills (tenant_id, owner_kind, owner_id);

-- A version is never changed: a skill that changes is a new version, and one that is read again as
-- it was is the same one (the hash is of the SKILL.md and of every file beside it).
CREATE TABLE actions.skill_versions (
    skill_id uuid NOT NULL REFERENCES actions.skills (id) ON DELETE CASCADE,
    version integer NOT NULL,
    tenant_id uuid NOT NULL,
    content_sha256 text NOT NULL,
    created_at timestamptz NOT NULL,
    manifest jsonb NOT NULL,
    instructions text NOT NULL,
    -- What was granted when the version was enabled, and by whom: the most it may reach.
    granted jsonb,
    enabled_at timestamptz,
    enabled_by uuid,
    PRIMARY KEY (skill_id, version),
    UNIQUE (skill_id, content_sha256)
);

-- The files beside the SKILL.md, so that a version is whole and the runner gets what was reviewed.
CREATE TABLE actions.skill_files (
    skill_id uuid NOT NULL,
    version integer NOT NULL,
    path text NOT NULL,
    content bytea NOT NULL,
    PRIMARY KEY (skill_id, version, path),
    FOREIGN KEY (skill_id, version) REFERENCES actions.skill_versions (skill_id, version) ON DELETE CASCADE
);

-- What a skill says about itself (its description and each example), by meaning. No size in the
-- column: it is the one of the owner's embedding model, and the space says which model that was.
CREATE TABLE actions.skill_vectors (
    skill_id uuid NOT NULL,
    version integer NOT NULL,
    tenant_id uuid NOT NULL,
    space_id text NOT NULL,
    seq integer NOT NULL,
    text text NOT NULL,
    embedding vector NOT NULL,
    PRIMARY KEY (skill_id, version, seq),
    FOREIGN KEY (skill_id, version) REFERENCES actions.skill_versions (skill_id, version) ON DELETE CASCADE
);

-- A yes or a no that is waited for: one at a time for a person on a device.
CREATE TABLE actions.confirmations (
    id uuid PRIMARY KEY,
    tenant_id uuid NOT NULL,
    subject_kind text NOT NULL,
    subject_id uuid NOT NULL,
    device_id uuid,
    proposal jsonb NOT NULL,
    created_at timestamptz NOT NULL,
    expires_at timestamptz NOT NULL
);
CREATE UNIQUE INDEX confirmations_one_at_a_time ON actions.confirmations (
    tenant_id, subject_kind, subject_id, coalesce(device_id, '00000000-0000-0000-0000-000000000000'::uuid)
);

-- Who did what, through which device, on whose behalf. Only ever added to; it has no speaker
-- (ADR 0004), and its parameters were redacted before they came here. What is not in a column is in
-- the record, which is the contract as it was written.
CREATE TABLE actions.audit (
    id uuid PRIMARY KEY,
    tenant_id uuid NOT NULL,
    occurred_at timestamptz NOT NULL,
    skill_name text NOT NULL,
    device_id uuid,
    record jsonb NOT NULL
);
CREATE INDEX audit_tenant_time ON actions.audit (tenant_id, occurred_at DESC);
