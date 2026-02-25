// ── Telegram API Types ─────────────────────────────────────────────

export interface TelegramUser {
    id: number;
    first_name: string;
    last_name?: string;
    username?: string;
}

export interface TelegramChat {
    id: number;
    type: 'private' | 'group' | 'supergroup' | 'channel';
    is_forum?: boolean;
}

export interface TelegramForumTopic {
    message_thread_id: number;
    name: string;
    icon_color: number;
}

export interface TelegramMessage {
    message_id: number;
    message_thread_id?: number;
    from?: TelegramUser;
    chat: TelegramChat;
    date: number;
    text?: string;
    caption?: string;
    photo?: Array<{ file_id: string; file_unique_id: string; width: number; height: number; file_size?: number }>;
    document?: { file_id: string; file_name?: string; mime_type?: string; file_size?: number };
    voice?: { file_id: string; duration: number; mime_type?: string; file_size?: number };
    audio?: { file_id: string; duration: number; file_name?: string; mime_type?: string; file_size?: number };
    video?: { file_id: string; duration: number; width: number; height: number; mime_type?: string; file_size?: number };
}

export interface TelegramUpdate {
    update_id: number;
    message?: TelegramMessage;
    callback_query?: {
        id: string;
        from: TelegramUser;
        message?: TelegramMessage;
        data?: string;
    };
}

// ── Configuration ──────────────────────────────────────────────────

export interface TelegramConfig {
    botToken: string;
    webhookUrl?: string;           // If set, uses webhook mode
    mode: 'webhook' | 'polling';
    defaultWorkspaceId: string;    // Workspace to assign Telegram users to
    allowedUserIds?: number[];     // Optional: restrict to these Telegram user IDs
}
