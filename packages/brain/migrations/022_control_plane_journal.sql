-- 022_control_plane_journal.sql
-- Typed task journal, action receipts, verifier evidence, and cross-surface session routing.

CREATE TABLE IF NOT EXISTS task_event_journal (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    sequence_id BIGSERIAL UNIQUE,
    workspace_id UUID REFERENCES workspaces(id) ON DELETE CASCADE,
    user_id UUID REFERENCES users(id) ON DELETE SET NULL,
    task_id UUID REFERENCES agent_tasks(id) ON DELETE CASCADE,
    event_type TEXT NOT NULL,
    step_id TEXT NOT NULL DEFAULT '',
    tool_name TEXT NOT NULL DEFAULT '',
    approval_id UUID REFERENCES agent_approvals(id) ON DELETE SET NULL,
    progress_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    metadata_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    metrics_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    payload_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_task_event_journal_task
    ON task_event_journal(task_id, sequence_id);
CREATE INDEX IF NOT EXISTS idx_task_event_journal_workspace
    ON task_event_journal(workspace_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_task_event_journal_event
    ON task_event_journal(event_type, created_at DESC);

CREATE TABLE IF NOT EXISTS action_receipts (
    receipt_id UUID PRIMARY KEY,
    workspace_id UUID REFERENCES workspaces(id) ON DELETE CASCADE,
    task_id UUID REFERENCES agent_tasks(id) ON DELETE CASCADE,
    step_id TEXT NOT NULL DEFAULT '',
    tool_name TEXT NOT NULL DEFAULT '',
    request_id TEXT NOT NULL DEFAULT '',
    runtime_class TEXT NOT NULL DEFAULT '',
    risk_class TEXT NOT NULL DEFAULT '',
    failure_class TEXT NOT NULL DEFAULT 'none',
    logs_pointer TEXT NOT NULL DEFAULT '',
    stdout_pointer TEXT NOT NULL DEFAULT '',
    stderr_pointer TEXT NOT NULL DEFAULT '',
    sandbox_id TEXT NOT NULL DEFAULT '',
    exit_code INTEGER NOT NULL DEFAULT 0,
    audit_summary TEXT NOT NULL DEFAULT '',
    artifact_manifest JSONB NOT NULL DEFAULT '[]'::jsonb,
    file_touches JSONB NOT NULL DEFAULT '[]'::jsonb,
    network_touches JSONB NOT NULL DEFAULT '[]'::jsonb,
    system_touches JSONB NOT NULL DEFAULT '[]'::jsonb,
    grants_json JSONB NOT NULL DEFAULT '[]'::jsonb,
    metadata_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    mutating BOOLEAN NOT NULL DEFAULT false,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_action_receipts_task
    ON action_receipts(task_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_action_receipts_workspace
    ON action_receipts(workspace_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_action_receipts_failure
    ON action_receipts(failure_class, created_at DESC);

CREATE TABLE IF NOT EXISTS verifier_claim_evidence (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id UUID REFERENCES workspaces(id) ON DELETE CASCADE,
    task_id UUID REFERENCES agent_tasks(id) ON DELETE CASCADE,
    step_id TEXT NOT NULL DEFAULT '',
    claim_text TEXT NOT NULL,
    verdict TEXT NOT NULL DEFAULT 'unknown',
    confidence DOUBLE PRECISION NOT NULL DEFAULT 0,
    rationale TEXT NOT NULL DEFAULT '',
    supporting_receipt_ids JSONB NOT NULL DEFAULT '[]'::jsonb,
    artifact_refs JSONB NOT NULL DEFAULT '[]'::jsonb,
    metadata_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_verifier_claim_evidence_task
    ON verifier_claim_evidence(task_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_verifier_claim_evidence_workspace
    ON verifier_claim_evidence(workspace_id, created_at DESC);

ALTER TABLE agent_sessions
    ADD COLUMN IF NOT EXISTS external_conversation_id TEXT NOT NULL DEFAULT '',
    ADD COLUMN IF NOT EXISTS external_thread_id TEXT NOT NULL DEFAULT '',
    ADD COLUMN IF NOT EXISTS return_route_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    ADD COLUMN IF NOT EXISTS session_metadata_json JSONB NOT NULL DEFAULT '{}'::jsonb;

ALTER TABLE agent_checkpoints
    ADD COLUMN IF NOT EXISTS journal_event_id UUID REFERENCES task_event_journal(id) ON DELETE SET NULL;

ALTER TABLE agent_approvals
    ADD COLUMN IF NOT EXISTS capability_grants_json JSONB NOT NULL DEFAULT '[]'::jsonb;
