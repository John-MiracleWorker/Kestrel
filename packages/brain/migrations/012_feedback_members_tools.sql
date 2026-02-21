-- Migration 012: Feedback, multi-user, MCP tools tables
-- Adds tables for: message feedback, workspace members, workspace invites, installed MCP tools

-- ── Message Feedback (P2: Learning) ─────────────────────────────────
CREATE TABLE IF NOT EXISTS message_feedback (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES users(id),
    workspace_id UUID NOT NULL REFERENCES workspaces(id),
    conversation_id UUID NOT NULL,
    message_id TEXT NOT NULL,
    rating SMALLINT NOT NULL CHECK (rating IN (-1, 0, 1)),  -- -1=bad, 0=neutral, 1=good
    comment TEXT DEFAULT '',
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_feedback_workspace ON message_feedback(workspace_id);
CREATE INDEX IF NOT EXISTS idx_feedback_user ON message_feedback(user_id);
CREATE INDEX IF NOT EXISTS idx_feedback_conversation ON message_feedback(conversation_id);

-- ── Workspace Members (P3: Multi-User) ──────────────────────────────
CREATE TABLE IF NOT EXISTS workspace_members (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id UUID NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    role TEXT NOT NULL DEFAULT 'member' CHECK (role IN ('admin', 'member', 'viewer')),
    joined_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(workspace_id, user_id)
);

CREATE INDEX IF NOT EXISTS idx_members_workspace ON workspace_members(workspace_id);
CREATE INDEX IF NOT EXISTS idx_members_user ON workspace_members(user_id);

-- ── Workspace Invites (P3: Multi-User) ──────────────────────────────
CREATE TABLE IF NOT EXISTS workspace_invites (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id UUID NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    invited_by UUID NOT NULL REFERENCES users(id),
    email TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'member' CHECK (role IN ('admin', 'member', 'viewer')),
    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'accepted', 'declined', 'expired')),
    token TEXT NOT NULL UNIQUE,  -- unique invite token for link
    expires_at TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_invites_workspace ON workspace_invites(workspace_id);
CREATE INDEX IF NOT EXISTS idx_invites_email ON workspace_invites(email);

-- ── Installed MCP Tools (P2: Tool Marketplace) ──────────────────────
CREATE TABLE IF NOT EXISTS installed_tools (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id UUID NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    description TEXT DEFAULT '',
    server_url TEXT NOT NULL,       -- MCP server URL or stdio command
    transport TEXT NOT NULL DEFAULT 'stdio' CHECK (transport IN ('stdio', 'http', 'sse')),
    config JSONB DEFAULT '{}',      -- Connection config, env vars, etc.
    tool_schemas JSONB DEFAULT '[]', -- Cached tool definitions from server
    enabled BOOLEAN DEFAULT true,
    installed_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(workspace_id, name)
);

CREATE INDEX IF NOT EXISTS idx_installed_tools_workspace ON installed_tools(workspace_id);

-- ── Notifications (P1: Proactive) ───────────────────────────────────
CREATE TABLE IF NOT EXISTS notifications (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    workspace_id UUID NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    type TEXT NOT NULL DEFAULT 'info' CHECK (type IN ('info', 'success', 'warning', 'task_complete', 'mention')),
    title TEXT NOT NULL,
    body TEXT DEFAULT '',
    source TEXT DEFAULT '',         -- e.g., 'cron:daily-report', 'webhook:github'
    read BOOLEAN DEFAULT false,
    data JSONB DEFAULT '{}',        -- Extra context
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_notifications_user ON notifications(user_id, read);
CREATE INDEX IF NOT EXISTS idx_notifications_workspace ON notifications(workspace_id);
