import { randomUUID } from 'crypto';
import {
    BaseChannelAdapter,
    ChannelType,
    IncomingMessage,
    OutgoingMessage,
    Attachment,
} from './base';
import { logger } from '../utils/logger';

// ── Telegram API Types ─────────────────────────────────────────────

interface TelegramUser {
    id: number;
    first_name: string;
    last_name?: string;
    username?: string;
}

interface TelegramChat {
    id: number;
    type: 'private' | 'group' | 'supergroup' | 'channel';
}

interface TelegramMessage {
    message_id: number;
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

interface TelegramUpdate {
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
}

// ── Telegram Adapter ───────────────────────────────────────────────

/**
 * Telegram Bot adapter — supports both webhook and polling modes.
 * Maps Telegram chats → Kestrel conversations, handles commands,
 * inline keyboards from OutgoingMessage.buttons, and file download.
 */
export class TelegramAdapter extends BaseChannelAdapter {
    readonly channelType: ChannelType = 'telegram';

    private readonly apiBase: string;
    private pollingActive = false;
    private pollingOffset = 0;
    private pollingTimer?: NodeJS.Timeout;

    // Telegram chat ID → userId mapping (for send())
    private chatIdMap = new Map<string, number>();   // kestrelUserId → telegramChatId
    private userIdMap = new Map<number, string>();    // telegramChatId → kestrelUserId

    constructor(private config: TelegramConfig) {
        super();
        this.apiBase = `https://api.telegram.org/bot${config.botToken}`;
    }

    // ── Lifecycle ──────────────────────────────────────────────────

    async connect(): Promise<void> {
        this.setStatus('connecting');

        // Validate token
        const me = await this.api('getMe');
        logger.info(`Telegram bot connected: @${me.username} (${me.id})`);

        if (this.config.mode === 'webhook' && this.config.webhookUrl) {
            await this.api('setWebhook', {
                url: `${this.config.webhookUrl}`,
                allowed_updates: JSON.stringify(['message', 'callback_query']),
            });
            logger.info(`Telegram webhook set: ${this.config.webhookUrl}`);
        } else {
            // Polling mode — delete any existing webhook first
            await this.api('deleteWebhook');
            this.startPolling();
            logger.info('Telegram polling started');
        }

        this.setStatus('connected');
    }

    async disconnect(): Promise<void> {
        this.pollingActive = false;
        if (this.pollingTimer) clearTimeout(this.pollingTimer);

        if (this.config.mode === 'webhook') {
            await this.api('deleteWebhook').catch(() => { /* best-effort */ });
        }

        this.setStatus('disconnected');
        logger.info('Telegram adapter disconnected');
    }

    // ── Sending ────────────────────────────────────────────────────

    async send(userId: string, message: OutgoingMessage): Promise<void> {
        const chatId = this.chatIdMap.get(userId);
        if (!chatId) {
            logger.warn('Cannot send Telegram message — no chat ID for user', { userId });
            return;
        }

        const params: Record<string, any> = {
            chat_id: chatId,
            text: message.content,
            parse_mode: 'Markdown',
        };

        // Inline keyboard from buttons
        if (message.options?.buttons?.length) {
            params.reply_markup = JSON.stringify({
                inline_keyboard: [
                    message.options.buttons.map((btn) => ({
                        text: btn.label,
                        callback_data: btn.action,
                    })),
                ],
            });
        }

        await this.api('sendMessage', params);

        // Send attachments as separate messages
        if (message.attachments?.length) {
            for (const att of message.attachments) {
                await this.sendAttachment(chatId, att);
            }
        }
    }

    private async sendAttachment(chatId: number, attachment: Attachment): Promise<void> {
        switch (attachment.type) {
            case 'image':
                await this.api('sendPhoto', { chat_id: chatId, photo: attachment.url });
                break;
            case 'audio':
                await this.api('sendAudio', { chat_id: chatId, audio: attachment.url });
                break;
            case 'video':
                await this.api('sendVideo', { chat_id: chatId, video: attachment.url });
                break;
            case 'file':
                await this.api('sendDocument', { chat_id: chatId, document: attachment.url });
                break;
        }
    }

