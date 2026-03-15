import { logger } from '../../utils/logger';

import type { TelegramAdapter } from './index';

export async function handleApproval(
    adapter: TelegramAdapter,
    chatId: number,
    approvalId: string,
    approved: boolean,
    actorUserId?: string,
    threadId?: number,
): Promise<void> {
    const icon = approved ? '✅' : '❌';
    const action = approved ? 'Approved' : 'Rejected';

    const result = await adapter.resolvePendingApproval(approvalId, approved, actorUserId);
    if (!result.success) {
        const params: Record<string, any> = {
            chat_id: chatId,
            text: `⚠️ Could not process approval \`${approvalId}\`: ${adapter.escapeMarkdown(result.error || 'unknown error')}`,
            parse_mode: 'Markdown',
        };
        if (threadId !== undefined) {
            params.message_thread_id = threadId;
        }
        await adapter.api('sendMessage', params);
        return;
    }

    const params: Record<string, any> = {
        chat_id: chatId,
        text: `${icon} *${action}* approval \`${approvalId}\``,
        parse_mode: 'Markdown',
    };
    if (threadId !== undefined) {
        params.message_thread_id = threadId;
    }
    await adapter.api('sendMessage', params);
}

export async function sendApprovalRequest(
    adapter: TelegramAdapter,
    chatId: number,
    approvalId: string,
    description: string,
    taskId: string,
    userId: string,
    threadId?: number,
): Promise<void> {
    adapter.pendingApprovals.set(approvalId, { taskId, chatId, userId, threadId });

    const replyMarkup = JSON.stringify({
        inline_keyboard: [
            [
                { text: '✅ Approve', callback_data: `approve:${approvalId}` },
                { text: '❌ Reject', callback_data: `reject:${approvalId}` },
            ],
        ],
    });

    const params: Record<string, any> = {
        chat_id: chatId,
        text:
            '⚠️ *Approval Required*\n\n' +
            `${adapter.escapeMarkdown(description)}\n\n` +
            `ID: ${adapter.escapeMarkdown(approvalId)}`,
        parse_mode: 'Markdown',
        reply_markup: replyMarkup,
    };
    if (threadId !== undefined) {
        params.message_thread_id = threadId;
    }

    try {
        await adapter.api('sendMessage', params);
    } catch (error) {
        logger.error('Telegram sendMessage failed for approval request', {
            method: 'sendMessage',
            approvalId,
            chatId,
            error,
        });

        const fallbackParams: Record<string, any> = {
            chat_id: chatId,
            text: '⚠️ Approval Required\n\n' + `${description}\n\n` + `ID: ${approvalId}`,
            reply_markup: replyMarkup,
        };
        if (threadId !== undefined) {
            fallbackParams.message_thread_id = threadId;
        }

        try {
            await adapter.api('sendMessage', fallbackParams);
        } catch (fallbackError) {
            logger.error('Telegram fallback sendMessage failed for approval request', {
                method: 'sendMessage',
                approvalId,
                chatId,
                error: fallbackError,
            });
            throw fallbackError;
        }
    }
}

export async function sendTaskProgress(
    adapter: TelegramAdapter,
    chatId: number,
    step: string,
    detail = '',
    threadId?: number,
): Promise<void> {
    const text = detail
        ? `🔧 *${adapter.escapeMarkdown(step)}*\n${adapter.escapeMarkdown(detail)}`
        : `🔧 ${adapter.escapeMarkdown(step)}`;

    const params: Record<string, any> = {
        chat_id: chatId,
        text,
        parse_mode: 'Markdown',
        disable_notification: true,
    };
    if (threadId !== undefined) {
        params.message_thread_id = threadId;
    }
    await adapter.api('sendMessage', params);
}
