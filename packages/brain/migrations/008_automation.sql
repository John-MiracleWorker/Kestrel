-- Migration 008: Cron Jobs and Webhooks for automation
-- Supports the CronScheduler and WebhookHandler

-- ── Cron Jobs ───────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS automation_cron_jobs (
    id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id     UUID NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    user_id          UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name             TEXT NOT NULL,
    description      TEXT DEFAULT '',
    cron_expression  TEXT NOT NULL,    -- Standard 5-field cron
    goal             TEXT NOT NULL,    -- Agent task goal
    status           TEXT NOT NULL DEFAULT 'active',  -- active, paused, disabled
    last_run         TIMESTAMPTZ,
    run_count        INTEGER DEFAULT 0,
    max_runs         INTEGER,          -- NULL = unlimited
    created_at       TIMESTAMPTZ DEFAULT now(),
    updated_at       TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_cron_workspace
    ON automation_cron_jobs(workspace_id, status);

-- ── Webhooks ────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS automation_webhooks (
    id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id     UUID NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    user_id          UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name             TEXT NOT NULL,
    description      TEXT DEFAULT '',
    goal_template    TEXT NOT NULL,    -- Template with {payload}/{headers} placeholders
    secret           TEXT DEFAULT '',  -- HMAC secret for signature verification
    status           TEXT NOT NULL DEFAULT 'active',
    trigger_count    INTEGER DEFAULT 0,
    allowed_sources  JSONB DEFAULT '[]',  -- IP allowlist
    created_at       TIMESTAMPTZ DEFAULT now(),
    updated_at       TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_webhooks_workspace
    ON automation_webhooks(workspace_id, status);

-- ── Webhook Execution Log ───────────────────────────────────────────

CREATE TABLE IF NOT EXISTS automation_webhook_log (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    webhook_id   UUID NOT NULL REFERENCES automation_webhooks(id) ON DELETE CASCADE,
    task_id      UUID REFERENCES agent_tasks(id) ON DELETE SET NULL,
    source_ip    TEXT DEFAULT '',
    payload_size INTEGER DEFAULT 0,
    status       TEXT NOT NULL,  -- success, failed, rejected
    error        TEXT,
    created_at   TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_webhook_log_webhook
    ON automation_webhook_log(webhook_id, created_at DESC);

-- ── RLS ─────────────────────────────────────────────────────────────

ALTER TABLE automation_cron_jobs ENABLE ROW LEVEL SECURITY;
ALTER TABLE automation_webhooks ENABLE ROW LEVEL SECURITY;
ALTER TABLE automation_webhook_log ENABLE ROW LEVEL SECURITY;

CREATE POLICY cron_workspace_isolation ON automation_cron_jobs
    USING (workspace_id = current_setting('app.workspace_id', true)::uuid);

CREATE POLICY webhooks_workspace_isolation ON automation_webhooks
    USING (workspace_id = current_setting('app.workspace_id', true)::uuid);

CREATE POLICY webhook_log_access ON automation_webhook_log
    USING (webhook_id IN (
        SELECT id FROM automation_webhooks
        WHERE workspace_id = current_setting('app.workspace_id', true)::uuid
    ));

-- ── Auto-update timestamps ──────────────────────────────────────────

CREATE OR REPLACE FUNCTION update_automation_timestamp()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_cron_updated
    BEFORE UPDATE ON automation_cron_jobs
    FOR EACH ROW EXECUTE FUNCTION update_automation_timestamp();

CREATE TRIGGER trg_webhooks_updated
    BEFORE UPDATE ON automation_webhooks
    FOR EACH ROW EXECUTE FUNCTION update_automation_timestamp();
