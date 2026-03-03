-- Migration 015: Add unique constraint on cron job names per workspace
-- Prevents duplicate cron jobs from being created when the agent loop
-- retries or replays tool calls.

-- Add next_run column if absent (used in code but missing from 008)
ALTER TABLE automation_cron_jobs
    ADD COLUMN IF NOT EXISTS next_run TIMESTAMPTZ;

-- Remove existing duplicates, keeping the most recently created row
DELETE FROM automation_cron_jobs a
    USING automation_cron_jobs b
    WHERE a.workspace_id = b.workspace_id
      AND a.name = b.name
      AND a.created_at < b.created_at;

-- Now enforce uniqueness
ALTER TABLE automation_cron_jobs
    ADD CONSTRAINT uq_cron_workspace_name UNIQUE (workspace_id, name);
