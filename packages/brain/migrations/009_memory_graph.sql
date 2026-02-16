-- Migration 009: Memory Graph, Personas, Evidence Chain
-- Supports Kestrel-unique differentiating features

-- ── Memory Graph Nodes ──────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS memory_graph_nodes (
    id                     TEXT PRIMARY KEY,
    workspace_id           TEXT NOT NULL,
    entity_type            TEXT NOT NULL,     -- person, project, file, concept, decision, etc.
    name                   TEXT NOT NULL,
    description            TEXT DEFAULT '',
    properties             JSONB DEFAULT '{}',
    weight                 REAL DEFAULT 1.0,
    mention_count          INTEGER DEFAULT 1,
    first_seen             TIMESTAMPTZ DEFAULT now(),
    last_seen              TIMESTAMPTZ DEFAULT now(),
    source_conversation_id TEXT
);

CREATE INDEX IF NOT EXISTS idx_graph_nodes_workspace
    ON memory_graph_nodes(workspace_id, entity_type);
CREATE INDEX IF NOT EXISTS idx_graph_nodes_name
    ON memory_graph_nodes(workspace_id, name);
CREATE INDEX IF NOT EXISTS idx_graph_nodes_weight
    ON memory_graph_nodes(workspace_id, weight DESC);

-- ── Memory Graph Edges ──────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS memory_graph_edges (
    id              TEXT PRIMARY KEY,
    source_id       TEXT NOT NULL REFERENCES memory_graph_nodes(id) ON DELETE CASCADE,
    target_id       TEXT NOT NULL REFERENCES memory_graph_nodes(id) ON DELETE CASCADE,
    relation_type   TEXT NOT NULL,    -- mentioned_in, depends_on, related_to, etc.
    strength        REAL DEFAULT 1.0,
    context         TEXT DEFAULT '',
    conversation_id TEXT,
    created_at      TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_graph_edges_source
    ON memory_graph_edges(source_id);
CREATE INDEX IF NOT EXISTS idx_graph_edges_target
    ON memory_graph_edges(target_id);

-- ── User Personas ───────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS user_personas (
    user_id     TEXT PRIMARY KEY,
    preferences JSONB DEFAULT '{}',
    updated_at  TIMESTAMPTZ DEFAULT now()
);

-- ── Evidence Chain ──────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS evidence_chain (
    id             TEXT PRIMARY KEY,
    task_id        TEXT NOT NULL,
    step_number    INTEGER NOT NULL,
    decision_type  TEXT NOT NULL,
    description    TEXT NOT NULL,
    reasoning      TEXT DEFAULT '',
    evidence       JSONB DEFAULT '[]',
    alternatives   JSONB DEFAULT '[]',
    confidence     REAL DEFAULT 0.5,
    outcome        TEXT,
    created_at     TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_evidence_task
    ON evidence_chain(task_id, step_number);

-- ── RLS ─────────────────────────────────────────────────────────────

ALTER TABLE memory_graph_nodes ENABLE ROW LEVEL SECURITY;
ALTER TABLE memory_graph_edges ENABLE ROW LEVEL SECURITY;
ALTER TABLE user_personas ENABLE ROW LEVEL SECURITY;
ALTER TABLE evidence_chain ENABLE ROW LEVEL SECURITY;

CREATE POLICY graph_nodes_workspace_isolation ON memory_graph_nodes
    USING (workspace_id = current_setting('app.workspace_id', true));

CREATE POLICY graph_edges_access ON memory_graph_edges
    USING (source_id IN (
        SELECT id FROM memory_graph_nodes
        WHERE workspace_id = current_setting('app.workspace_id', true)
    ));

CREATE POLICY personas_user_access ON user_personas
    USING (user_id = current_setting('app.user_id', true));

CREATE POLICY evidence_chain_access ON evidence_chain
    USING (task_id IN (
        SELECT id FROM agent_tasks
        WHERE workspace_id = current_setting('app.workspace_id', true)
    ));
