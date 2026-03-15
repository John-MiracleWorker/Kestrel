import { logger } from '../../utils/logger';
import { handleApproval } from './approvals';
import { emitTelegramIngress } from './events';

import type { TelegramAdapter } from './index';
import type { TelegramMessage, TelegramUser } from './types';

export async function handleCallbackQuery(
    adapter: TelegramAdapter,
    query: {
        id: string;
        from: TelegramUser;
        message?: TelegramMessage;
        data?: string;
    },
): Promise<void> {
    await adapter.api('answerCallbackQuery', { callback_query_id: query.id });

    if (!query.data || !query.message) {
        return;
    }
    const chatId = query.message.chat.id;
    const threadId = query.message.message_thread_id;

    if (query.data.startsWith('model:')) {
        const modelName = query.data.substring('model:'.length);
        const userId = adapter.resolveUserId(query.from, chatId);
        adapter.rememberThread(userId, threadId);

        const params: Record<string, any> = {
            chat_id: chatId,
            text: `🔄 Switching to \`${modelName}\`...`,
            parse_mode: 'Markdown',
        };
        if (threadId !== undefined) {
            params.message_thread_id = threadId;
        }
        await adapter.api('sendMessage', params);

        emitTelegramIngress(adapter, {
            userId,
            from: query.from,
            chatId,
            conversationId: adapter.resolveConversationId(chatId, undefined, threadId),
            content: `Switch to model "${modelName}" on Ollama. Use the model_swap tool with action="swap", model_id="${modelName}", provider="ollama".`,
            threadId,
            channelMessageId: String(query.message.message_id),
            timestamp: new Date(),
            payloadKind: 'command',
            metadata: {
                isCallbackQuery: true,
                isCommand: true,
            },
        });
        return;
    }

    if (query.data.startsWith('si_approve:') || query.data.startsWith('si_deny:')) {
        const isApprove = query.data.startsWith('si_approve:');
        const proposalId = query.data.substring(query.data.indexOf(':') + 1);
        const action = isApprove ? 'approve' : 'deny';
        const icon = isApprove ? '✅' : '❌';

        const processingParams: Record<string, any> = {
            chat_id: chatId,
            text: `${icon} Processing ${action} for proposal \`${proposalId.substring(0, 8)}...\``,
            parse_mode: 'Markdown',
        };
        if (threadId !== undefined) {
            processingParams.message_thread_id = threadId;
        }
        await adapter.api('sendMessage', processingParams);

        try {
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

            const responseParams: Record<string, any> = result.error
                ? {
                      chat_id: chatId,
                      text: `⚠️ ${result.error}`,
                  }
                : {
                      chat_id: chatId,
                      text: `${icon} *${result.status === 'approved' ? 'Approved' : 'Denied'}*\n${result.message || ''}`,
                      parse_mode: 'Markdown',
                  };
            if (threadId !== undefined) {
                responseParams.message_thread_id = threadId;
            }
            await adapter.api('sendMessage', responseParams);
        } catch (error) {
            logger.error('Self-improve callback failed', { error: (error as Error).message });
            const params: Record<string, any> = {
                chat_id: chatId,
                text: `⚠️ Failed to process: ${(error as Error).message?.substring(0, 200)}`,
            };
            if (threadId !== undefined) {
                params.message_thread_id = threadId;
            }
            await adapter.api('sendMessage', params);
        }
        return;
    }

    if (query.data.startsWith('approve:')) {
        const approvalId = query.data.substring('approve:'.length);
        const actorUserId = adapter.resolveUserId(query.from, chatId);
        await handleApproval(adapter, chatId, approvalId, true, actorUserId, threadId);
        return;
    }
    if (query.data.startsWith('reject:')) {
        const approvalId = query.data.substring('reject:'.length);
        const actorUserId = adapter.resolveUserId(query.from, chatId);
        await handleApproval(adapter, chatId, approvalId, false, actorUserId, threadId);
        return;
    }

    const userId = adapter.resolveUserId(query.from, chatId);
    emitTelegramIngress(adapter, {
        userId,
        from: query.from,
        chatId,
        conversationId: adapter.resolveConversationId(chatId, undefined, threadId),
        content: query.data,
        threadId,
        channelMessageId: String(query.message.message_id),
        timestamp: new Date(),
        metadata: {
            isCallbackQuery: true,
        },
    });
}
