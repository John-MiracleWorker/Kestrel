-- Macro registry for reusable tool sequences
CREATE TABLE IF NOT EXISTS macros (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id UUID REFERENCES workspaces(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    description TEXT DEFAULT '',
    steps_json JSONB NOT NULL,
    variables JSONB DEFAULT '[]',
    version INTEGER DEFAULT 1,
    enabled BOOLEAN DEFAULT true,
    created_at TIMESTAMPTZ DEFAULT now(),
    updated_at TIMESTAMPTZ DEFAULT now(),
    UNIQUE(workspace_id, name)
);

-- Task state transition audit log
CREATE TABLE IF NOT EXISTS task_state_log (
    id BIGSERIAL PRIMARY KEY,
    task_id UUID NOT NULL,
    from_status TEXT NOT NULL,
    to_status TEXT NOT NULL,
    legal BOOLEAN DEFAULT true,
    triggered_at TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_task_state_log_task ON task_state_log(task_id, triggered_at);
