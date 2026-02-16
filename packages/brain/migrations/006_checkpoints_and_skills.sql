-- Migration 006: Agent checkpoints and dynamic skills
-- Supports state rollback and agent-created tools

-- ── Checkpoints Table ──────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS agent_checkpoints (
    id              TEXT PRIMARY KEY,
    task_id         TEXT NOT NULL REFERENCES agent_tasks(id) ON DELETE CASCADE,
    step_index      INTEGER NOT NULL DEFAULT 0,
    label           TEXT NOT NULL DEFAULT '',
    state_json      JSONB NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_checkpoints_task ON agent_checkpoints(task_id);
CREATE INDEX idx_checkpoints_task_time ON agent_checkpoints(task_id, created_at DESC);

-- ── Dynamic Skills Table ───────────────────────────────────────────

CREATE TABLE IF NOT EXISTS agent_skills (
    id              TEXT PRIMARY KEY DEFAULT gen_random_uuid()::text,
    workspace_id    TEXT NOT NULL,
    name            TEXT NOT NULL,
    description     TEXT NOT NULL DEFAULT '',
    python_code     TEXT NOT NULL,
    parameters      JSONB NOT NULL DEFAULT '{}',
    risk_level      TEXT NOT NULL DEFAULT 'medium',
    created_by      TEXT NOT NULL,
    enabled         BOOLEAN NOT NULL DEFAULT true,
    usage_count     INTEGER NOT NULL DEFAULT 0,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),

    UNIQUE(workspace_id, name)
);

CREATE INDEX idx_skills_workspace ON agent_skills(workspace_id);

-- Auto-update timestamp trigger
CREATE TRIGGER set_skills_updated_at
    BEFORE UPDATE ON agent_skills
    FOR EACH ROW
    EXECUTE FUNCTION update_updated_at_column();

-- RLS policies
ALTER TABLE agent_checkpoints ENABLE ROW LEVEL SECURITY;
ALTER TABLE agent_skills ENABLE ROW LEVEL SECURITY;

CREATE POLICY checkpoints_all ON agent_checkpoints
    USING (true)
    WITH CHECK (true);

CREATE POLICY skills_all ON agent_skills
    USING (true)
    WITH CHECK (true);
