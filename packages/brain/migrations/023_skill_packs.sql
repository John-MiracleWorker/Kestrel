-- Migration 023: Shared skill pack catalog

CREATE TABLE IF NOT EXISTS agent_skill_packs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id UUID REFERENCES workspaces(id) ON DELETE CASCADE,
    pack_id TEXT NOT NULL,
    version TEXT NOT NULL DEFAULT '',
    scope TEXT NOT NULL DEFAULT 'user',
    source_path TEXT NOT NULL,
    source_type TEXT NOT NULL DEFAULT 'directory',
    enabled BOOLEAN NOT NULL DEFAULT true,
    trusted BOOLEAN NOT NULL DEFAULT false,
    manifest_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    installed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    removed_at TIMESTAMPTZ
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_agent_skill_packs_workspace
    ON agent_skill_packs(workspace_id, pack_id)
    WHERE removed_at IS NULL;

CREATE UNIQUE INDEX IF NOT EXISTS idx_agent_skill_packs_global
    ON agent_skill_packs(pack_id)
    WHERE workspace_id IS NULL AND removed_at IS NULL;
