-- A role bound to a model, by the tenant or by one of its users (slice 7c). The binding is kept
-- whole, as the contract has it (ModelBinding): owner_kind, owner_id and role are columns of their
-- own because binding the same role again, for the same owner, replaces it rather than being
-- refused (choosing a different model for a role is routine, not a conflict).

CREATE TABLE hub.model_bindings (
    id uuid PRIMARY KEY,
    tenant_id uuid NOT NULL REFERENCES hub.tenants (id) ON DELETE CASCADE,
    owner_kind text NOT NULL CHECK (owner_kind IN ('user', 'tenant')),
    owner_id uuid NOT NULL,
    role text NOT NULL,
    binding jsonb NOT NULL,
    created_at timestamptz NOT NULL,
    UNIQUE (tenant_id, owner_kind, owner_id, role)
);
CREATE INDEX model_bindings_tenant ON hub.model_bindings (tenant_id);