    // ── Formatting ─────────────────────────────────────────────────

    formatOutgoing(message: OutgoingMessage): OutgoingMessage {
        // Truncate very long messages (Telegram limit: 4096 chars)
        let content = message.content;
        if (content.length > 4000) {
            content = content.substring(0, 3997) + '...';
        }
        return { ...message, content };
    }

    // ── Attachment Processing ──────────────────────────────────────

    async handleAttachment(attachment: Attachment): Promise<Attachment> {
        // For Telegram, file URLs need to be fetched through the Bot API
        // The attachment.url contains the file_id, which we resolve to a real URL
        if (attachment.url.startsWith('tg://')) {
            const fileId = attachment.url.replace('tg://', '');
            const file = await this.api('getFile', { file_id: fileId });
            attachment.url = `https://api.telegram.org/file/bot${this.config.botToken}/${file.file_path}`;
        }
        return attachment;
    }

    // ── Webhook Handler ────────────────────────────────────────────

    /**
     * Process an incoming Telegram update (called by the webhook route).
     */
    async processUpdate(update: TelegramUpdate): Promise<void> {
        if (update.callback_query) {
            await this.handleCallbackQuery(update.callback_query);
            return;
        }

        if (!update.message) return;

        const msg = update.message;
        const from = msg.from;
        if (!from) return;

        // Handle commands
        const text = msg.text || msg.caption || '';
        if (text.startsWith('/')) {
            await this.handleCommand(msg, text);
            return;
        }

        // Map Telegram user → Kestrel user
        const userId = this.resolveUserId(from, msg.chat.id);

        // Build attachments
        const attachments: Attachment[] = [];
        if (msg.photo?.length) {
            const largest = msg.photo[msg.photo.length - 1];
            attachments.push({
                type: 'image',
                url: `tg://${largest.file_id}`,
                size: largest.file_size,
            });
        }
        if (msg.document) {
            attachments.push({
                type: 'file',
                url: `tg://${msg.document.file_id}`,
                filename: msg.document.file_name,
                mimeType: msg.document.mime_type,
                size: msg.document.file_size,
            });
        }
        if (msg.voice) {
            attachments.push({
                type: 'audio',
                url: `tg://${msg.voice.file_id}`,
                mimeType: msg.voice.mime_type,
                size: msg.voice.file_size,
            });
        }
        if (msg.audio) {
            attachments.push({
                type: 'audio',
                url: `tg://${msg.audio.file_id}`,
                filename: msg.audio.file_name,
                mimeType: msg.audio.mime_type,
                size: msg.audio.file_size,
            });
        }
        if (msg.video) {
            attachments.push({
                type: 'video',
                url: `tg://${msg.video.file_id}`,
                mimeType: msg.video.mime_type,
                size: msg.video.file_size,
            });
        }

        // Emit normalized message
        const incoming: IncomingMessage = {
            id: randomUUID(),
            channel: 'telegram',
            userId,
            workspaceId: this.config.defaultWorkspaceId,
            conversationId: `tg-${msg.chat.id}`,   // 1 Telegram chat = 1 conversation
            content: text,
            attachments: attachments.length ? attachments : undefined,
            metadata: {
                channelUserId: String(from.id),
                channelMessageId: String(msg.message_id),
                timestamp: new Date(msg.date * 1000),
                telegramChatId: msg.chat.id,
                telegramUsername: from.username,
            },
        };

        this.emit('message', incoming);
    }

    // ── Commands ───────────────────────────────────────────────────

