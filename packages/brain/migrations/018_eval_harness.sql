-- Evaluation harness results table
CREATE TABLE IF NOT EXISTS eval_runs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    scenario_id TEXT NOT NULL,
    scenario_name TEXT NOT NULL,
    success BOOLEAN NOT NULL,
    iterations INTEGER DEFAULT 0,
    tool_calls INTEGER DEFAULT 0,
    token_usage INTEGER DEFAULT 0,
    wall_time_ms INTEGER DEFAULT 0,
    verifier_passed BOOLEAN,
    metrics_json JSONB DEFAULT '{}',
    git_sha TEXT DEFAULT '',
    created_at TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_eval_runs_scenario ON eval_runs(scenario_id, created_at);
CREATE INDEX IF NOT EXISTS idx_eval_runs_created ON eval_runs(created_at);
