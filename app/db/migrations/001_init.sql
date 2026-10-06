CREATE TABLE IF NOT EXISTS api_keys (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    key_hash      text NOT NULL UNIQUE,          -- sha256 hex of the raw key; raw key is never stored
    tenant_id     text NOT NULL,
    user_id       text NOT NULL,
    is_admin      boolean NOT NULL DEFAULT false,
    rate_per_min  integer NOT NULL DEFAULT 60,
    rate_per_day  integer NOT NULL DEFAULT 10000,
    label         text,
    created_at    timestamptz NOT NULL DEFAULT now(),
    revoked_at    timestamptz
);
CREATE INDEX IF NOT EXISTS api_keys_tenant_idx ON api_keys (tenant_id);

-- last-activity bookkeeping for conversation TTL cleanup (checkpointer tables have no timestamps)
CREATE TABLE IF NOT EXISTS thread_activity (
    thread_id    text PRIMARY KEY,
    tenant_id    text NOT NULL,
    last_active  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS thread_activity_last_idx ON thread_activity (last_active);
