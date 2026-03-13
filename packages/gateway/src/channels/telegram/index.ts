import { randomUUID, createHash } from 'crypto';
import { createReadStream, existsSync } from 'fs';
import path from 'path';
import {
    BaseChannelAdapter,
    ChannelType,
    IncomingMessage,
    OutgoingMessage,
    Attachment,
    StreamHandle,
    ToolActivity,
} from '../base';
import { logger } from '../../utils/logger';
import {
    TelegramConfig,
    TelegramUpdate,
    TelegramMessage,
    TelegramUser,
    TelegramChat,
} from './types';
import {
    processUpdate,
    handleTaskRequest,
    handleCommand,
    handleApproval,
    sendApprovalRequest,
    sendTaskProgress,
    handleCallbackQuery,
} from './handlers';
import type { ChannelSessionStore } from '../store';
import type { TelegramChannelStateRecord } from '../store';
import { getMediaDir } from '../../utils/paths';

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
    private readonly mediaDir = getMediaDir();
    private persistTimer?: NodeJS.Timeout;

    public readonly apiBase: string;
    public pollingActive = false;
    public pollingOffset = 0;
    public pollingTimer?: NodeJS.Timeout;

    // Telegram chat ID → userId mapping
    public chatIdMap = new Map<string, number>(); // kestrelUserId → telegramChatId
    public userIdMap = new Map<number, string>(); // telegramChatId → kestrelUserId

    // Active typing indicators per chat
    public typingIntervals = new Map<number, NodeJS.Timeout>();
    public lastHeartbeatAt = new Map<number, number>();

    // Per-chat conversation mode
    public chatModes = new Map<number, 'chat' | 'task'>();

    // Pending approval requests
    public pendingApprovals = new Map<
        string,
        { taskId: string; chatId: number; userId: string; threadId?: number }
    >();

    // Maps kestrelUserId → current message_thread_id (undefined for non-forum chats)
    public userThreadMap = new Map<string, number | undefined>();

    // Bot identity (populated after connect)
    private _botId: number | undefined;
    private _botUsername: string | undefined;

    // Callback to resolve approvals in Brain
    private approvalHandler?: (
        approvalId: string,
        userId: string,
        approved: boolean,
    ) => Promise<{ success: boolean; error?: string }>;
    private pendingApprovalsLookupHandler?: (
        userId: string,
        workspaceId: string,
    ) => Promise<Array<{ approval_id: string }>>;
    private cancelStreamHandler?: (userId: string) => void;
    private readonly sessionStore?: ChannelSessionStore;

    public setApprovalHandler(
        handler: (
            approvalId: string,
            userId: string,
            approved: boolean,
        ) => Promise<{ success: boolean; error?: string }>,
    ): void {
        this.approvalHandler = handler;
    }

    public setPendingApprovalsLookupHandler(
        handler: (userId: string, workspaceId: string) => Promise<Array<{ approval_id: string }>>,
    ): void {
        this.pendingApprovalsLookupHandler = handler;
    }

    public setCancelStreamHandler(handler: (userId: string) => void): void {
        this.cancelStreamHandler = handler;
    }

    public cancelActiveStream(userId: string): void {
        if (this.cancelStreamHandler) {
            this.cancelStreamHandler(userId);
        }
    }

    public async resolvePendingApproval(
        approvalId: string,
        approved: boolean,
        actorUserId?: string,
    ): Promise<{ success: boolean; error?: string }> {
        if (!this.approvalHandler) {
            return { success: false, error: 'Approval handler is not configured on the gateway.' };
        }

        const pending = this.pendingApprovals.get(approvalId);

        // Normal path: resolve against tracked pending request.
        if (pending) {
            if (actorUserId && pending.userId && pending.userId !== actorUserId) {
                return { success: false, error: 'This approval request belongs to another user.' };
            }

            const result = await this.approvalHandler(approvalId, pending.userId, approved);
            if (result.success) {
                this.pendingApprovals.delete(approvalId);
            }
            return result;
        }

        // Fallback path: allow command-driven approval after adapter restart
        // when in-memory pending map is empty, as long as we know actor user.
        if (!actorUserId) {
            return { success: false, error: 'Approval ID not found or already resolved.' };
        }

        return this.approvalHandler(approvalId, actorUserId, approved);
    }

    public async listPendingApprovalsForUser(
        userId: string,
    ): Promise<Array<{ approval_id: string }>> {
        if (!this.pendingApprovalsLookupHandler) {
            return [];
        }

        return this.pendingApprovalsLookupHandler(userId, this.config.defaultWorkspaceId);
    }

    public async sendApprovalRequestForUser(
        userId: string,
        approvalId: string,
        description: string,
        taskId: string,
    ): Promise<void> {
        const existing = this.pendingApprovals.get(approvalId);
        if (existing) {
            return;
        }

        const chatId = this.chatIdMap.get(userId);
        if (!chatId) {
            logger.warn('Cannot send Telegram approval request — no chat ID for user', {
                userId,
                approvalId,
            });
            return;
        }

        const threadId = this.userThreadMap.get(userId);
        await sendApprovalRequest(this, chatId, approvalId, description, taskId, userId, threadId);
    }

    get botInfo(): { id: number; username: string } | undefined {
        if (!this._botId || !this._botUsername) return undefined;
        return { id: this._botId, username: this._botUsername };
    }

    constructor(
        public config: TelegramConfig,
        options: { sessionStore?: ChannelSessionStore } = {},
    ) {
        super();
        this.apiBase = `https://api.telegram.org/bot${config.botToken}`;
        this.sessionStore = options.sessionStore;
    }

    // ── Lifecycle ──────────────────────────────────────────────────

    async connect(): Promise<void> {
        this.setStatus('connecting');

        // Validate token and capture bot identity
        const me = await this.api('getMe');
        this._botId = me.id;
        this._botUsername = me.username;
        logger.info(`Telegram bot connected: @${me.username} (${me.id})`);
        await this.restoreSessionState();

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
        if (this.persistTimer) clearTimeout(this.persistTimer);

        // Clear all typing indicators
        for (const [, interval] of this.typingIntervals) {
            clearInterval(interval);
        }
        this.typingIntervals.clear();

        if (this.config.mode === 'webhook') {
            await this.api('deleteWebhook').catch(() => {
                /* best-effort */
            });
        }

        await this.persistSessionState();
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

        const threadId = this.userThreadMap.get(userId);
        await this.sendToChat(chatId, message, threadId);
    }

    /**
     * Send a message to a specific Telegram chat.
     * Handles chunking for long messages (4096 char Telegram limit).
     */
    private async sendToChat(
        chatId: number,
        message: OutgoingMessage,
        threadId?: number,
    ): Promise<void> {
        // Stop typing indicator when sending
        this.stopTyping(chatId);

        const content = this.sanitizeForTelegram(message.content);

        // Chunk long messages
        const chunks = this.chunkMessage(content, 4000);
        for (let i = 0; i < chunks.length; i++) {
            const params: Record<string, any> = {
                chat_id: chatId,
                text: chunks[i],
                parse_mode: 'Markdown',
                disable_web_page_preview: true,
            };

            // Route to the correct forum topic if applicable
            if (threadId !== undefined) {
                params.message_thread_id = threadId;
            }

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
                await this.sendAttachment(chatId, att, threadId);
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

    private async sendAttachment(
        chatId: number,
        attachment: Attachment,
        threadId?: number,
    ): Promise<void> {
        const thread = threadId !== undefined ? { message_thread_id: threadId } : {};
        switch (attachment.type) {
            case 'image':
                await this.api('sendPhoto', { chat_id: chatId, photo: attachment.url, ...thread });
                break;
            case 'audio':
                await this.api('sendAudio', { chat_id: chatId, audio: attachment.url, ...thread });
                break;
            case 'video':
                await this.api('sendVideo', { chat_id: chatId, video: attachment.url, ...thread });
                break;
            case 'file':
                await this.api('sendDocument', {
                    chat_id: chatId,
                    document: attachment.url,
                    ...thread,
                });
                break;
        }
    }

    // ── Live Streaming Interface ────────────────────────────────────
    // These methods are detected by registry.ts to enable progressive
    // response updates instead of waiting for the full response.

    /**
     * Send a "Thinking..." placeholder and return a handle for
     * subsequent edits. The handle's chatContext carries the
     * Telegram chat ID so we can edit the right message.
     */
    async sendStreamStart(userId: string, meta: { conversationId: string }): Promise<StreamHandle> {
        const chatId = this.chatIdMap.get(userId);
        if (!chatId) throw new Error(`No chat ID mapped for user ${userId}`);

        const threadId = this.userThreadMap.get(userId);
        this.lastHeartbeatAt.delete(chatId);

        // Send placeholder
        const params: Record<string, any> = { chat_id: chatId, text: '🤔 Thinking...' };
        if (threadId !== undefined) params.message_thread_id = threadId;
        const result = await this.api('sendMessage', params);

        // Start typing indicator (continues while streaming)
        this.startTyping(chatId, threadId);

        return {
            messageId: String(result.message_id),
            chatContext: { chatId, threadId },
        };
    }

    /**
     * Edit the placeholder message with the latest accumulated content.
     * Called on a throttled interval (~1.5s) by the registry.
     */
    async sendStreamUpdate(handle: StreamHandle, accumulatedContent: string): Promise<void> {
        const chatId = handle.chatContext.chatId as number;
        const { textContent, mediaFiles } = this.extractMediaFromMarkdown(accumulatedContent);
        if (mediaFiles.length > 0 && !textContent.trim()) {
            return;
        }
        const contentToRender = textContent.trim() ? textContent : accumulatedContent;
        if (!contentToRender.trim()) {
            return;
        }
        // Append a blinking cursor to show it's still generating
        const sanitized = this.sanitizeForTelegram(contentToRender);
        const display = sanitized.length > 4000 ? sanitized.slice(-4000) + '▌' : sanitized + ' ▌';
        try {
            await this.api('editMessageText', {
                chat_id: chatId,
                message_id: Number(handle.messageId),
                text: display,
                parse_mode: 'Markdown',
                disable_web_page_preview: true,
            });
        } catch (err) {
            // Telegram returns "message is not modified" if text is identical — ignore
            const msg = (err as Error).message || '';
            if (!msg.includes('message is not modified')) {
                // Retry without Markdown (some partial content may break parsing)
                try {
                    await this.api('editMessageText', {
                        chat_id: chatId,
                        message_id: Number(handle.messageId),
                        text: display.replace(/[_*[\]()~`>#+\-=|{}.!\\]/g, ''),
                    });
                } catch {
                    /* best effort */
                }
            }
        }
    }

    /**
     * Extract image/video markdown patterns and return the media info
     * and the text content with media references stripped.
     */
    private extractMediaFromMarkdown(content: string): {
        textContent: string;
        mediaFiles: Array<{ alt: string; filePath: string; type: 'photo' | 'video' }>;
    } {
        const mediaFiles: Array<{ alt: string; filePath: string; type: 'photo' | 'video' }> = [];
        const videoExts = ['.mp4', '.webm', '.mov'];

        // Match ![alt](/media/filename)
        const mediaRegex = /!\[([^\]]*)\]\((\/media\/[^)]+)\)/g;
        const textContent = content
            .replace(mediaRegex, (_match, alt: string, url: string) => {
                // Map /media/filename to the configured shared media directory.
                const encodedName = url.replace('/media/', '');
                const filename = path.basename(decodeURIComponent(encodedName));
                const filePath = path.join(this.mediaDir, filename);
                const ext = path.extname(filename).toLowerCase();
                const type = videoExts.includes(ext) ? 'video' : 'photo';
                mediaFiles.push({ alt: alt || 'Generated media', filePath, type });
                return ''; // Strip the markdown from the text
            })
            .replace(/\n{3,}/g, '\n\n')
            .trim(); // Clean up leftover whitespace

        return { textContent, mediaFiles };
    }

    /**
     * Send a media file to Telegram via multipart form upload.
     * Falls back gracefully if the file doesn't exist.
     * Uses sendDocument for images (to avoid Telegram's photo compression)
     * and sendVideo for videos.
     */
    // Media files already sent for the current stream — prevents the same
    // image/video from being sent multiple times when both the brain and
    // gateway streaming pipeline independently deliver the file.
    private sentMedia = new Set<string>();

    private async sendMediaFile(
        chatId: number,
        media: { alt: string; filePath: string; type: 'photo' | 'video' },
        threadId?: number,
    ): Promise<void> {
        if (!existsSync(media.filePath)) {
            logger.warn('Media file not found for Telegram upload', { path: media.filePath });
            return;
        }

        // Deduplicate — skip if already sent in this stream lifecycle
        const dedupeKey = `${chatId}:${media.filePath}`;
        if (this.sentMedia.has(dedupeKey)) {
            logger.info('Media already sent, skipping duplicate', { path: media.filePath });
            return;
        }
        this.sentMedia.add(dedupeKey);

        // Use sendDocument for images to preserve full resolution (sendPhoto compresses heavily).
        // Use sendVideo for videos so Telegram can show an inline player.
        const method = media.type === 'video' ? 'sendVideo' : 'sendDocument';
        const fieldName = media.type === 'video' ? 'video' : 'document';
        const url = `${this.apiBase}/${method}`;

        try {
            const formData = new FormData();
            // Read file as a blob for upload
            const { readFileSync } = await import('fs');
            const fileBuffer = readFileSync(media.filePath);
            const ext = path.extname(media.filePath).toLowerCase();
            const mimeMap: Record<string, string> = {
                '.png': 'image/png',
                '.jpg': 'image/jpeg',
                '.jpeg': 'image/jpeg',
                '.webp': 'image/webp',
                '.gif': 'image/gif',
                '.mp4': 'video/mp4',
                '.webm': 'video/webm',
                '.mov': 'video/quicktime',
            };
            const mime = mimeMap[ext] || 'application/octet-stream';
            const blob = new Blob([fileBuffer], { type: mime });
            formData.append(fieldName, blob, path.basename(media.filePath));
            formData.append('chat_id', String(chatId));
            if (media.alt) formData.append('caption', media.alt);
            if (threadId !== undefined) formData.append('message_thread_id', String(threadId));

            const res = await fetch(url, { method: 'POST', body: formData });
            const data = (await res.json()) as { ok: boolean; description?: string };
            if (!data.ok) {
                logger.warn('Telegram media upload failed', {
                    error: data.description,
                    path: media.filePath,
                });
            }
        } catch (err) {
            logger.warn('Failed to send media to Telegram', {
                error: (err as Error).message,
                path: media.filePath,
            });
        }
    }

    /**
     * Finalize the streaming message with the complete content.
     * Removes the cursor indicator and applies full Markdown.
     * Extracts image/video markdown and sends as actual Telegram media.
     */
    async sendStreamEnd(handle: StreamHandle, finalContent: string): Promise<void> {
        const chatId = handle.chatContext.chatId as number;
        const threadId = handle.chatContext.threadId as number | undefined;
        this.lastHeartbeatAt.delete(chatId);

        // Stop typing
        this.stopTyping(chatId);

        // Extract embedded media from markdown before sending text
        const { textContent, mediaFiles } = this.extractMediaFromMarkdown(finalContent);

        // Send the text content only; media markdown is uploaded separately.
        const contentToSend = textContent;
        const sanitizedFinal = this.sanitizeForTelegram(contentToSend);

        if (sanitizedFinal.length <= 4000 && sanitizedFinal.trim()) {
            try {
                await this.api('editMessageText', {
                    chat_id: chatId,
                    message_id: Number(handle.messageId),
                    text: sanitizedFinal,
                    parse_mode: 'Markdown',
                    disable_web_page_preview: true,
                });
            } catch {
                // Retry without markdown
                try {
                    await this.api('editMessageText', {
                        chat_id: chatId,
                        message_id: Number(handle.messageId),
                        text: contentToSend,
                    });
                } catch {
                    /* best effort */
                }
            }
        } else if (sanitizedFinal.trim()) {
            // Content too long — delete streaming msg and send as chunks
            try {
                await this.api('deleteMessage', {
                    chat_id: chatId,
                    message_id: Number(handle.messageId),
                });
            } catch {
                /* ignore */
            }

            await this.sendToChat(
                chatId,
                { conversationId: '', content: contentToSend, options: { markdown: true } },
                threadId,
            );
        } else {
            // No text content — delete the "Thinking..." placeholder
            try {
                await this.api('deleteMessage', {
                    chat_id: chatId,
                    message_id: Number(handle.messageId),
                });
            } catch {
                /* ignore */
            }
        }

        // Send media files as separate Telegram messages
        for (const media of mediaFiles) {
            await this.sendMediaFile(chatId, media, threadId);
        }
    }

    /**
     * Send tool activity as a separate short message.
     * Uses emoji indicators for different activity types.
     */
    async sendToolActivity(
        userId: string,
        handle: StreamHandle,
        activity: ToolActivity,
    ): Promise<void> {
        const chatId = handle.chatContext.chatId as number;
        const threadId = handle.chatContext.threadId as number | undefined;

        // Telegram already has a streaming placeholder plus a dedicated
        // approval message path. Emitting every internal status as a
        // standalone message creates noisy transcripts, so only keep a
        // minimal heartbeat when a request has been quiet for a while.
        if (activity.status !== 'thinking') {
            return;
        }

        const thinkingText = activity.thinking || '';
        if (!thinkingText.startsWith('Still working...')) {
            return;
        }
        const now = Date.now();
        const lastHeartbeat = this.lastHeartbeatAt.get(chatId) || 0;
        if (now - lastHeartbeat < 60000) {
            return;
        }
        this.lastHeartbeatAt.set(chatId, now);

        try {
            const params: Record<string, any> = {
                chat_id: chatId,
                text: `⏳ ${thinkingText}`,
                parse_mode: 'Markdown',
                disable_notification: true,
            };
            if (threadId !== undefined) params.message_thread_id = threadId;
            await this.api('sendMessage', params);
        } catch {
            // Best effort — don't break streaming for a status message
        }
    }

    // ── Formatting ─────────────────────────────────────────────────

    formatOutgoing(message: OutgoingMessage): OutgoingMessage {
        // Truncate very long messages — we handle chunking in sendToChat
        return message;
    }

    /**
     * Convert standard LLM markdown to Telegram-compatible Markdown.
     * Telegram's legacy Markdown mode has quirks:
     *   - **bold** must be *bold*
     *   - ## headers aren't supported — convert to bold lines
     *   - Unmatched * or _ break parsing
     *   - Nested formatting isn't supported
     */
    public sanitizeForTelegram(text: string): string {
        let result = text;

        // 1. Convert **bold** to *bold* (Telegram uses single asterisk for bold)
        result = result.replace(/\*\*(.+?)\*\*/g, '*$1*');

        // 2. Convert ## headers to bold lines
        result = result.replace(/^#{1,6}\s+(.+)$/gm, '*$1*');

        // 3. Convert > blockquotes (not supported in legacy mode)
        result = result.replace(/^>\s+(.+)$/gm, '│ $1');

        // 4. Convert --- / *** horizontal rules
        result = result.replace(/^[-*_]{3,}$/gm, '————————');

        // 5. Ensure code blocks use ``` which Telegram supports
        // (already compatible — no change needed)

        // 6. Strip HTML tags that might be in the response
        result = result.replace(/<[^>]+>/g, '');

        return result;
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
    public startTyping(chatId: number, threadId?: number): void {
        const params: Record<string, any> = { chat_id: chatId, action: 'typing' };
        if (threadId !== undefined) params.message_thread_id = threadId;

        // Send immediately
        this.api('sendChatAction', params).catch(() => {});

        // Refresh every 4 seconds
        if (!this.typingIntervals.has(chatId)) {
            const interval = setInterval(() => {
                this.api('sendChatAction', params).catch(() => {});
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
                this.queuePersistSessionState();
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
        this.queuePersistSessionState();
        return userId;
    }

    public rememberThread(userId: string, threadId?: number): void {
        this.userThreadMap.set(userId, threadId);
        this.queuePersistSessionState();
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
     * Includes threadId so different forum topics get distinct conversation IDs.
     */
    public resolveConversationId(chatId: number, suffix?: string, threadId?: number): string {
        const threadPart = threadId !== undefined ? `:t${threadId}` : '';
        const base = `telegram-conv:${chatId}${threadPart}`;
        const seed = suffix ? `${base}:${suffix}` : base;
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

        const data = (await res.json()) as { ok: boolean; result: any; description?: string };

        if (!data.ok) {
            throw new Error(
                `Telegram API error: ${data.description || 'Unknown error'} (${method})`,
            );
        }

        return data.result;
    }

    private queuePersistSessionState(): void {
        if (!this.sessionStore) {
            return;
        }
        if (this.persistTimer) {
            clearTimeout(this.persistTimer);
        }
        this.persistTimer = setTimeout(() => {
            this.persistTimer = undefined;
            void this.persistSessionState();
        }, 25);
    }

    private async restoreSessionState(): Promise<void> {
        if (!this.sessionStore) {
            return;
        }
        const state = await this.sessionStore.getTelegramState();
        if (!state) {
            return;
        }
        this.applySessionState(state);
    }

    private applySessionState(state: TelegramChannelStateRecord): void {
        this.pollingOffset = Number(state.pollingOffset || 0);
        this.chatIdMap.clear();
        this.userIdMap.clear();
        this.userThreadMap.clear();
        for (const mapping of state.mappings || []) {
            this.chatIdMap.set(mapping.userId, mapping.chatId);
            this.userIdMap.set(mapping.chatId, mapping.userId);
            this.userThreadMap.set(mapping.userId, mapping.threadId);
        }
    }

    private async persistSessionState(): Promise<void> {
        if (!this.sessionStore) {
            return;
        }
        const mappings = Array.from(this.chatIdMap.entries()).map(([userId, chatId]) => ({
            userId,
            chatId,
            threadId: this.userThreadMap.get(userId),
        }));
        await this.sessionStore.setTelegramState({
            mappings,
            pollingOffset: this.pollingOffset,
            updatedAt: new Date().toISOString(),
        });
    }
}
