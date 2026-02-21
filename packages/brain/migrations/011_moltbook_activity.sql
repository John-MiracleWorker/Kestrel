-- Moltbook activity log — tracks all Kestrel interactions with Moltbook
-- so the human can see what/when/where the agent is posting.

CREATE TABLE IF NOT EXISTS moltbook_activity (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id    UUID REFERENCES workspaces(id) ON DELETE CASCADE,
    action          TEXT NOT NULL,           -- 'post', 'comment', 'upvote', 'register', etc.
    title           TEXT,                    -- post title (if applicable)
    content         TEXT,                    -- post/comment content
    submolt         TEXT,                    -- community name
    post_id         TEXT,                    -- moltbook post ID
    url             TEXT,                    -- link to the content on moltbook.com
    result          JSONB,                   -- raw API response data
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_moltbook_activity_workspace ON moltbook_activity(workspace_id, created_at DESC);
