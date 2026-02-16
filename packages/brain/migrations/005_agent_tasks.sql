-- 005_agent_tasks.sql — Agent task execution tables
-- Stores agent tasks, steps, and approval requests for the autonomous runtime

-- ── Agent Tasks ────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS agent_tasks (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    workspace_id UUID NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    conversation_id UUID REFERENCES conversations(id) ON DELETE SET NULL,
    goal TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'planning'
        CHECK (status IN (
            'planning', 'executing', 'observing', 'reflecting',
            'waiting_approval', 'paused', 'complete', 'failed', 'cancelled'
        )),
    plan JSONB,                             -- Serialized TaskPlan
    config JSONB NOT NULL DEFAULT '{}'::jsonb,  -- GuardrailConfig
    result TEXT,
    error TEXT,
    token_usage INTEGER DEFAULT 0,
    tool_calls_count INTEGER DEFAULT 0,
    iterations INTEGER DEFAULT 0,
    created_at TIMESTAMPTZ DEFAULT now(),
    updated_at TIMESTAMPTZ DEFAULT now(),
    completed_at TIMESTAMPTZ
);

CREATE INDEX idx_agent_tasks_user ON agent_tasks(user_id);
CREATE INDEX idx_agent_tasks_workspace ON agent_tasks(workspace_id);
CREATE INDEX idx_agent_tasks_status ON agent_tasks(status)
    WHERE status NOT IN ('complete', 'failed', 'cancelled');

-- ── Agent Approval Requests ────────────────────────────────────────

CREATE TABLE IF NOT EXISTS agent_approvals (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    task_id UUID NOT NULL REFERENCES agent_tasks(id) ON DELETE CASCADE,
    step_id TEXT,                            -- TaskStep.id within the plan
    tool_name TEXT NOT NULL,
    tool_args JSONB NOT NULL DEFAULT '{}'::jsonb,
    risk_level TEXT NOT NULL DEFAULT 'high'
        CHECK (risk_level IN ('low', 'medium', 'high', 'critical')),
    reason TEXT,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'approved', 'denied', 'expired')),
    decided_by UUID REFERENCES users(id),
    decided_at TIMESTAMPTZ,
    expires_at TIMESTAMPTZ DEFAULT (now() + INTERVAL '30 minutes'),
    created_at TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX idx_agent_approvals_task ON agent_approvals(task_id);
CREATE INDEX idx_agent_approvals_pending ON agent_approvals(status)
    WHERE status = 'pending';

-- ── RLS Policies ───────────────────────────────────────────────────

ALTER TABLE agent_tasks ENABLE ROW LEVEL SECURITY;
ALTER TABLE agent_approvals ENABLE ROW LEVEL SECURITY;

-- Users can see their own tasks
CREATE POLICY agent_tasks_owner ON agent_tasks
    USING (user_id = current_setting('app.user_id')::uuid);

-- Users can see approvals for their tasks
CREATE POLICY agent_approvals_owner ON agent_approvals
    USING (task_id IN (
        SELECT id FROM agent_tasks
        WHERE user_id = current_setting('app.user_id')::uuid
    ));

-- Workspace members can see tasks in their workspace
CREATE POLICY agent_tasks_workspace ON agent_tasks
    USING (workspace_id IN (
        SELECT workspace_id FROM workspace_members
        WHERE user_id = current_setting('app.user_id')::uuid
    ));

-- ── Auto-update updated_at ─────────────────────────────────────────

CREATE OR REPLACE FUNCTION update_agent_task_timestamp()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trigger_agent_task_updated
    BEFORE UPDATE ON agent_tasks
    FOR EACH ROW
    EXECUTE FUNCTION update_agent_task_timestamp();
