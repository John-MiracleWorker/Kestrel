-- ============================================================
-- Libre Bird Platform — Initial PostgreSQL Schema
-- Multi-user, multi-workspace, multi-channel
-- ============================================================

-- Extensions
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
CREATE EXTENSION IF NOT EXISTS "pgcrypto";

-- ── Users ────────────────────────────────────────────────────
CREATE TABLE users (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    email TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    salt TEXT NOT NULL,
    display_name TEXT NOT NULL DEFAULT '',
    avatar_url TEXT,
    is_active BOOLEAN DEFAULT TRUE,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_users_email ON users(email);

-- ── Workspaces ───────────────────────────────────────────────
CREATE TABLE workspaces (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name TEXT NOT NULL,
    description TEXT,
    settings JSONB DEFAULT '{}',
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

-- ── Workspace Members ────────────────────────────────────────
CREATE TABLE workspace_members (
    workspace_id UUID REFERENCES workspaces(id) ON DELETE CASCADE,
    user_id UUID REFERENCES users(id) ON DELETE CASCADE,
    role TEXT NOT NULL DEFAULT 'member'
        CHECK (role IN ('owner', 'admin', 'member', 'guest')),
    joined_at TIMESTAMPTZ DEFAULT NOW(),
    PRIMARY KEY (workspace_id, user_id)
);

CREATE INDEX idx_workspace_members_user ON workspace_members(user_id);

-- ── Conversations ────────────────────────────────────────────
CREATE TABLE conversations (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id UUID NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    title TEXT NOT NULL DEFAULT 'New Conversation',
    channel TEXT DEFAULT 'web'
        CHECK (channel IN ('web', 'whatsapp', 'telegram', 'discord', 'mobile', 'cli')),
    is_archived BOOLEAN DEFAULT FALSE,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_conversations_workspace ON conversations(workspace_id);
CREATE INDEX idx_conversations_updated ON conversations(updated_at DESC);

-- ── Messages ─────────────────────────────────────────────────
CREATE TABLE messages (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    conversation_id UUID NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    role TEXT NOT NULL CHECK (role IN ('user', 'assistant', 'system', 'tool')),
    content TEXT NOT NULL,
    tool_calls JSONB,          -- [{name, arguments, id}]
    tool_call_id TEXT,         -- for role='tool' responses
    metadata JSONB DEFAULT '{}',
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_messages_conversation ON messages(conversation_id, created_at);

-- ── Context Snapshots (desktop agent) ────────────────────────
CREATE TABLE context_snapshots (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id UUID REFERENCES workspaces(id) ON DELETE CASCADE,
    app_name TEXT,
    window_title TEXT,
    focused_text TEXT,
    bundle_id TEXT,
    captured_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_context_workspace ON context_snapshots(workspace_id, captured_at);

-- ── Long-term Memories ───────────────────────────────────────
CREATE TABLE memories (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id UUID REFERENCES workspaces(id) ON DELETE CASCADE,
    memory_date DATE NOT NULL,
    summary TEXT NOT NULL,
    app_names TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_memories_workspace ON memories(workspace_id, memory_date);

-- ── Journal Entries ──────────────────────────────────────────
CREATE TABLE journal_entries (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id UUID REFERENCES workspaces(id) ON DELETE CASCADE,
    entry_date DATE NOT NULL,
    summary TEXT NOT NULL,
    activities JSONB,
    tasks_extracted JSONB,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (workspace_id, entry_date)
);

-- ── Tasks ────────────────────────────────────────────────────
CREATE TABLE tasks (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id UUID REFERENCES workspaces(id) ON DELETE CASCADE,
    title TEXT NOT NULL,
    description TEXT,
    status TEXT DEFAULT 'todo'
        CHECK (status IN ('todo', 'in_progress', 'done', 'dismissed')),
    priority TEXT DEFAULT 'medium'
        CHECK (priority IN ('low', 'medium', 'high')),
    source TEXT,
    source_id TEXT,
    assigned_to UUID REFERENCES users(id),
    created_at TIMESTAMPTZ DEFAULT NOW(),
    completed_at TIMESTAMPTZ
);

CREATE INDEX idx_tasks_workspace ON tasks(workspace_id, status);

-- ── Settings ─────────────────────────────────────────────────
CREATE TABLE settings (
    workspace_id UUID REFERENCES workspaces(id) ON DELETE CASCADE,
    key TEXT NOT NULL,
    value TEXT NOT NULL,
    PRIMARY KEY (workspace_id, key)
);

-- ── Audit Log ────────────────────────────────────────────────
CREATE TABLE audit_log (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id UUID REFERENCES workspaces(id) ON DELETE SET NULL,
    user_id UUID REFERENCES users(id) ON DELETE SET NULL,
    action TEXT NOT NULL,
    resource_type TEXT,
    resource_id TEXT,
    details JSONB DEFAULT '{}',
    ip_address INET,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_audit_workspace ON audit_log(workspace_id, created_at DESC);

-- ── Push Tokens (mobile) ─────────────────────────────────────
CREATE TABLE push_tokens (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    device_token TEXT NOT NULL,
    platform TEXT CHECK (platform IN ('ios', 'android', 'web')),
    created_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (user_id, device_token)
);

-- ── Row-Level Security (RLS) ─────────────────────────────────
-- Enable RLS on multi-tenant tables
ALTER TABLE conversations ENABLE ROW LEVEL SECURITY;
ALTER TABLE messages ENABLE ROW LEVEL SECURITY;
ALTER TABLE context_snapshots ENABLE ROW LEVEL SECURITY;
ALTER TABLE memories ENABLE ROW LEVEL SECURITY;
ALTER TABLE journal_entries ENABLE ROW LEVEL SECURITY;
ALTER TABLE tasks ENABLE ROW LEVEL SECURITY;
ALTER TABLE settings ENABLE ROW LEVEL SECURITY;

-- Application must SET LOCAL app.current_user_id before each request.
-- Example: SET LOCAL app.current_user_id = '<uuid>';

-- Helper function: returns workspace IDs the current user belongs to
CREATE OR REPLACE FUNCTION user_workspace_ids() RETURNS SETOF UUID AS $$
  SELECT workspace_id FROM workspace_members
  WHERE user_id = current_setting('app.current_user_id', true)::uuid;
$$ LANGUAGE sql SECURITY DEFINER STABLE;

-- Conversations: users can only see conversations in their workspaces
CREATE POLICY workspace_isolation ON conversations
    USING (workspace_id IN (SELECT user_workspace_ids()));

-- Messages: accessible if the parent conversation is accessible
CREATE POLICY workspace_isolation ON messages
    USING (conversation_id IN (
        SELECT id FROM conversations
        WHERE workspace_id IN (SELECT user_workspace_ids())
    ));

-- Context snapshots: workspace-scoped
CREATE POLICY workspace_isolation ON context_snapshots
    USING (workspace_id IN (SELECT user_workspace_ids()));

-- Memories: workspace-scoped
CREATE POLICY workspace_isolation ON memories
    USING (workspace_id IN (SELECT user_workspace_ids()));

-- Journal entries: workspace-scoped
CREATE POLICY workspace_isolation ON journal_entries
    USING (workspace_id IN (SELECT user_workspace_ids()));

-- Tasks: workspace-scoped
CREATE POLICY workspace_isolation ON tasks
    USING (workspace_id IN (SELECT user_workspace_ids()));

-- Settings: workspace-scoped
CREATE POLICY workspace_isolation ON settings
    USING (workspace_id IN (SELECT user_workspace_ids()));

