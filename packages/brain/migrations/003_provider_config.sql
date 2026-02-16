-- ============================================================
-- Kestrel — Provider Configuration (per-workspace)
-- ============================================================

-- ── Provider Config ──────────────────────────────────────────
CREATE TABLE workspace_provider_config (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id UUID NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    provider TEXT NOT NULL CHECK (provider IN ('local', 'openai', 'anthropic', 'google')),
    is_default BOOLEAN DEFAULT FALSE,
    model TEXT,                       -- override default model for this provider
    api_key_encrypted TEXT,           -- workspace-specific API key (encrypted)
    temperature FLOAT DEFAULT 0.7,
    max_tokens INTEGER DEFAULT 2048,
    system_prompt TEXT,               -- workspace-level system prompt
    settings JSONB DEFAULT '{}',      -- additional provider-specific settings
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (workspace_id, provider)
);

CREATE INDEX idx_provider_config_workspace ON workspace_provider_config(workspace_id);

-- ── RAG Settings ─────────────────────────────────────────────
ALTER TABLE workspace_provider_config
    ADD COLUMN rag_enabled BOOLEAN DEFAULT TRUE,
    ADD COLUMN rag_top_k INTEGER DEFAULT 5,
    ADD COLUMN rag_min_similarity FLOAT DEFAULT 0.3;

-- ── RLS ──────────────────────────────────────────────────────
ALTER TABLE workspace_provider_config ENABLE ROW LEVEL SECURITY;

CREATE POLICY workspace_isolation ON workspace_provider_config
    USING (workspace_id IN (SELECT user_workspace_ids()));

-- ── Helper: get default provider for a workspace ─────────────
CREATE OR REPLACE FUNCTION get_default_provider(ws_id UUID)
RETURNS TABLE (
    provider TEXT,
    model TEXT,
    api_key_encrypted TEXT,
    temperature FLOAT,
    max_tokens INTEGER,
    system_prompt TEXT,
    rag_enabled BOOLEAN,
    rag_top_k INTEGER,
    rag_min_similarity FLOAT,
    settings JSONB
) AS $$
    SELECT provider, model, api_key_encrypted, temperature,
           max_tokens, system_prompt, rag_enabled, rag_top_k,
           rag_min_similarity, settings
    FROM workspace_provider_config
    WHERE workspace_id = ws_id AND is_default = TRUE
    LIMIT 1;
$$ LANGUAGE sql SECURITY DEFINER STABLE;
