-- Migration 013: God-Tier Features
-- Adds tables for model routing, daemon agents, time-travel branching,
-- UI artifacts, and proactive interrupts.

-- ── Feature 1: Model Routing Config ─────────────────────────────────
CREATE TABLE IF NOT EXISTS model_routing_config (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id UUID REFERENCES workspaces(id) ON DELETE CASCADE,
    step_type TEXT NOT NULL,           -- planning, coding, research, etc.
    provider TEXT NOT NULL DEFAULT 'cloud',
    model TEXT NOT NULL,
    temperature REAL DEFAULT 0.7,
    max_tokens INTEGER DEFAULT 4096,
    created_at TIMESTAMPTZ DEFAULT now(),
    UNIQUE (workspace_id, step_type)
);

-- ── Feature 3: Daemon Agents ────────────────────────────────────────
CREATE TABLE IF NOT EXISTS daemon_agents (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id UUID REFERENCES workspaces(id) ON DELETE CASCADE,
    user_id UUID REFERENCES users(id),
    name TEXT NOT NULL,
    description TEXT DEFAULT '',
    daemon_type TEXT NOT NULL DEFAULT 'custom',
    watch_target TEXT DEFAULT '',
    poll_interval_seconds INTEGER DEFAULT 300,
    sensitivity TEXT DEFAULT 'medium',
    state TEXT DEFAULT 'idle',
    created_at TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE IF NOT EXISTS daemon_observations (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    daemon_id UUID REFERENCES daemon_agents(id) ON DELETE CASCADE,
    source TEXT DEFAULT '',
    content TEXT DEFAULT '',
    is_anomaly BOOLEAN DEFAULT false,
    created_at TIMESTAMPTZ DEFAULT now()
);

-- ── Feature 4: Time-Travel Branching ────────────────────────────────
ALTER TABLE agent_checkpoints ADD COLUMN IF NOT EXISTS branch_id TEXT DEFAULT 'main';

CREATE TABLE IF NOT EXISTS task_branches (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    task_id UUID REFERENCES agent_tasks(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    parent_branch TEXT DEFAULT 'main',
    fork_checkpoint_id TEXT DEFAULT '',
    fork_step_index INTEGER DEFAULT 0,
    status TEXT DEFAULT 'active',
    strategy_hint TEXT DEFAULT '',
    outcome_summary TEXT DEFAULT '',
    total_tool_calls INTEGER DEFAULT 0,
    total_tokens INTEGER DEFAULT 0,
    created_at TIMESTAMPTZ DEFAULT now(),
    completed_at TIMESTAMPTZ
);

-- ── Feature 7: UI Artifacts ─────────────────────────────────────────
CREATE TABLE IF NOT EXISTS ui_artifacts (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id UUID REFERENCES workspaces(id) ON DELETE CASCADE,
    title TEXT NOT NULL,
    description TEXT DEFAULT '',
    component_type TEXT DEFAULT 'react',
    component_code TEXT DEFAULT '',
    props_schema JSONB DEFAULT '{}',
    data_source TEXT DEFAULT '',
    version INTEGER DEFAULT 1,
    created_at TIMESTAMPTZ DEFAULT now(),
    updated_at TIMESTAMPTZ DEFAULT now(),
    created_by TEXT DEFAULT 'agent'
);

-- ── Feature 6: Proactive Interrupts ─────────────────────────────────
CREATE TABLE IF NOT EXISTS proactive_interrupts (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id UUID,
    user_id UUID,
    signal_source TEXT DEFAULT '',
    title TEXT DEFAULT '',
    body TEXT DEFAULT '',
    severity TEXT DEFAULT 'info',
    hypothesis TEXT DEFAULT '',
    recommendation TEXT DEFAULT '',
    channel TEXT DEFAULT 'notification',
    delivered BOOLEAN DEFAULT false,
    created_at TIMESTAMPTZ DEFAULT now()
);
