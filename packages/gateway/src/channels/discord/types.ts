// ── Configuration ──────────────────────────────────────────────────

export interface DiscordConfig {
    botToken: string;
    clientId: string;
    guildId?: string;               // For guild-specific slash commands
    defaultWorkspaceId: string;
    allowedRoleIds?: string[];      // Optional: restrict to users with these roles
}

// ── Discord API Types ──────────────────────────────────────────────

export interface DiscordUser {
    id: string;
    username: string;
    discriminator: string;
    global_name?: string;
}

export interface DiscordMessagePayload {
    id: string;
    channel_id: string;
    author: DiscordUser & { bot?: boolean };
    content: string;
    timestamp: string;
    attachments?: Array<{
        id: string;
        filename: string;
        url: string;
        content_type?: string;
        size: number;
    }>;
    guild_id?: string;
}

export interface DiscordInteraction {
    id: string;
    type: number;                    // 2 = APPLICATION_COMMAND, 3 = MESSAGE_COMPONENT
    data?: {
        name: string;
        custom_id?: string;
        options?: Array<{ name: string; value: string; type: number }>;
    };
    channel_id: string;
    guild_id?: string;
    member?: { user: DiscordUser; roles?: string[] };
    user?: DiscordUser;
    token: string;
}