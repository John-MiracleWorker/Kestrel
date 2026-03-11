-- 021_adaptive_kernel_dynamic_caps.sql
-- Soft presets, global skills, adaptive recipes, and kernel memory.

ALTER TABLE workspace_agents
    ADD COLUMN IF NOT EXISTS kernel_preset TEXT NOT NULL DEFAULT 'ops'
        CHECK (kernel_preset IN ('core', 'ops', 'labs')),
    ADD COLUMN IF NOT EXISTS kernel_policy_json JSONB NOT NULL DEFAULT '{}'::jsonb;

UPDATE workspace_agents
SET kernel_preset = default_mode
WHERE kernel_preset IS NULL OR kernel_preset = '';

ALTER TABLE agent_skills
    ALTER COLUMN workspace_id DROP NOT NULL;

ALTER TABLE agent_skills
    ADD COLUMN IF NOT EXISTS scope TEXT NOT NULL DEFAULT 'workspace'
        CHECK (scope IN ('global', 'workspace', 'session')),
    ADD COLUMN IF NOT EXISTS state TEXT NOT NULL DEFAULT 'approved'
        CHECK (state IN ('draft', 'approved', 'disabled', 'rejected'));

UPDATE agent_skills
SET scope = CASE
        WHEN workspace_id IS NULL THEN 'global'
        ELSE 'workspace'
    END,
    state = COALESCE(NULLIF(state, ''), 'approved');

ALTER TABLE agent_skills
    DROP CONSTRAINT IF EXISTS agent_skills_workspace_id_name_key;

CREATE UNIQUE INDEX IF NOT EXISTS idx_agent_skills_workspace_name
    ON agent_skills(workspace_id, name)
    WHERE workspace_id IS NOT NULL;

CREATE UNIQUE INDEX IF NOT EXISTS idx_agent_skills_global_name
    ON agent_skills(name)
    WHERE workspace_id IS NULL;

CREATE INDEX IF NOT EXISTS idx_agent_skills_state_scope
    ON agent_skills(state, scope);

CREATE TABLE IF NOT EXISTS agent_recipes (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id UUID REFERENCES workspaces(id) ON DELETE CASCADE,
    scope TEXT NOT NULL DEFAULT 'workspace'
        CHECK (scope IN ('global', 'workspace', 'session')),
    name TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    trigger_text TEXT NOT NULL DEFAULT '',
    steps_json JSONB NOT NULL DEFAULT '[]'::jsonb,
    success_count INTEGER NOT NULL DEFAULT 0,
    failure_count INTEGER NOT NULL DEFAULT 0,
    state TEXT NOT NULL DEFAULT 'approved'
        CHECK (state IN ('draft', 'approved', 'disabled', 'rejected')),
    created_by UUID REFERENCES users(id) ON DELETE SET NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_agent_recipes_scope_name
    ON agent_recipes(COALESCE(workspace_id, '00000000-0000-0000-0000-000000000000'::uuid), name, scope);

CREATE INDEX IF NOT EXISTS idx_agent_recipes_state
    ON agent_recipes(state, success_count DESC, updated_at DESC);

CREATE TABLE IF NOT EXISTS agent_kernel_memory (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id UUID REFERENCES workspaces(id) ON DELETE CASCADE,
    agent_profile_id UUID REFERENCES workspace_agents(id) ON DELETE CASCADE,
    signal_key TEXT NOT NULL,
    node_name TEXT NOT NULL,
    outcome TEXT NOT NULL DEFAULT '',
    evidence_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    success_count INTEGER NOT NULL DEFAULT 0,
    failure_count INTEGER NOT NULL DEFAULT 0,
    last_used_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE(agent_profile_id, signal_key, node_name)
);

CREATE INDEX IF NOT EXISTS idx_agent_kernel_memory_lookup
    ON agent_kernel_memory(agent_profile_id, signal_key, success_count DESC, updated_at DESC);
