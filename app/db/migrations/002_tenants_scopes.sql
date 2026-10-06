CREATE TABLE IF NOT EXISTS tenants (
    tenant_id           text PRIMARY KEY,
    domain_description  text,                    -- NULL => global default (ASSISTANT_DOMAIN)
    updated_at          timestamptz NOT NULL DEFAULT now()
);

ALTER TABLE api_keys ADD COLUMN IF NOT EXISTS scopes text[] NOT NULL DEFAULT '{}';  -- e.g. {eval}
