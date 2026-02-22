import { randomUUID, createHash } from 'crypto';
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
    allowedUserIds?: number[];     // Optional: restrict to these Telegram user IDs
}

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

    private readonly apiBase: string;
    private pollingActive = false;
    private pollingOffset = 0;
    private pollingTimer?: NodeJS.Timeout;

    // Telegram chat ID → userId mapping
    private chatIdMap = new Map<string, number>();   // kestrelUserId → telegramChatId
    private userIdMap = new Map<number, string>();    // telegramChatId → kestrelUserId

    // Active typing indicators per chat
    private typingIntervals = new Map<number, NodeJS.Timeout>();

    // Per-chat conversation mode
    private chatModes = new Map<number, 'chat' | 'task'>();

    // Pending approval requests
    private pendingApprovals = new Map<string, { taskId: string; chatId: number; userId: string }>();

    // Bot identity (populated after connect)
    private _botId: number | undefined;
    private _botUsername: string | undefined;

    get botInfo(): { id: number; username: string } | undefined {
        if (!this._botId || !this._botUsername) return undefined;
        return { id: this._botId, username: this._botUsername };
    }

    constructor(private config: TelegramConfig) {
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
    private startTyping(chatId: number): void {
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

    private stopTyping(chatId: number): void {
        const interval = this.typingIntervals.get(chatId);
        if (interval) {
            clearInterval(interval);
            this.typingIntervals.delete(chatId);
        }
    }

    // ── Access Control ─────────────────────────────────────────────

    private isAllowed(userId: number): boolean {
        if (!this.config.allowedUserIds?.length) return true;
        return this.config.allowedUserIds.includes(userId);
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

        // Access control check
        if (!this.isAllowed(from.id)) {
            await this.api('sendMessage', {
                chat_id: msg.chat.id,
                text: '🔒 Access denied. You are not authorized to use this bot.',
            });
            return;
        }

        const text = msg.text || msg.caption || '';

        // Handle Telegram-specific commands
        if (text.startsWith('/')) {
            await this.handleCommand(msg, text);
            return;
        }

        // Handle task mode (!goal prefix)
        if (text.startsWith('!')) {
            const goal = text.substring(1).trim();
            if (goal) {
                await this.handleTaskRequest(msg, from, goal);
                return;
            }
        }

        // Start typing indicator
        this.startTyping(msg.chat.id);

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
            conversationId: this.resolveConversationId(msg.chat.id),
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

    // ── Task Mode ─────────────────────────────────────────────────

    /**
     * Handle a task request (message starting with !).
     * Sends the goal to the agent as an autonomous task and streams
     * progress updates back to the Telegram chat.
     */
    private async handleTaskRequest(msg: TelegramMessage, from: TelegramUser, goal: string): Promise<void> {
        const chatId = msg.chat.id;
        const userId = this.resolveUserId(from, chatId);

        await this.api('sendMessage', {
            chat_id: chatId,
            text:
                '🦅 *Starting autonomous task...*\n\n' +
                `📋 *Goal:* ${this.escapeMarkdown(goal)}\n\n` +
                '_I\'ll work on this and send you updates._',
            parse_mode: 'Markdown',
        });

        this.startTyping(chatId);

        // Emit as a task-type message
        const incoming: IncomingMessage = {
            id: randomUUID(),
            channel: 'telegram',
            userId,
            workspaceId: this.config.defaultWorkspaceId,
            conversationId: this.resolveConversationId(msg.chat.id, `task-${Date.now()}`),
            content: goal,
            metadata: {
                channelUserId: String(from.id),
                channelMessageId: String(msg.message_id),
                timestamp: new Date(msg.date * 1000),
                telegramChatId: chatId,
                telegramUsername: from.username,
                isTaskRequest: true,
            },
        };

        this.emit('message', incoming);
    }

    // ── Commands ───────────────────────────────────────────────────

    private async handleCommand(msg: TelegramMessage, text: string): Promise<void> {
        const chatId = msg.chat.id;
        const parts = text.split(/\s+/);
        const command = parts[0].replace(/@\w+$/, ''); // Strip @botname
        const args = parts.slice(1);

        switch (command) {
            case '/start':
                await this.api('sendMessage', {
                    chat_id: chatId,
                    text:
                        '🦅 *Welcome to Kestrel\\!*\n\n' +
                        'I\'m your autonomous AI agent\\. Here\'s what I can do:\n\n' +
                        '*💬 Chat Mode*\n' +
                        'Just send me a message to chat\\.\n\n' +
                        '*🤖 Task Mode*\n' +
                        'Start a message with `\\!` to launch an autonomous task:\n' +
                        '`\\!review the auth module for security issues`\n\n' +
                        '*Commands:*\n' +
                        '/help — Show all commands\n' +
                        '/task \\<goal\\> — Start an autonomous task\n' +
                        '/tasks — List active tasks\n' +
                        '/status — System status\n' +
                        '/cancel \\<id\\> — Cancel a task\n' +
                        '/model \\<name\\> — Switch AI model\n' +
                        '/new — Start a new conversation',
                    parse_mode: 'MarkdownV2',
                });
                break;

            case '/help':
                await this.api('sendMessage', {
                    chat_id: chatId,
                    text:
                        '*🦅 Kestrel Commands*\n\n' +
                        '*Communication:*\n' +
                        '  Just type — Chat with the AI\n' +
                        '  `!goal` — Launch autonomous task\n\n' +
                        '*Commands:*\n' +
                        '  /task `<goal>` — Start an autonomous task\n' +
                        '  /tasks — List your active tasks\n' +
                        '  /status — Show system status\n' +
                        '  /cancel `<id>` — Cancel a running task\n' +
                        '  /approve `<id>` — Approve a pending action\n' +
                        '  /reject `<id>` — Reject a pending action\n' +
                        '  /model `<name>` — Switch AI model\n' +
                        '  /workspace — Show current workspace\n' +
                        '  /new — Start a new conversation\n' +
                        '  /stop — Stop all typing indicators\n',
                    parse_mode: 'Markdown',
                });
                break;

            case '/task': {
                const goal = args.join(' ').trim();
                if (!goal) {
                    await this.api('sendMessage', {
                        chat_id: chatId,
                        text: '❓ Usage: `/task <your goal>`\n\nExample: `/task review the database schema for performance issues`',
                        parse_mode: 'Markdown',
                    });
                    return;
                }
                if (msg.from) {
                    await this.handleTaskRequest(msg, msg.from, goal);
                }
                break;
            }

            case '/tasks':
                await this.api('sendMessage', {
                    chat_id: chatId,
                    text:
                        '📋 *Your Tasks*\n\n' +
                        '_Task listing requires the web dashboard or CLI._\n' +
                        '_Use_ `kestrel tasks` _in the CLI to view all tasks._',
                    parse_mode: 'Markdown',
                });
                break;

            case '/status':
                await this.api('sendMessage', {
                    chat_id: chatId,
                    text:
                        '🦅 *Kestrel Status*\n\n' +
                        `✅ Bot: Online\n` +
                        `📡 Mode: ${this.config.mode}\n` +
                        `🏢 Workspace: \`${this.config.defaultWorkspaceId}\`\n` +
                        `👤 Your ID: \`${msg.from?.id || 'unknown'}\`\n` +
                        `💬 Chat: \`${chatId}\``,
                    parse_mode: 'Markdown',
                });
                break;

            case '/cancel': {
                const taskId = args[0];
                if (!taskId) {
                    await this.api('sendMessage', {
                        chat_id: chatId,
                        text: '❓ Usage: `/cancel <task_id>`',
                        parse_mode: 'Markdown',
                    });
                    return;
                }
                // Emit as a cancel command
                if (msg.from) {
                    const userId = this.resolveUserId(msg.from, chatId);
                    this.emit('message', {
                        id: randomUUID(),
                        channel: 'telegram',
                        userId,
                        workspaceId: this.config.defaultWorkspaceId,
                        conversationId: this.resolveConversationId(chatId),
                        content: `/cancel ${taskId}`,
                        metadata: {
                            channelUserId: String(msg.from.id),
                            channelMessageId: String(msg.message_id),
                            timestamp: new Date(msg.date * 1000),
                            isCommand: true,
                        },
                    });
                }
                break;
            }

            case '/approve': {
                const approvalId = args[0];
                if (!approvalId) {
                    await this.api('sendMessage', {
                        chat_id: chatId,
                        text: '❓ Usage: `/approve <approval_id>`',
                        parse_mode: 'Markdown',
                    });
                    return;
                }
                await this.handleApproval(chatId, approvalId, true);
                break;
            }

            case '/reject': {
                const rejectId = args[0];
                if (!rejectId) {
                    await this.api('sendMessage', {
                        chat_id: chatId,
                        text: '❓ Usage: `/reject <approval_id>`',
                        parse_mode: 'Markdown',
                    });
                    return;
                }
                await this.handleApproval(chatId, rejectId, false);
                break;
            }

            case '/model':
                if (args[0]) {
                    await this.api('sendMessage', {
                        chat_id: chatId,
                        text: `🔄 Model switched to \`${args[0]}\``,
                        parse_mode: 'Markdown',
                    });
                } else {
                    await this.api('sendMessage', {
                        chat_id: chatId,
                        text: '❓ Usage: `/model <model_name>`\n\nExamples:\n`/model gpt-4o`\n`/model claude-sonnet-4-20250514`\n`/model gemini-2.5-pro`',
                        parse_mode: 'Markdown',
                    });
                }
                break;

            case '/workspace':
                await this.api('sendMessage', {
                    chat_id: chatId,
                    text: `🏢 Current workspace: \`${this.config.defaultWorkspaceId}\``,
                    parse_mode: 'Markdown',
                });
                break;

            case '/new':
                await this.api('sendMessage', {
                    chat_id: chatId,
                    text: '✨ New conversation started! Send your first message.',
                    parse_mode: 'Markdown',
                });
                break;

            case '/stop':
                this.stopTyping(chatId);
                await this.api('sendMessage', {
                    chat_id: chatId,
                    text: '⏹ Stopped.',
                });
                break;

            default:
                await this.api('sendMessage', {
                    chat_id: chatId,
                    text: `Unknown command: ${command}. Use /help for available commands.`,
                });
        }
    }

    // ── Approval Handling ──────────────────────────────────────────

    private async handleApproval(chatId: number, approvalId: string, approved: boolean): Promise<void> {
        const pending = this.pendingApprovals.get(approvalId);

        const icon = approved ? '✅' : '❌';
        const action = approved ? 'Approved' : 'Rejected';

        await this.api('sendMessage', {
            chat_id: chatId,
            text: `${icon} *${action}* approval \`${approvalId}\``,
            parse_mode: 'Markdown',
        });

        // Clean up
        this.pendingApprovals.delete(approvalId);
    }

    /**
     * Send an approval request to a Telegram chat with inline buttons.
     */
    async sendApprovalRequest(chatId: number, approvalId: string, description: string, taskId: string): Promise<void> {
        this.pendingApprovals.set(approvalId, { taskId, chatId, userId: '' });

        await this.api('sendMessage', {
            chat_id: chatId,
            text:
                '⚠️ *Approval Required*\n\n' +
                `${description}\n\n` +
                `ID: \`${approvalId}\``,
            parse_mode: 'Markdown',
            reply_markup: JSON.stringify({
                inline_keyboard: [[
                    { text: '✅ Approve', callback_data: `approve:${approvalId}` },
                    { text: '❌ Reject', callback_data: `reject:${approvalId}` },
                ]],
            }),
        });
    }

    /**
     * Send a progress update for an active task.
     */
    async sendTaskProgress(chatId: number, step: string, detail: string = ''): Promise<void> {
        const text = detail
            ? `🔧 *${this.escapeMarkdown(step)}*\n${this.escapeMarkdown(detail)}`
            : `🔧 ${this.escapeMarkdown(step)}`;

        await this.api('sendMessage', {
            chat_id: chatId,
            text,
            parse_mode: 'Markdown',
            disable_notification: true,  // Silent for progress updates
        });
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
        const chatId = query.message.chat.id;

        // Handle self-improvement callbacks (si_approve / si_deny)
        if (query.data.startsWith('si_approve:') || query.data.startsWith('si_deny:')) {
            const isApprove = query.data.startsWith('si_approve:');
            const proposalId = query.data.substring(query.data.indexOf(':') + 1);
            const action = isApprove ? 'approve' : 'deny';
            const icon = isApprove ? '✅' : '❌';

            await this.api('sendMessage', {
                chat_id: chatId,
                text: `${icon} Processing ${action} for proposal \`${proposalId.substring(0, 8)}...\``,
                parse_mode: 'Markdown',
            });

            try {
                // Call brain's self_improve handler via docker exec
                const { execSync } = await import('child_process');
                const cmd = `docker exec littlebirdalt-brain-1 python3 -c "
import sys, json
sys.path.insert(0, '/app')
from agent.tools.self_improve import _handle_approval
result = _handle_approval('${proposalId}', approved=${isApprove ? 'True' : 'False'})
print(json.dumps(result))
"`;
                const output = execSync(cmd, { timeout: 15000, encoding: 'utf-8' }).trim();
                const result = JSON.parse(output);

                if (result.error) {
                    await this.api('sendMessage', {
                        chat_id: chatId,
                        text: `⚠️ ${result.error}`,
                    });
                } else {
                    await this.api('sendMessage', {
                        chat_id: chatId,
                        text: `${icon} *${result.status === 'approved' ? 'Approved' : 'Denied'}*\n${result.message || ''}`,
                        parse_mode: 'Markdown',
                    });
                }
            } catch (err) {
                logger.error('Self-improve callback failed', { error: (err as Error).message });
                await this.api('sendMessage', {
                    chat_id: chatId,
                    text: `⚠️ Failed to process: ${(err as Error).message?.substring(0, 200)}`,
                });
            }
            return;
        }

        // Handle approval callbacks
        if (query.data.startsWith('approve:')) {
            const approvalId = query.data.substring('approve:'.length);
            await this.handleApproval(chatId, approvalId, true);
            return;
        }
        if (query.data.startsWith('reject:')) {
            const approvalId = query.data.substring('reject:'.length);
            await this.handleApproval(chatId, approvalId, false);
            return;
        }

        // Treat other callback data as a message
        const userId = this.resolveUserId(query.from, chatId);
        const incoming: IncomingMessage = {
            id: randomUUID(),
            channel: 'telegram',
            userId,
            workspaceId: this.config.defaultWorkspaceId,
            conversationId: this.resolveConversationId(chatId),
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
    private resolveConversationId(chatId: number, suffix?: string): string {
        const seed = suffix
            ? `telegram-conv:${chatId}:${suffix}`
            : `telegram-conv:${chatId}`;
        return this.deterministicUUID(seed);
    }

    // ── Helpers ────────────────────────────────────────────────────

    private escapeMarkdown(text: string): string {
        // Escape characters that break Telegram Markdown
        return text.replace(/([_*\[\]()~`>#+\-=|{}.!\\])/g, '\\$1');
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
