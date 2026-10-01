-- The hub: tenants and their users (slice 7a). A device's pairing, a tenant's spaces, its linked
-- accounts, its model bindings and the secrets of its skills are later migrations of this service.

CREATE TABLE hub.tenants (
    id uuid PRIMARY KEY,
    created_at timestamptz NOT NULL
);

CREATE TABLE hub.users (
    id uuid PRIMARY KEY,
    tenant_id uuid NOT NULL REFERENCES hub.tenants (id) ON DELETE CASCADE,
    username text NOT NULL,
    password_hash text NOT NULL,
    roles text[] NOT NULL,
    must_change_password boolean NOT NULL,
    created_at timestamptz NOT NULL,
    UNIQUE (tenant_id, username)
);
