import { createHash, randomUUID } from 'crypto';
import { z } from 'zod';
import type { Attachment, ChannelType } from './base';
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

const dateSchema = z.preprocess((value: unknown) => {
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

export const ingressEnvelopeSchema = z.object({
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

export const sessionCommandSchema = z.object({
    sessionId: z.string().min(1),
    conversationId: z.string().optional(),
    userId: z.string().min(1),
    workspaceId: z.string().min(1),
    taskIntent: payloadKindSchema,
    normalizedContent: z.string(),
    attachments: z.array(attachmentSchema).default([]),
    returnRoute: z.object({
        channel: channelTypeSchema,
        userId: z.string().min(1),
        externalConversationId: z.string().optional(),
        externalThreadId: z.string().optional(),
        sessionId: z.string().min(1),
    }),
    approvalRoute: z.object({
        channel: channelTypeSchema,
        sessionId: z.string().min(1),
    }),
    artifactRoute: z.object({
        channel: channelTypeSchema,
        sessionId: z.string().min(1),
    }),
    parameters: z.record(z.string(), z.string()),
    ingress: ingressEnvelopeSchema,
});

export type NormalizedIngressAuthContext = z.infer<typeof authContextSchema>;
export type NormalizedIngressPayloadKind = z.infer<typeof payloadKindSchema>;
export type IngressEnvelope = z.infer<typeof ingressEnvelopeSchema>;
export type SessionCommand = z.infer<typeof sessionCommandSchema>;
export type NormalizedIngressEvent = IngressEnvelope;

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

export function buildIngressDedupeKey(input: BuildDedupeKeyInput): string {
    const conversationId = input.externalConversationId || 'direct';
    const messageRef =
        input.channelMessageId ||
        stableTextHash(`${conversationId}:${input.content.trim() || '__empty__'}`);

    return `${input.channel}:${input.externalUserId}:${conversationId}:${messageRef}`;
}

export function parseIngressEnvelope(value: unknown): IngressEnvelope {
    return ingressEnvelopeSchema.parse(value);
}

export function isIngressEnvelope(value: unknown): value is IngressEnvelope {
    return ingressEnvelopeSchema.safeParse(value).success;
}

export function createIngressEnvelope(input: CreateNormalizedIngressEventInput): IngressEnvelope {
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

    return parseIngressEnvelope(event);
}

export function createNormalizedIngressEvent(
    input: CreateNormalizedIngressEventInput,
): IngressEnvelope {
    return createIngressEnvelope(input);
}

export function buildSessionCommand(
    ingress: IngressEnvelope,
    route: {
        sessionId?: string;
        conversationId?: string;
    } = {},
): SessionCommand {
    const sessionId =
        route.sessionId || ingress.authContext.sessionId || ingress.conversationId || ingress.id;
    const parameters: Record<string, string> = {
        channel: ingress.channel,
        correlation_id: ingress.correlationId,
        ingress_dedupe_key: ingress.dedupeKey,
        ingress_kind: ingress.payload.kind,
        auth_transport: ingress.authContext.transport,
        external_user_id: ingress.externalUserId,
        session_id: sessionId,
        return_route: JSON.stringify({
            channel: ingress.channel,
            user_id: ingress.userId,
            external_conversation_id: ingress.externalConversationId || '',
            external_thread_id: ingress.externalThreadId || '',
            session_id: sessionId,
        }),
    };

    if (ingress.externalConversationId) {
        parameters.external_conversation_id = ingress.externalConversationId;
    }
    if (ingress.externalThreadId) {
        parameters.external_thread_id = ingress.externalThreadId;
    }
    if (ingress.attachments?.length) {
        parameters.attachments = JSON.stringify(ingress.attachments);
    }

    return sessionCommandSchema.parse({
        sessionId,
        conversationId: route.conversationId ?? ingress.conversationId,
        userId: ingress.userId,
        workspaceId: ingress.workspaceId,
        taskIntent: ingress.payload.kind,
        normalizedContent: ingress.payload.content,
        attachments: ingress.payload.attachments,
        returnRoute: {
            channel: ingress.channel,
            userId: ingress.userId,
            externalConversationId: ingress.externalConversationId,
            externalThreadId: ingress.externalThreadId,
            sessionId,
        },
        approvalRoute: {
            channel: ingress.channel,
            sessionId,
        },
        artifactRoute: {
            channel: ingress.channel,
            sessionId,
        },
        parameters,
        ingress,
    });
}

export function buildBrainStreamChatRequest(
    command: SessionCommand,
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
    return {
        userId: command.userId,
        workspaceId: command.workspaceId,
        conversationId: overrides.conversationId ?? command.conversationId ?? '',
        messages: [{ role: 0, content: command.normalizedContent }],
        provider: overrides.provider || '',
        model: overrides.model || '',
        parameters: { ...command.parameters },
    };
}

export const normalizedIngressEventSchema = ingressEnvelopeSchema;
export const parseNormalizedIngressEvent = parseIngressEnvelope;
export const isNormalizedIngressEvent = isIngressEnvelope;
