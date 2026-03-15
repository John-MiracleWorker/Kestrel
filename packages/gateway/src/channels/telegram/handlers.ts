import { Attachment } from '../base';
import { parseApprovalDecision } from '../orchestration/intents';

import { handleApproval, sendApprovalRequest, sendTaskProgress } from './approvals';
import { handleCallbackQuery } from './callbacks';
import { handleCommand, handleTaskRequest, resolveTaskRequest } from './commands';
import { emitTelegramIngress } from './events';

import type { TelegramAdapter } from './index';
import type { TelegramUpdate } from './types';

export async function processUpdate(
    adapter: TelegramAdapter,
    update: TelegramUpdate,
): Promise<void> {
    if (update.callback_query) {
        await handleCallbackQuery(adapter, update.callback_query);
        return;
    }
    if (!update.message) {
        return;
    }

    const msg = update.message;
    const from = msg.from;
    if (!from) {
        return;
    }
    if (!adapter.isAllowed(from.id)) {
        await adapter.api('sendMessage', {
            chat_id: msg.chat.id,
            text: '🔒 Access denied. You are not authorized to use this bot.',
        });
        return;
    }

    const text = msg.text || msg.caption || '';
    if (text.startsWith('/')) {
        await handleCommand(adapter, msg, text);
        return;
    }

    const taskGoal = resolveTaskRequest(text);
    if (taskGoal) {
        await handleTaskRequest(adapter, msg, from, taskGoal);
        return;
    }

    const pendingForChat = [...adapter.pendingApprovals.entries()].filter(
        ([, approval]) => approval.chatId === msg.chat.id,
    );
    if (pendingForChat.length > 0) {
        const approvalDecision = parseApprovalDecision(text);
        if (approvalDecision !== null) {
            const resolvedUser = adapter.resolveUserId(from, msg.chat.id);
            const threadId = msg.message_thread_id;
            const [approvalId] = pendingForChat[pendingForChat.length - 1];
            await handleApproval(
                adapter,
                msg.chat.id,
                approvalId,
                approvalDecision,
                resolvedUser,
                threadId,
            );
            return;
        }
    }

    const threadId = msg.message_thread_id;
    adapter.startTyping(msg.chat.id, threadId);

    const userId = adapter.resolveUserId(from, msg.chat.id);
    adapter.rememberThread(userId, threadId);

    const attachments: Attachment[] = [];
    if (msg.photo?.length) {
        const largest = msg.photo[msg.photo.length - 1];
        attachments.push({
            type: 'image',
            url: `tg://${largest.file_id}`,
            mimeType: 'image/jpeg',
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

    emitTelegramIngress(adapter, {
        userId,
        from,
        chatId: msg.chat.id,
        conversationId: adapter.resolveConversationId(msg.chat.id, undefined, threadId),
        content: text,
        attachments: attachments.length ? attachments : undefined,
        threadId,
        channelMessageId: String(msg.message_id),
        timestamp: new Date(msg.date * 1000),
    });
}

export { handleApproval, sendApprovalRequest, sendTaskProgress };
export { handleCallbackQuery } from './callbacks';
export { handleCommand, handleTaskRequest } from './commands';
