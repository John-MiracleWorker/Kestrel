-- Migration 007: Agent Sessions for cross-session communication
-- Supports the sessions_list / sessions_send / sessions_history tools

-- ── Agent Sessions ──────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS agent_sessions (
    id          TEXT PRIMARY KEY,
    task_id     TEXT REFERENCES agent_tasks(id) ON DELETE SET NULL,
    workspace_id TEXT NOT NULL,
    user_id     TEXT NOT NULL,
    agent_type  TEXT NOT NULL DEFAULT 'main',  -- main, specialist, cron
    status      TEXT NOT NULL DEFAULT 'active', -- active, idle, paused, completed
    model       TEXT DEFAULT '',
    current_goal TEXT DEFAULT '',
    token_usage INTEGER DEFAULT 0,
    started_at  TIMESTAMPTZ DEFAULT now(),
    last_activity TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_sessions_workspace
    ON agent_sessions(workspace_id, status);
CREATE INDEX IF NOT EXISTS idx_sessions_task
    ON agent_sessions(task_id);

-- ── Session Messages (inter-agent communication) ────────────────────

CREATE TABLE IF NOT EXISTS agent_session_messages (
    id               TEXT PRIMARY KEY,
    from_session_id  TEXT NOT NULL REFERENCES agent_sessions(id) ON DELETE CASCADE,
    to_session_id    TEXT NOT NULL REFERENCES agent_sessions(id) ON DELETE CASCADE,
    content          TEXT NOT NULL,
    message_type     TEXT NOT NULL DEFAULT 'text',  -- text, request, response, announce
    reply_to         TEXT REFERENCES agent_session_messages(id),
    metadata         JSONB DEFAULT '{}',
    created_at       TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_session_messages_to
    ON agent_session_messages(to_session_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_session_messages_from
    ON agent_session_messages(from_session_id, created_at DESC);

-- ── RLS Policies ────────────────────────────────────────────────────

ALTER TABLE agent_sessions ENABLE ROW LEVEL SECURITY;
ALTER TABLE agent_session_messages ENABLE ROW LEVEL SECURITY;

CREATE POLICY sessions_workspace_isolation ON agent_sessions
    USING (workspace_id = current_setting('app.workspace_id', true));

CREATE POLICY session_messages_access ON agent_session_messages
    USING (
        from_session_id IN (SELECT id FROM agent_sessions WHERE workspace_id = current_setting('app.workspace_id', true))
        OR to_session_id IN (SELECT id FROM agent_sessions WHERE workspace_id = current_setting('app.workspace_id', true))
    );
