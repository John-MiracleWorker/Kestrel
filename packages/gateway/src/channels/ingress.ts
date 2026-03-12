import { createHash, randomUUID } from 'crypto';
import { z } from 'zod';
import type { Attachment, ChannelType, IncomingMessage } from './base';
import { generateCorrelationId } from '../utils/logger';

const channelTypeSchema = z.enum([
    'web',
    'whatsapp',
    'telegram',
    'discord',
    'mobile',
    'moltbook',
    'feishu',
]);

const attachmentSchema: z.ZodType<Attachment> = z.object({
    type: z.enum(['image', 'audio', 'video', 'file']),
    url: z.string(),
    mimeType: z.string().optional(),
    size: z.number().optional(),
    filename: z.string().optional(),
});

const dateSchema = z.preprocess((value) => {
    if (value instanceof Date) return value;
    if (typeof value === 'string' || typeof value === 'number') return new Date(value);
    return value;
}, z.date());

const metadataSchema = z
    .object({
        channelUserId: z.string(),
        channelMessageId: z.string(),
        timestamp: dateSchema,
    })
    .catchall(z.unknown());

const authContextSchema = z.object({
    transport: z.string().min(1),
    authenticatedUserId: z.string().optional(),
    sessionId: z.string().optional(),
    isProvisionalUser: z.boolean(),
});

const payloadKindSchema = z.enum(['message', 'task', 'command']);

const payloadSchema = z.object({
    kind: payloadKindSchema,
    content: z.string(),
    attachments: z.array(attachmentSchema).default([]),
});

export const normalizedIngressEventSchema = z.object({
    id: z.string().min(1),
    channel: channelTypeSchema,
    userId: z.string().min(1),
    workspaceId: z.string().min(1),
    conversationId: z.string().optional(),
    content: z.string(),
    attachments: z.array(attachmentSchema).optional(),
    metadata: metadataSchema,
    externalUserId: z.string().min(1),
    externalConversationId: z.string().optional(),
    externalThreadId: z.string().optional(),
    dedupeKey: z.string().min(1),
    correlationId: z.string().min(1),
    receivedAt: dateSchema,
    authContext: authContextSchema,
    payload: payloadSchema,
    rawMetadata: z.record(z.string(), z.unknown()),
});

export type NormalizedIngressAuthContext = z.infer<typeof authContextSchema>;
export type NormalizedIngressPayloadKind = z.infer<typeof payloadKindSchema>;
export type NormalizedIngressEvent = z.infer<typeof normalizedIngressEventSchema>;

type CreateNormalizedIngressEventInput = {
    id?: string;
    channel: ChannelType;
    userId: string;
    workspaceId: string;
    conversationId?: string;
    content: string;
    attachments?: Attachment[];
    metadata: {
        channelUserId: string;
        channelMessageId?: string;
        timestamp: Date | string | number;
        [key: string]: unknown;
    };
    externalUserId?: string;
    externalConversationId?: string;
    externalThreadId?: string;
    dedupeKey?: string;
    correlationId?: string;
    receivedAt?: Date | string | number;
    authContext: NormalizedIngressAuthContext;
    payloadKind?: NormalizedIngressPayloadKind;
    rawMetadata?: Record<string, unknown>;
};

type BuildDedupeKeyInput = {
    channel: ChannelType;
    externalUserId: string;
    externalConversationId?: string;
    channelMessageId?: string;
    content: string;
};

function stableTextHash(value: string): string {
    return createHash('sha1').update(value).digest('hex').slice(0, 12);
}

function coerceOptionalString(value: unknown): string | undefined {
    if (typeof value === 'string' && value.trim()) return value;
    if (typeof value === 'number' && Number.isFinite(value)) return String(value);
    return undefined;
}

function inferExternalThreadId(metadata: Record<string, unknown>): string | undefined {
    const keys = [
        'channelThreadId',
        'telegramThreadId',
        'discordThreadId',
        'whatsappThreadId',
        'feishuThreadId',
        'moltbookThreadId',
        'threadId',
    ];

    for (const key of keys) {
        const value = coerceOptionalString(metadata[key]);
        if (value) return value;
    }

    return undefined;
}

function inferPayloadKind(message: IncomingMessage): NormalizedIngressPayloadKind {
    const metadata = message.metadata as Record<string, unknown>;
    if (metadata.isTaskRequest === true) return 'task';
    if (metadata.isSlashCommand === true || message.content.trim().startsWith('/'))
        return 'command';
    return 'message';
}

