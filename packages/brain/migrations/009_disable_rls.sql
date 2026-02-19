-- Re-enable RLS
ALTER TABLE conversations ENABLE ROW LEVEL SECURITY;
ALTER TABLE messages ENABLE ROW LEVEL SECURITY;
ALTER TABLE context_snapshots ENABLE ROW LEVEL SECURITY;
ALTER TABLE memories ENABLE ROW LEVEL SECURITY;
ALTER TABLE journal_entries ENABLE ROW LEVEL SECURITY;
ALTER TABLE tasks ENABLE ROW LEVEL SECURITY;
ALTER TABLE settings ENABLE ROW LEVEL SECURITY;

-- Create policies to allow the application role (kestrel) full access,
-- while still keeping RLS active for defense against unauthorized roles.

DO $$
DECLARE
    role_exists boolean;
BEGIN
    SELECT EXISTS(SELECT 1 FROM pg_roles WHERE rolname = 'kestrel') INTO role_exists;
    IF role_exists THEN
        -- Drop if exists to be idempotent
        DROP POLICY IF EXISTS "kestrel_bypass_conversations" ON conversations;
        DROP POLICY IF EXISTS "kestrel_bypass_messages" ON messages;
        DROP POLICY IF EXISTS "kestrel_bypass_context_snapshots" ON context_snapshots;
        DROP POLICY IF EXISTS "kestrel_bypass_memories" ON memories;
        DROP POLICY IF EXISTS "kestrel_bypass_journal_entries" ON journal_entries;
        DROP POLICY IF EXISTS "kestrel_bypass_tasks" ON tasks;
        DROP POLICY IF EXISTS "kestrel_bypass_settings" ON settings;

        CREATE POLICY "kestrel_bypass_conversations" ON conversations FOR ALL TO kestrel USING (true) WITH CHECK (true);
        CREATE POLICY "kestrel_bypass_messages" ON messages FOR ALL TO kestrel USING (true) WITH CHECK (true);
        CREATE POLICY "kestrel_bypass_context_snapshots" ON context_snapshots FOR ALL TO kestrel USING (true) WITH CHECK (true);
        CREATE POLICY "kestrel_bypass_memories" ON memories FOR ALL TO kestrel USING (true) WITH CHECK (true);
        CREATE POLICY "kestrel_bypass_journal_entries" ON journal_entries FOR ALL TO kestrel USING (true) WITH CHECK (true);
        CREATE POLICY "kestrel_bypass_tasks" ON tasks FOR ALL TO kestrel USING (true) WITH CHECK (true);
        CREATE POLICY "kestrel_bypass_settings" ON settings FOR ALL TO kestrel USING (true) WITH CHECK (true);
    END IF;
END
$$;
