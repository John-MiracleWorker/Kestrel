-- Proactive signals table for tracking signal history and hypothesis audit trail
CREATE TABLE IF NOT EXISTS proactive_signals (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id UUID,
    user_id UUID,
    source TEXT NOT NULL,
    source_id TEXT DEFAULT '',
    title TEXT DEFAULT '',
    body TEXT DEFAULT '',
    severity TEXT DEFAULT 'info',
    fingerprint TEXT DEFAULT '',
    hypothesis_id TEXT DEFAULT '',
    created_at TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_proactive_signals_fp ON proactive_signals(fingerprint, created_at);
CREATE INDEX IF NOT EXISTS idx_proactive_signals_workspace ON proactive_signals(workspace_id, created_at);

-- User personas table for adaptive persona system
CREATE TABLE IF NOT EXISTS user_personas (
    user_id UUID PRIMARY KEY REFERENCES users(id),
    preferences JSONB DEFAULT '{}',
    updated_at TIMESTAMPTZ DEFAULT now()
);
