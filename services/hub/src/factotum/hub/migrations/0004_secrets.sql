-- The secrets a skill is given, by whose they are (slice 7d). Unlike a device's credential or a
-- model's key, there is no hash or reference to keep instead: a skill's secret (a router's
-- password, a long-lived token) is read back in the clear, by the actions service, to run it.
-- Setting the same name again, for the same owner, replaces it rather than being refused: rotating
-- a secret is routine, the same choice already made for a model binding.

CREATE TABLE hub.secrets (
    tenant_id uuid NOT NULL REFERENCES hub.tenants (id) ON DELETE CASCADE,
    owner_kind text NOT NULL CHECK (owner_kind IN ('user', 'tenant')),
    owner_id uuid NOT NULL,
    name text NOT NULL,
    value text NOT NULL,
    created_at timestamptz NOT NULL,
    PRIMARY KEY (tenant_id, owner_kind, owner_id, name)
);
