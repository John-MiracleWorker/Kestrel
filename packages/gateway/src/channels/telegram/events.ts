import { Attachment } from '../base';
import { createIngressEnvelope, type NormalizedIngressPayloadKind } from '../ingress';

import type { TelegramUser } from './types';
import type { TelegramAdapter } from './index';

export type TelegramIngressSeed = {
    userId: string;
    from: TelegramUser;
    chatId: number;
    content: string;
    conversationId: string;
    timestamp: Date;
    threadId?: number;
    channelMessageId?: string;
    attachments?: Attachment[];
    payloadKind?: NormalizedIngressPayloadKind;
    metadata?: Record<string, unknown>;
};

export function emitTelegramIngress(adapter: TelegramAdapter, seed: TelegramIngressSeed): void {
    const event = createIngressEnvelope({
        channel: 'telegram',
        userId: seed.userId,
        workspaceId: adapter.config.defaultWorkspaceId,
        conversationId: seed.conversationId,
        content: seed.content,
        attachments: seed.attachments,
        metadata: {
            channelUserId: String(seed.from.id),
            channelMessageId: seed.channelMessageId,
            timestamp: seed.timestamp,
            telegramChatId: seed.chatId,
            telegramThreadId: seed.threadId,
            telegramUsername: seed.from.username,
            ...seed.metadata,
        },
        externalUserId: String(seed.from.id),
        externalConversationId: String(seed.chatId),
        externalThreadId: seed.threadId !== undefined ? String(seed.threadId) : undefined,
        authContext: {
            transport: adapter.config.mode === 'webhook' ? 'telegram_webhook' : 'telegram_polling',
            authenticatedUserId: seed.userId,
            isProvisionalUser: false,
        },
        payloadKind: seed.payloadKind,
    });

    (adapter as any).emit('message', event);
}
