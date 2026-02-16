-- Migration 004: Channel Identities
-- Links external platform user IDs to Kestrel user accounts
-- for cross-channel conversation continuity.

-- ── Channel Identities Table ──────────────────────────────────────

CREATE TABLE IF NOT EXISTS user_channel_identities (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    channel TEXT NOT NULL CHECK (channel IN ('web', 'whatsapp', 'telegram', 'discord', 'mobile')),
    channel_user_id TEXT NOT NULL,
    display_name TEXT,
    linked BOOLEAN DEFAULT FALSE,
    metadata JSONB DEFAULT '{}',
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW(),

    -- Each channel+platform_id pair must be unique
    UNIQUE (channel, channel_user_id)
);

-- Fast lookups by channel + channel_user_id (primary access pattern)
CREATE INDEX IF NOT EXISTS idx_channel_identities_lookup
    ON user_channel_identities(channel, channel_user_id);

-- Reverse lookup: all identities for a Kestrel user
CREATE INDEX IF NOT EXISTS idx_channel_identities_user
    ON user_channel_identities(user_id);

-- ── RLS ───────────────────────────────────────────────────────────

ALTER TABLE user_channel_identities ENABLE ROW LEVEL SECURITY;

-- Users can view their own identities
CREATE POLICY user_channel_identities_select ON user_channel_identities
    FOR SELECT USING (user_id = current_setting('app.user_id')::uuid);

-- Users can manage their own identities
CREATE POLICY user_channel_identities_insert ON user_channel_identities
    FOR INSERT WITH CHECK (user_id = current_setting('app.user_id')::uuid);

CREATE POLICY user_channel_identities_update ON user_channel_identities
    FOR UPDATE USING (user_id = current_setting('app.user_id')::uuid);

CREATE POLICY user_channel_identities_delete ON user_channel_identities
    FOR DELETE USING (user_id = current_setting('app.user_id')::uuid);

-- ── Helper Function ───────────────────────────────────────────────

-- Resolve a channel user ID to a Kestrel user ID
CREATE OR REPLACE FUNCTION resolve_channel_user(
    p_channel TEXT,
    p_channel_user_id TEXT
) RETURNS UUID AS $$
    SELECT user_id
    FROM user_channel_identities
    WHERE channel = p_channel AND channel_user_id = p_channel_user_id
    LIMIT 1;
$$ LANGUAGE SQL STABLE;

-- ── Updated Timestamp Trigger ─────────────────────────────────────

CREATE OR REPLACE FUNCTION update_channel_identity_timestamp()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_channel_identity_updated
    BEFORE UPDATE ON user_channel_identities
    FOR EACH ROW EXECUTE FUNCTION update_channel_identity_timestamp();
