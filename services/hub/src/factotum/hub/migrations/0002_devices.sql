-- Devices (system or external) and the bindings of a skill to one (slice 7b). The record is kept
-- whole, as the contract has it (SystemDeviceRecord or ExternalDeviceRecord): kind and name are
-- columns of their own only because a device is looked for and is unique by them.

CREATE TABLE hub.devices (
    id uuid PRIMARY KEY,
    tenant_id uuid NOT NULL REFERENCES hub.tenants (id) ON DELETE CASCADE,
    kind text NOT NULL CHECK (kind IN ('system', 'external')),
    name text NOT NULL,
    -- Only a system device has one; two NULLs (two external devices) are never equal, so this does
    -- not stop an unlimited number of them.
    hardware_id text,
    record jsonb NOT NULL,
    -- A system device's credential, hashed (sha256: the token is already random and long, so a
    -- slow password hash buys nothing and would cost every device every time it connects). An
    -- external device has none.
    credential_hash text,
    created_at timestamptz NOT NULL,
    UNIQUE (tenant_id, name),
    UNIQUE (tenant_id, hardware_id)
);
CREATE INDEX devices_tenant ON hub.devices (tenant_id);

-- Which skill (by name: a binding is made before, or without, that skill being installed in the
-- actions service, which resolves the name) drives which device, and with what parameters of the
-- device's own.
CREATE TABLE hub.bindings (
    id uuid PRIMARY KEY,
    tenant_id uuid NOT NULL REFERENCES hub.tenants (id) ON DELETE CASCADE,
    skill text NOT NULL,
    device_id uuid NOT NULL REFERENCES hub.devices (id) ON DELETE CASCADE,
    params jsonb NOT NULL,
    enabled boolean NOT NULL,
    created_at timestamptz NOT NULL,
    UNIQUE (tenant_id, skill, device_id)
);
CREATE INDEX bindings_tenant ON hub.bindings (tenant_id);
