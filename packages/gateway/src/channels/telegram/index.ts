import { randomUUID, createHash } from 'crypto';
import {
    BaseChannelAdapter,
    ChannelType,
    IncomingMessage,
    OutgoingMessage,
    Attachment,
} from '../base';
import { logger } from '../../utils/logger';
import { TelegramConfig, TelegramUpdate, TelegramMessage, TelegramUser, TelegramChat } from './types';
import { processUpdate, handleTaskRequest, handleCommand, handleApproval, sendApprovalRequest, sendTaskProgress, handleCallbackQuery } from './handlers';


// ── Telegram Adapter ───────────────────────────────────────────────

/**
 * Full-featured Telegram Bot adapter for Kestrel.
 *
 * Features:
 *   ✅ Chat mode — conversational AI via Telegram
 *   ✅ Task mode — launch autonomous agent tasks with !goal or /task
 *   ✅ Extended commands — /status, /tasks, /cancel, /approve, /model, /help
 *   ✅ Typing indicators — shows "typing..." while processing
 *   ✅ Progress updates — sends tool call and thinking updates for tasks
 *   ✅ Smart chunking — splits long responses into multiple messages
 *   ✅ Inline keyboards — for approvals and task actions
 *   ✅ File handling — photos, documents, voice, audio, video
 *   ✅ Access control — optional allowlist by Telegram user ID
 *   ✅ Both webhook and polling modes
 */
export class TelegramAdapter extends BaseChannelAdapter {
    readonly channelType: ChannelType = 'telegram';

    public readonly apiBase: string;
    public pollingActive = false;
    public pollingOffset = 0;
    public pollingTimer?: NodeJS.Timeout;

    // Telegram chat ID → userId mapping
    public chatIdMap = new Map<string, number>();   // kestrelUserId → telegramChatId
    public userIdMap = new Map<number, string>();    // telegramChatId → kestrelUserId

    // Active typing indicators per chat
    public typingIntervals = new Map<number, NodeJS.Timeout>();

    // Per-chat conversation mode
    public chatModes = new Map<number, 'chat' | 'task'>();

    // Pending approval requests
    public pendingApprovals = new Map<string, { taskId: string; chatId: number; userId: string }>();

    // Bot identity (populated after connect)
    private _botId: number | undefined;
    private _botUsername: string | undefined;

    get botInfo(): { id: number; username: string } | undefined {
        if (!this._botId || !this._botUsername) return undefined;
        return { id: this._botId, username: this._botUsername };
    }

    constructor(public config: TelegramConfig) {
        super();
        this.apiBase = `https://api.telegram.org/bot${config.botToken}`;
    }

    // ── Lifecycle ──────────────────────────────────────────────────

    async connect(): Promise<void> {
        this.setStatus('connecting');

        // Validate token and capture bot identity
        const me = await this.api('getMe');
        this._botId = me.id;
        this._botUsername = me.username;
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

        // Clear all typing indicators
        for (const [, interval] of this.typingIntervals) {
            clearInterval(interval);
        }
        this.typingIntervals.clear();

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

        await this.sendToChat(chatId, message);
    }

    /**
     * Send a message to a specific Telegram chat.
     * Handles chunking for long messages (4096 char Telegram limit).
     */
    private async sendToChat(chatId: number, message: OutgoingMessage): Promise<void> {
        // Stop typing indicator when sending
        this.stopTyping(chatId);

        const content = message.content;

        // Chunk long messages
        const chunks = this.chunkMessage(content, 4000);
        for (let i = 0; i < chunks.length; i++) {
            const params: Record<string, any> = {
                chat_id: chatId,
                text: chunks[i],
                parse_mode: 'Markdown',
                disable_web_page_preview: true,
            };

            // Only add buttons to the last chunk
            if (i === chunks.length - 1 && message.options?.buttons?.length) {
                params.reply_markup = JSON.stringify({
                    inline_keyboard: [
                        message.options.buttons.map((btn) => ({
                            text: btn.label,
                            callback_data: btn.action,
                        })),
                    ],
                });
            }

            try {
                await this.api('sendMessage', params);
            } catch (err) {
                // Retry without Markdown if parsing fails
                logger.warn('Telegram Markdown parse failed, retrying as plain text');
                delete params.parse_mode;
                await this.api('sendMessage', params);
            }
        }

        // Send attachments as separate messages
        if (message.attachments?.length) {
            for (const att of message.attachments) {
                await this.sendAttachment(chatId, att);
            }
        }
    }

