import { getMediaDir } from '../../utils/paths';
import {
    BaseChannelAdapter,
    type Attachment,
    type ChannelType,
    type OutgoingMessage,
    type StreamHandle,
    type ToolActivity,
} from '../base';
import type { ChannelSessionStore } from '../store';

import {
    connectTelegramAdapter,
    disconnectTelegramAdapter,
    startTyping,
    stopTyping,
    poll,
    startPolling,
    callTelegramApi,
    queuePersistSessionState,
} from './adapter-lifecycle';
import {
    formatOutgoingMessage,
    handleAttachment,
    sanitizeForTelegram,
    sendMessageForUser,
    sendToChat,
} from './adapter-messaging';
import {
    cancelActiveStream,
    escapeMarkdown,
    isAllowed,
    listPendingApprovalsForUser,
    rememberThread,
    resolveConversationId,
    resolvePendingApproval,
    resolveUserId,
    sendApprovalRequestForUser,
    setApprovalHandler,
    setCancelStreamHandler,
    setPendingApprovalsLookupHandler,
} from './adapter-state';
import {
    sendStreamEnd,
    sendStreamStart,
    sendStreamUpdate,
    sendToolActivity,
} from './adapter-streaming';
import type { TelegramConfig } from './types';

export class TelegramAdapter extends BaseChannelAdapter {
    readonly channelType: ChannelType = 'telegram';
    public readonly mediaDir = getMediaDir();
    public readonly apiBase: string;

    public pollingActive = false;
    public pollingOffset = 0;
    public pollingTimer?: NodeJS.Timeout;
    public persistTimer?: NodeJS.Timeout;

    public chatIdMap = new Map<string, number>();
    public userIdMap = new Map<number, string>();
    public typingIntervals = new Map<number, NodeJS.Timeout>();
    public lastHeartbeatAt = new Map<number, number>();
    public chatModes = new Map<number, 'chat' | 'task'>();
    public pendingApprovals = new Map<
        string,
        { taskId: string; chatId: number; userId: string; threadId?: number }
    >();
    public userThreadMap = new Map<string, number | undefined>();
    public sentMedia = new Set<string>();

    public _botId: number | undefined;
    public _botUsername: string | undefined;
    public approvalHandler?: (
        approvalId: string,
        userId: string,
        approved: boolean,
    ) => Promise<{ success: boolean; error?: string }>;
    public pendingApprovalsLookupHandler?: (
        userId: string,
        workspaceId: string,
    ) => Promise<Array<{ approval_id: string }>>;
    public cancelStreamHandler?: (userId: string) => void;
    public readonly sessionStore?: ChannelSessionStore;

    constructor(
        public config: TelegramConfig,
        options: { sessionStore?: ChannelSessionStore } = {},
    ) {
        super();
        this.apiBase = `https://api.telegram.org/bot${config.botToken}`;
        this.sessionStore = options.sessionStore;
    }

    get botInfo(): { id: number; username: string } | undefined {
        if (!this._botId || !this._botUsername) {
            return undefined;
        }
        return { id: this._botId, username: this._botUsername };
    }

    public setApprovalHandler(
        handler: (
            approvalId: string,
            userId: string,
            approved: boolean,
        ) => Promise<{ success: boolean; error?: string }>,
    ): void {
        setApprovalHandler(this, handler);
    }

    public setPendingApprovalsLookupHandler(
        handler: (userId: string, workspaceId: string) => Promise<Array<{ approval_id: string }>>,
    ): void {
        setPendingApprovalsLookupHandler(this, handler);
    }

    public setCancelStreamHandler(handler: (userId: string) => void): void {
        setCancelStreamHandler(this, handler);
    }

    public cancelActiveStream(userId: string): void {
        cancelActiveStream(this, userId);
    }

    public async resolvePendingApproval(
        approvalId: string,
        approved: boolean,
        actorUserId?: string,
    ): Promise<{ success: boolean; error?: string }> {
        return resolvePendingApproval(this, approvalId, approved, actorUserId);
    }

    public async listPendingApprovalsForUser(
        userId: string,
    ): Promise<Array<{ approval_id: string }>> {
        return listPendingApprovalsForUser(this, userId);
    }

    public async sendApprovalRequestForUser(
        userId: string,
        approvalId: string,
        description: string,
        taskId: string,
    ): Promise<void> {
        return sendApprovalRequestForUser(this, userId, approvalId, description, taskId);
    }

    async connect(): Promise<void> {
        return connectTelegramAdapter(this);
    }

    async disconnect(): Promise<void> {
        return disconnectTelegramAdapter(this);
    }

    async send(userId: string, message: OutgoingMessage): Promise<void> {
        return sendMessageForUser(this, userId, message);
    }

    async sendToChat(chatId: number, message: OutgoingMessage, threadId?: number): Promise<void> {
        return sendToChat(this, chatId, message, threadId);
    }

    async sendStreamStart(userId: string, meta: { conversationId: string }): Promise<StreamHandle> {
        return sendStreamStart(this, userId, meta);
    }

    async sendStreamUpdate(handle: StreamHandle, accumulatedContent: string): Promise<void> {
        return sendStreamUpdate(this, handle, accumulatedContent);
    }

    async sendStreamEnd(handle: StreamHandle, finalContent: string): Promise<void> {
        return sendStreamEnd(this, handle, finalContent);
    }

    async sendToolActivity(
        userId: string,
        handle: StreamHandle,
        activity: ToolActivity,
    ): Promise<void> {
        return sendToolActivity(this, userId, handle, activity);
    }

    formatOutgoing(message: OutgoingMessage): OutgoingMessage {
        return formatOutgoingMessage(this, message);
    }

    public sanitizeForTelegram(text: string): string {
        return sanitizeForTelegram(this, text);
    }

    async handleAttachment(attachment: Attachment): Promise<Attachment> {
        return handleAttachment(this, attachment);
    }

    public startTyping(chatId: number, threadId?: number): void {
        startTyping(this, chatId, threadId);
    }

    public stopTyping(chatId: number): void {
        stopTyping(this, chatId);
    }

    public isAllowed(userId: number): boolean {
        return isAllowed(this, userId);
    }

    public startPolling(): void {
        startPolling(this);
    }

    public async poll(): Promise<void> {
        return poll(this);
    }

    public resolveUserId(from: { id: number }, chatId: number): string {
        return resolveUserId(this, from, chatId);
    }

    public rememberThread(userId: string, threadId?: number): void {
        rememberThread(this, userId, threadId);
    }

    public resolveConversationId(chatId: number, suffix?: string, threadId?: number): string {
        return resolveConversationId(this, chatId, suffix, threadId);
    }

    public escapeMarkdown(text: string): string {
        return escapeMarkdown(this, text);
    }

    public async api(method: string, params?: Record<string, any>): Promise<any> {
        return callTelegramApi(this, method, params);
    }

    public queuePersistSessionState(): void {
        queuePersistSessionState(this);
    }
}
