-- 014_approval_memory.sql — Approval pattern learning for autonomous execution
--
-- Tracks generalized (tool_name, args_pattern) approval history per workspace.
-- After a user approves the same pattern N times, the agent auto-approves
-- future matching tool calls without blocking.

CREATE TABLE IF NOT EXISTS approval_patterns (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    pattern_hash    TEXT NOT NULL,
    tool_name       TEXT NOT NULL,
    args_pattern    JSONB NOT NULL DEFAULT '{}'::jsonb,
    workspace_id    UUID NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    user_id         UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    approval_count  INTEGER NOT NULL DEFAULT 0,
    denial_count    INTEGER NOT NULL DEFAULT 0,
    last_approved_at TIMESTAMPTZ,
    last_denied_at  TIMESTAMPTZ,
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (pattern_hash, workspace_id)
);

CREATE INDEX IF NOT EXISTS idx_approval_patterns_lookup
    ON approval_patterns(pattern_hash, workspace_id);
CREATE INDEX IF NOT EXISTS idx_approval_patterns_workspace
    ON approval_patterns(workspace_id);
