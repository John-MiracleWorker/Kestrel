-- 020_autonomy_kernel.sql — Workspace agents, queue leases, and opportunities

CREATE TABLE IF NOT EXISTS workspace_agents (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id UUID NOT NULL UNIQUE REFERENCES workspaces(id) ON DELETE CASCADE,
    default_mode TEXT NOT NULL DEFAULT 'ops'
        CHECK (default_mode IN ('core', 'ops', 'labs')),
    autonomy_policy TEXT NOT NULL DEFAULT 'moderate'
        CHECK (autonomy_policy IN ('conservative', 'moderate', 'full')),
    memory_namespace TEXT NOT NULL,
    tool_policy_bundle JSONB NOT NULL DEFAULT '[]'::jsonb,
    persona_version INTEGER NOT NULL DEFAULT 1,
    runtime_defaults JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ DEFAULT now(),
    updated_at TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_workspace_agents_mode
    ON workspace_agents(default_mode);

ALTER TABLE task_queue
    ADD COLUMN IF NOT EXISTS agent_profile_id UUID REFERENCES workspace_agents(id) ON DELETE SET NULL,
    ADD COLUMN IF NOT EXISTS payload_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    ADD COLUMN IF NOT EXISTS dedupe_key TEXT NOT NULL DEFAULT '',
    ADD COLUMN IF NOT EXISTS lease_owner TEXT NOT NULL DEFAULT '',
    ADD COLUMN IF NOT EXISTS lease_expires_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS parent_queue_id UUID REFERENCES task_queue(id) ON DELETE SET NULL,
    ADD COLUMN IF NOT EXISTS trigger_kind TEXT NOT NULL DEFAULT 'task',
    ADD COLUMN IF NOT EXISTS terminal_task_id UUID REFERENCES agent_tasks(id) ON DELETE SET NULL;

CREATE INDEX IF NOT EXISTS idx_task_queue_agent_profile
    ON task_queue(agent_profile_id, status);
CREATE INDEX IF NOT EXISTS idx_task_queue_lease
    ON task_queue(status, lease_expires_at);
CREATE INDEX IF NOT EXISTS idx_task_queue_dedupe
    ON task_queue(workspace_id, dedupe_key);

ALTER TABLE agent_sessions
    ADD COLUMN IF NOT EXISTS agent_profile_id UUID REFERENCES workspace_agents(id) ON DELETE SET NULL,
    ADD COLUMN IF NOT EXISTS channel TEXT NOT NULL DEFAULT 'task',
    ADD COLUMN IF NOT EXISTS prunable_after TIMESTAMPTZ;

CREATE INDEX IF NOT EXISTS idx_agent_sessions_prunable
    ON agent_sessions(prunable_after)
    WHERE prunable_after IS NOT NULL;

CREATE TABLE IF NOT EXISTS agent_opportunities (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    agent_profile_id UUID NOT NULL REFERENCES workspace_agents(id) ON DELETE CASCADE,
    source TEXT NOT NULL DEFAULT '',
    title TEXT NOT NULL DEFAULT '',
    goal_template TEXT NOT NULL DEFAULT '',
    score DOUBLE PRECISION NOT NULL DEFAULT 0,
    severity TEXT NOT NULL DEFAULT 'info'
        CHECK (severity IN ('info', 'warning', 'critical')),
    dedupe_key TEXT NOT NULL DEFAULT '',
    expires_at TIMESTAMPTZ,
    state TEXT NOT NULL DEFAULT 'pending'
        CHECK (state IN ('pending', 'queued', 'dismissed', 'expired', 'completed')),
    payload_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ DEFAULT now(),
    updated_at TIMESTAMPTZ DEFAULT now(),
    UNIQUE (agent_profile_id, dedupe_key)
);

CREATE INDEX IF NOT EXISTS idx_agent_opportunities_agent_state
    ON agent_opportunities(agent_profile_id, state, score DESC, created_at ASC);