    private chunkMessage(text: string, maxLength: number): string[] {
        if (text.length <= maxLength) return [text];

        const chunks: string[] = [];
        let remaining = text;

        while (remaining.length > 0) {
            if (remaining.length <= maxLength) {
                chunks.push(remaining);
                break;
            }

            // Try to split at a natural boundary
            let splitAt = remaining.lastIndexOf('\n', maxLength);
            if (splitAt < maxLength / 2) splitAt = remaining.lastIndexOf('. ', maxLength);
            if (splitAt < maxLength / 2) splitAt = remaining.lastIndexOf(' ', maxLength);
            if (splitAt < maxLength / 2) splitAt = maxLength;

            chunks.push(remaining.substring(0, splitAt));
            remaining = remaining.substring(splitAt).trimStart();
        }

        return chunks;
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
        // Truncate very long messages — we handle chunking in sendToChat
        return message;
    }

    // ── Attachment Processing ──────────────────────────────────────

    async handleAttachment(attachment: Attachment): Promise<Attachment> {
        if (attachment.url.startsWith('tg://')) {
            const fileId = attachment.url.replace('tg://', '');
            const file = await this.api('getFile', { file_id: fileId });
            attachment.url = `https://api.telegram.org/file/bot${this.config.botToken}/${file.file_path}`;
        }
        return attachment;
    }

    // ── Typing Indicators ──────────────────────────────────────────

    /**
     * Show "typing..." indicator in a chat.
     * Automatically refreshes every 4 seconds (Telegram's indicator lasts 5s).
     */
    public startTyping(chatId: number): void {
        // Send immediately
        this.api('sendChatAction', { chat_id: chatId, action: 'typing' }).catch(() => { });

        // Refresh every 4 seconds
        if (!this.typingIntervals.has(chatId)) {
            const interval = setInterval(() => {
                this.api('sendChatAction', { chat_id: chatId, action: 'typing' }).catch(() => { });
            }, 4000);
            this.typingIntervals.set(chatId, interval);
        }
    }

    public stopTyping(chatId: number): void {
        const interval = this.typingIntervals.get(chatId);
        if (interval) {
            clearInterval(interval);
            this.typingIntervals.delete(chatId);
        }
    }

    // ── Access Control ─────────────────────────────────────────────

    public isAllowed(userId: number): boolean {
        if (!this.config.allowedUserIds?.length) return true;
        return this.config.allowedUserIds.includes(userId);
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
                await processUpdate(this, update);
            }
        } catch (err) {
            logger.error('Telegram polling error', { error: (err as Error).message });
        }

        // Schedule next poll
        this.pollingTimer = setTimeout(() => this.poll(), this.pollingActive ? 100 : 5000);
    }

    // ── User Mapping ───────────────────────────────────────────────

    public resolveUserId(from: TelegramUser, chatId: number): string {
        const existing = this.userIdMap.get(chatId);
        if (existing) return existing;

        // Generate a deterministic UUID from the Telegram user ID
        // Brain requires valid UUIDs — this ensures the same TG user
        // always maps to the same Kestrel user ID.
        const userId = this.deterministicUUID(`telegram-user:${from.id}`);

        this.userIdMap.set(chatId, userId);
        this.chatIdMap.set(userId, chatId);
        return userId;
    }

    /**
     * Generate a deterministic UUID from an arbitrary seed string.
     */
    private deterministicUUID(seed: string): string {
        const hash = createHash('sha256').update(seed).digest('hex');
        return [
            hash.substring(0, 8),
            hash.substring(8, 12),
            '4' + hash.substring(13, 16),
            ((parseInt(hash[16], 16) & 0x3) | 0x8).toString(16) + hash.substring(17, 20),
            hash.substring(20, 32),
        ].join('-');
    }

    /**
     * Generate a deterministic conversation UUID from a chat ID.
     */
    public resolveConversationId(chatId: number, suffix?: string): string {
        const seed = suffix
            ? `telegram-conv:${chatId}:${suffix}`
            : `telegram-conv:${chatId}`;
        return this.deterministicUUID(seed);
    }

    // ── Helpers ────────────────────────────────────────────────────

    public escapeMarkdown(text: string): string {
        // Escape characters that break Telegram Markdown
        return text.replace(/([_*\[\]()~`>#+\-=|{}.!\\])/g, '\\$1');
    }

    // ── Telegram API ───────────────────────────────────────────────

    public async api(method: string, params?: Record<string, any>): Promise<any> {
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