export function buildIngressDedupeKey(input: BuildDedupeKeyInput): string {
    const conversationId = input.externalConversationId || 'direct';
    const messageRef =
        input.channelMessageId ||
        stableTextHash(`${conversationId}:${input.content.trim() || '__empty__'}`);

    return `${input.channel}:${input.externalUserId}:${conversationId}:${messageRef}`;
}

export function parseNormalizedIngressEvent(value: unknown): NormalizedIngressEvent {
    return normalizedIngressEventSchema.parse(value);
}

export function isNormalizedIngressEvent(value: unknown): value is NormalizedIngressEvent {
    return normalizedIngressEventSchema.safeParse(value).success;
}

export function createNormalizedIngressEvent(
    input: CreateNormalizedIngressEventInput,
): NormalizedIngressEvent {
    const attachments = input.attachments?.length ? input.attachments : undefined;
    const metadata = {
        ...input.metadata,
        channelMessageId: input.metadata.channelMessageId || input.id || randomUUID(),
    };
    const externalUserId = input.externalUserId || metadata.channelUserId || input.userId;
    const externalConversationId = input.externalConversationId ?? input.conversationId;
    const dedupeKey =
        input.dedupeKey ||
        buildIngressDedupeKey({
            channel: input.channel,
            externalUserId,
            externalConversationId,
            channelMessageId: metadata.channelMessageId,
            content: input.content,
        });

    const event = {
        id: input.id || randomUUID(),
        channel: input.channel,
        userId: input.userId,
        workspaceId: input.workspaceId,
        conversationId: input.conversationId,
        content: input.content,
        attachments,
        metadata,
        externalUserId,
        externalConversationId,
        externalThreadId:
            input.externalThreadId || inferExternalThreadId(metadata as Record<string, unknown>),
        dedupeKey,
        correlationId: input.correlationId || generateCorrelationId(),
        receivedAt: input.receivedAt || metadata.timestamp,
        authContext: input.authContext,
        payload: {
            kind: input.payloadKind || 'message',
            content: input.content,
            attachments: attachments || [],
        },
        rawMetadata: input.rawMetadata || { ...metadata },
    };

    return parseNormalizedIngressEvent(event);
}

export function normalizeIngressEvent(
    message: IncomingMessage | NormalizedIngressEvent,
): NormalizedIngressEvent {
    if (isNormalizedIngressEvent(message)) {
        return parseNormalizedIngressEvent(message);
    }

    return createNormalizedIngressEvent({
        id: message.id,
        channel: message.channel,
        userId: message.userId,
        workspaceId: message.workspaceId,
        conversationId: message.conversationId,
        content: message.content,
        attachments: message.attachments,
        metadata: {
            ...message.metadata,
            channelMessageId: message.metadata.channelMessageId || message.id,
        },
        externalUserId: message.metadata.channelUserId || message.userId,
        externalConversationId: message.conversationId,
        authContext: {
            transport: 'legacy_adapter',
            authenticatedUserId: message.userId,
            isProvisionalUser: false,
        },
        payloadKind: inferPayloadKind(message),
        rawMetadata: { ...message.metadata },
    });
}

export function buildBrainStreamChatRequest(
    message: NormalizedIngressEvent,
    overrides: {
        conversationId?: string;
        provider?: string;
        model?: string;
    } = {},
): {
    userId: string;
    workspaceId: string;
    conversationId: string;
    messages: Array<{ role: number; content: string }>;
    provider: string;
    model: string;
    parameters: Record<string, string>;
} {
    const parameters: Record<string, string> = {
        channel: message.channel,
        correlation_id: message.correlationId,
        ingress_dedupe_key: message.dedupeKey,
        ingress_kind: message.payload.kind,
        auth_transport: message.authContext.transport,
        external_user_id: message.externalUserId,
    };

    if (message.externalConversationId) {
        parameters.external_conversation_id = message.externalConversationId;
    }
    if (message.externalThreadId) {
        parameters.external_thread_id = message.externalThreadId;
    }
    if (message.attachments?.length) {
        parameters.attachments = JSON.stringify(message.attachments);
    }

    return {
        userId: message.userId,
        workspaceId: message.workspaceId,
        conversationId: overrides.conversationId ?? message.conversationId ?? '',
        messages: [{ role: 0, content: message.payload.content }],
        provider: overrides.provider || '',
        model: overrides.model || '',
        parameters,
    };
}
