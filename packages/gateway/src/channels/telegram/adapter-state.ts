import { createHash } from 'crypto';

import { logger } from '../../utils/logger';
import { sendApprovalRequest } from './handlers';

type TelegramAdapterRuntime = any;

export function setApprovalHandler(
    adapter: TelegramAdapterRuntime,
    handler: (
        approvalId: string,
        userId: string,
        approved: boolean,
    ) => Promise<{ success: boolean; error?: string }>,
): void {
    adapter.approvalHandler = handler;
}

export function setPendingApprovalsLookupHandler(
    adapter: TelegramAdapterRuntime,
    handler: (userId: string, workspaceId: string) => Promise<Array<{ approval_id: string }>>,
): void {
    adapter.pendingApprovalsLookupHandler = handler;
}

export function setCancelStreamHandler(
    adapter: TelegramAdapterRuntime,
    handler: (userId: string) => void,
): void {
    adapter.cancelStreamHandler = handler;
}

export function cancelActiveStream(adapter: TelegramAdapterRuntime, userId: string): void {
    if (adapter.cancelStreamHandler) {
        adapter.cancelStreamHandler(userId);
    }
}

export async function resolvePendingApproval(
    adapter: TelegramAdapterRuntime,
    approvalId: string,
    approved: boolean,
    actorUserId?: string,
): Promise<{ success: boolean; error?: string }> {
    if (!adapter.approvalHandler) {
        return { success: false, error: 'Approval handler is not configured on the gateway.' };
    }

    const pending = adapter.pendingApprovals.get(approvalId);
    if (pending) {
        if (actorUserId && pending.userId && pending.userId !== actorUserId) {
            return { success: false, error: 'This approval request belongs to another user.' };
        }

        const result = await adapter.approvalHandler(approvalId, pending.userId, approved);
        if (result.success) {
            adapter.pendingApprovals.delete(approvalId);
        }
        return result;
    }

    if (!actorUserId) {
        return { success: false, error: 'Approval ID not found or already resolved.' };
    }

    return adapter.approvalHandler(approvalId, actorUserId, approved);
}

export async function listPendingApprovalsForUser(
    adapter: TelegramAdapterRuntime,
    userId: string,
): Promise<Array<{ approval_id: string }>> {
    if (!adapter.pendingApprovalsLookupHandler) {
        return [];
    }
    return adapter.pendingApprovalsLookupHandler(userId, adapter.config.defaultWorkspaceId);
}

export async function sendApprovalRequestForUser(
    adapter: TelegramAdapterRuntime,
    userId: string,
    approvalId: string,
    description: string,
    taskId: string,
): Promise<void> {
    const existing = adapter.pendingApprovals.get(approvalId);
    if (existing) {
        return;
    }

    const chatId = adapter.chatIdMap.get(userId);
    if (!chatId) {
        logger.warn('Cannot send Telegram approval request — no chat ID for user', {
            userId,
            approvalId,
        });
        return;
    }

    const threadId = adapter.userThreadMap.get(userId);
    await sendApprovalRequest(adapter, chatId, approvalId, description, taskId, userId, threadId);
}

export function isAllowed(adapter: TelegramAdapterRuntime, userId: number): boolean {
    if (!adapter.config.allowedUserIds?.length) {
        return true;
    }
    return adapter.config.allowedUserIds.includes(userId);
}

export function resolveUserId(
    adapter: TelegramAdapterRuntime,
    from: { id: number },
    chatId: number,
): string {
    const existing = adapter.userIdMap.get(chatId);
    if (existing) {
        return existing;
    }

    const userId = deterministicUUID(`telegram-user:${from.id}`);
    adapter.userIdMap.set(chatId, userId);
    adapter.chatIdMap.set(userId, chatId);
    adapter.queuePersistSessionState();
    return userId;
}

export function rememberThread(
    adapter: TelegramAdapterRuntime,
    userId: string,
    threadId?: number,
): void {
    adapter.userThreadMap.set(userId, threadId);
    adapter.queuePersistSessionState();
}

export function deterministicUUID(seed: string): string {
    const hash = createHash('sha256').update(seed).digest('hex');
    return [
        hash.substring(0, 8),
        hash.substring(8, 12),
        '4' + hash.substring(13, 16),
        ((parseInt(hash[16], 16) & 0x3) | 0x8).toString(16) + hash.substring(17, 20),
        hash.substring(20, 32),
    ].join('-');
}

export function resolveConversationId(
    _adapter: TelegramAdapterRuntime,
    chatId: number,
    suffix?: string,
    threadId?: number,
): string {
    const threadPart = threadId !== undefined ? `:t${threadId}` : '';
    const base = `telegram-conv:${chatId}${threadPart}`;
    const seed = suffix ? `${base}:${suffix}` : base;
    return deterministicUUID(seed);
}

export function escapeMarkdown(_adapter: TelegramAdapterRuntime, text: string): string {
    return text.replace(/([_*\[\]()~`>#+\-=|{}.!\\])/g, '\\$1');
}
