CREATE TABLE IF NOT EXISTS planner_jobs (
    id uuid PRIMARY KEY,
    kind text NOT NULL CHECK (kind IN ('plan', 'import')),
    idempotency_key text NOT NULL,
    request_digest text NOT NULL CHECK (length(request_digest) = 64),
    payload jsonb NOT NULL,
    status text NOT NULL DEFAULT 'queued' CHECK (status IN ('queued', 'running', 'succeeded', 'failed')),
    attempts integer NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    result jsonb,
    error text,
    claim_token uuid,
    lease_until timestamptz,
    ready_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    dispatch_after timestamptz NOT NULL DEFAULT clock_timestamp(),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (kind, idempotency_key),
    CHECK ((status = 'running') = (claim_token IS NOT NULL AND lease_until IS NOT NULL)),
    CHECK ((status = 'succeeded') = (result IS NOT NULL))
);
CREATE INDEX IF NOT EXISTS planner_jobs_dispatch_idx ON planner_jobs (dispatch_after, ready_at)
    WHERE status IN ('queued', 'running');
