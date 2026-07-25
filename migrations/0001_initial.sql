CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TABLE IF NOT EXISTS repositories (
    id uuid PRIMARY KEY,
    full_name text NOT NULL UNIQUE,
    data jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    CHECK (jsonb_typeof(data) = 'object')
);

CREATE TABLE IF NOT EXISTS queue_items (
    id uuid PRIMARY KEY,
    repository_id uuid NOT NULL REFERENCES repositories(id) ON DELETE CASCADE,
    data jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    CHECK (jsonb_typeof(data) = 'object')
);
CREATE INDEX IF NOT EXISTS queue_items_repository_created_idx
    ON queue_items (repository_id, created_at DESC);

CREATE TABLE IF NOT EXISTS builds (
    id uuid PRIMARY KEY,
    repository_id uuid NOT NULL REFERENCES repositories(id) ON DELETE CASCADE,
    data jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CHECK (jsonb_typeof(data) = 'object')
);
CREATE INDEX IF NOT EXISTS builds_repository_created_idx
    ON builds (repository_id, created_at DESC);

CREATE TABLE IF NOT EXISTS webhook_deliveries (
    delivery_id text PRIMARY KEY,
    data jsonb NOT NULL,
    received_at timestamptz NOT NULL DEFAULT now(),
    CHECK (jsonb_typeof(data) = 'object')
);

CREATE TABLE IF NOT EXISTS extension_runs (
    id uuid PRIMARY KEY,
    idempotency_key text NOT NULL UNIQUE,
    data jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    CHECK (jsonb_typeof(data) = 'object')
);

CREATE TABLE IF NOT EXISTS audit_records (
    id uuid PRIMARY KEY,
    actor text NOT NULL,
    action text NOT NULL,
    target text NOT NULL,
    data jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    CHECK (jsonb_typeof(data) = 'object')
);
CREATE INDEX IF NOT EXISTS audit_records_created_idx
    ON audit_records (created_at DESC);

REVOKE CREATE ON SCHEMA public FROM PUBLIC;