    private async handleCommand(msg: TelegramMessage, text: string): Promise<void> {
        const chatId = msg.chat.id;
        const [command] = text.split(/\s+/);

        switch (command) {
            case '/start':
                await this.api('sendMessage', {
                    chat_id: chatId,
                    text:
                        '🪶 *Welcome to Kestrel!*\n\n' +
                        'I\'m your AI assistant. Just send me a message to start chatting.\n\n' +
                        '*Commands:*\n' +
                        '/help — Show this help\n' +
                        '/workspace — Show workspace info\n' +
                        '/new — Start a new conversation',
                    parse_mode: 'Markdown',
                });
                break;

            case '/help':
                await this.api('sendMessage', {
                    chat_id: chatId,
                    text:
                        '*Available Commands:*\n' +
                        '/start — Welcome message\n' +
                        '/help — This help text\n' +
                        '/workspace — Current workspace\n' +
                        '/new — Start a new conversation\n\n' +
                        'Or just send me a message!',
                    parse_mode: 'Markdown',
                });
                break;

            case '/workspace':
                await this.api('sendMessage', {
                    chat_id: chatId,
                    text: `Current workspace: \`${this.config.defaultWorkspaceId}\``,
                    parse_mode: 'Markdown',
                });
                break;

            case '/new':
                // Clear per-chat conversation mapping would happen here
                await this.api('sendMessage', {
                    chat_id: chatId,
                    text: '✨ New conversation started! Send your first message.',
                    parse_mode: 'Markdown',
                });
                break;

            default:
                await this.api('sendMessage', {
                    chat_id: chatId,
                    text: `Unknown command: ${command}. Use /help for available commands.`,
                });
        }
    }

    // ── Callback Queries ───────────────────────────────────────────

    private async handleCallbackQuery(query: {
        id: string;
        from: TelegramUser;
        message?: TelegramMessage;
        data?: string;
    }): Promise<void> {
        // Acknowledge the callback
        await this.api('answerCallbackQuery', { callback_query_id: query.id });

        if (!query.data || !query.message) return;

        // Treat callback data as a message
        const userId = this.resolveUserId(query.from, query.message.chat.id);
        const incoming: IncomingMessage = {
            id: randomUUID(),
            channel: 'telegram',
            userId,
            workspaceId: this.config.defaultWorkspaceId,
            conversationId: `tg-${query.message.chat.id}`,
            content: query.data,
            metadata: {
                channelUserId: String(query.from.id),
                channelMessageId: String(query.message.message_id),
                timestamp: new Date(),
                isCallbackQuery: true,
            },
        };

        this.emit('message', incoming);
    }

    // ── Polling ────────────────────────────────────────────────────

    private startPolling(): void {
        this.pollingActive = true;
        this.poll();
    }

    private async poll(): Promise<void> {
        if (!this.pollingActive) return;

        try {
            const updates: TelegramUpdate[] = await this.api('getUpdates', {
                offset: this.pollingOffset,
                timeout: 30,
                allowed_updates: JSON.stringify(['message', 'callback_query']),
            });

            for (const update of updates) {
                this.pollingOffset = update.update_id + 1;
                await this.processUpdate(update);
            }
        } catch (err) {
            logger.error('Telegram polling error', { error: (err as Error).message });
        }

        // Schedule next poll
        this.pollingTimer = setTimeout(() => this.poll(), this.pollingActive ? 100 : 5000);
    }

    // ── User Mapping ───────────────────────────────────────────────

    private resolveUserId(from: TelegramUser, chatId: number): string {
        // Deterministic user ID from Telegram user
        const existing = this.userIdMap.get(chatId);
        if (existing) return existing;

        const userId = `tg-${from.id}`;
        this.userIdMap.set(chatId, userId);
        this.chatIdMap.set(userId, chatId);
        return userId;
    }

    // ── Telegram API ───────────────────────────────────────────────

    private async api(method: string, params?: Record<string, any>): Promise<any> {
        const url = `${this.apiBase}/${method}`;

        const res = await fetch(url, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: params ? JSON.stringify(params) : undefined,
        });

        const data = await res.json() as { ok: boolean; result: any; description?: string };

        if (!data.ok) {
            throw new Error(`Telegram API error: ${data.description || 'Unknown error'} (${method})`);
        }

        return data.result;
    }
}
