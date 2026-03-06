-- 019_task_queue_and_audit.sql — Persistent task queue + audit trail
-- Enables crash-safe task resumption and queryable execution history

-- ── Update agent_tasks status check to include new states ────────────
ALTER TABLE agent_tasks DROP CONSTRAINT IF EXISTS agent_tasks_status_check;
ALTER TABLE agent_tasks ADD CONSTRAINT agent_tasks_status_check
    CHECK (status IN (
        'planning', 'executing', 'observing', 'reflecting', 'verifying',
        'waiting_approval', 'paused', 'complete', 'failed', 'cancelled'
    ));

-- ── Persistent Task Queue ────────────────────────────────────────────
-- Tasks that should survive service restarts and be resumed automatically.
-- Differs from agent_tasks in that these are explicitly queued for
-- background/scheduled execution rather than interactive chat tasks.

CREATE TABLE IF NOT EXISTS task_queue (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id UUID NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    goal TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'queued'
        CHECK (status IN ('queued', 'running', 'paused', 'complete', 'failed', 'cancelled')),
    priority INTEGER NOT NULL DEFAULT 5
        CHECK (priority BETWEEN 1 AND 10),
    source TEXT NOT NULL DEFAULT 'user'
        CHECK (source IN ('user', 'daemon', 'automation', 'heartbeat', 'self_improve')),
    agent_task_id UUID REFERENCES agent_tasks(id) ON DELETE SET NULL,
    checkpoint_json JSONB,             -- Serialized checkpoint for resumption
    scheduled_at TIMESTAMPTZ,          -- NULL = run immediately, else wait
    started_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ,
    error TEXT,
    retry_count INTEGER DEFAULT 0,
    max_retries INTEGER DEFAULT 3,
    created_at TIMESTAMPTZ DEFAULT now(),
    updated_at TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX idx_task_queue_workspace ON task_queue(workspace_id);
CREATE INDEX idx_task_queue_status ON task_queue(status)
    WHERE status IN ('queued', 'running', 'paused');
CREATE INDEX idx_task_queue_scheduled ON task_queue(scheduled_at)
    WHERE status = 'queued' AND scheduled_at IS NOT NULL;
CREATE INDEX idx_task_queue_priority ON task_queue(priority DESC, created_at ASC)
    WHERE status = 'queued';

-- ── Audit Trail ──────────────────────────────────────────────────────
-- Queryable history of every significant agent action.

CREATE TABLE IF NOT EXISTS audit_events (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id UUID NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    user_id UUID REFERENCES users(id) ON DELETE SET NULL,
    task_id UUID REFERENCES agent_tasks(id) ON DELETE SET NULL,
    event_type TEXT NOT NULL
        CHECK (event_type IN (
            'tool_executed', 'tool_cached', 'tool_blocked',
            'approval_requested', 'approval_granted', 'approval_denied',
            'model_routed', 'model_escalated',
            'verification_passed', 'verification_failed',
            'daemon_observation', 'daemon_interrupt',
            'state_transition', 'task_queued', 'task_resumed',
            'mcp_installed', 'mcp_removed',
            'self_improve_proposed', 'self_improve_applied', 'self_improve_rolled_back',
            'macro_created', 'macro_executed',
            'error', 'warning'
        )),
    tool_name TEXT,
    details JSONB NOT NULL DEFAULT '{}'::jsonb,
    cost_usd NUMERIC(10, 6),          -- LLM cost for this action
    duration_ms INTEGER,               -- Execution time
    created_at TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX idx_audit_workspace ON audit_events(workspace_id, created_at DESC);
CREATE INDEX idx_audit_task ON audit_events(task_id)
    WHERE task_id IS NOT NULL;
CREATE INDEX idx_audit_type ON audit_events(event_type, created_at DESC);
CREATE INDEX idx_audit_tool ON audit_events(tool_name)
    WHERE tool_name IS NOT NULL;

-- ── RLS Policies ────────────────────────────────────────────────────
ALTER TABLE task_queue ENABLE ROW LEVEL SECURITY;
ALTER TABLE audit_events ENABLE ROW LEVEL SECURITY;

CREATE POLICY task_queue_workspace ON task_queue
    USING (workspace_id IN (
        SELECT workspace_id FROM workspace_members
        WHERE user_id = current_setting('app.user_id')::uuid
    ));

CREATE POLICY audit_events_workspace ON audit_events
    USING (workspace_id IN (
        SELECT workspace_id FROM workspace_members
        WHERE user_id = current_setting('app.user_id')::uuid
    ));

-- ── Auto-update timestamps ──────────────────────────────────────────
CREATE OR REPLACE FUNCTION update_task_queue_timestamp()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trigger_task_queue_updated
    BEFORE UPDATE ON task_queue
    FOR EACH ROW
    EXECUTE FUNCTION update_task_queue_timestamp();
