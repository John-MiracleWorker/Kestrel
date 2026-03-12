import { describe, expect, it } from 'vitest';
import {
    buildBrainStreamChatRequest,
    buildSessionCommand,
    createNormalizedIngressEvent,
} from '../src/channels/ingress';

describe('ingress envelope pipeline', () => {
    it('creates a normalized ingress event with generated correlation and dedupe keys', () => {
        const event = createNormalizedIngressEvent({
            channel: 'web',
            userId: 'user-1',
            workspaceId: 'ws-1',
            conversationId: 'conv-1',
            content: 'hello world',
            metadata: {
                channelUserId: 'user-1',
                channelMessageId: 'client-msg-1',
                timestamp: new Date('2026-03-12T10:00:00.000Z'),
            },
            authContext: {
                transport: 'websocket_jwt',
                authenticatedUserId: 'user-1',
                sessionId: 'sess-1',
                isProvisionalUser: false,
            },
        });

        expect(event.channel).toBe('web');
        expect(event.payload.kind).toBe('message');
        expect(event.payload.content).toBe('hello world');
        expect(event.correlationId).toMatch(/^kst-/);
        expect(event.dedupeKey).toContain('web:user-1:conv-1:client-msg-1');
    });

    it('builds a session command with cross-surface routing metadata', () => {
        const event = createNormalizedIngressEvent({
            id: 'ingress-1',
            channel: 'telegram',
            userId: 'resolved-user',
            workspaceId: 'ws-1',
            conversationId: 'tg-chat-99',
            content: 'deploy it',
            metadata: {
                channelUserId: 'tg-user-42',
                channelMessageId: '99',
                timestamp: new Date('2026-03-12T11:00:00.000Z'),
                telegramThreadId: 777,
            },
            externalUserId: 'tg-user-42',
            externalConversationId: 'tg-chat-99',
            externalThreadId: '777',
            authContext: {
                transport: 'telegram_polling',
                authenticatedUserId: 'resolved-user',
                sessionId: 'sess-telegram-1',
                isProvisionalUser: false,
            },
            payloadKind: 'task',
        });
        const command = buildSessionCommand(event, {
            sessionId: 'sess-telegram-1',
            conversationId: 'brain-conv-1',
        });

        expect(command.sessionId).toBe('sess-telegram-1');
        expect(command.conversationId).toBe('brain-conv-1');
        expect(command.taskIntent).toBe('task');
        expect(command.returnRoute).toMatchObject({
            channel: 'telegram',
            externalConversationId: 'tg-chat-99',
            externalThreadId: '777',
        });
        expect(JSON.parse(command.parameters.return_route)).toMatchObject({
            channel: 'telegram',
            external_conversation_id: 'tg-chat-99',
            external_thread_id: '777',
            session_id: 'sess-telegram-1',
        });
    });

    it('builds a Brain stream request with ingress metadata in parameters', () => {
        const event = createNormalizedIngressEvent({
            channel: 'web',
            userId: 'user-1',
            workspaceId: 'ws-1',
            conversationId: 'conv-1',
            content: 'inspect this file',
            attachments: [
                {
                    type: 'file',
                    url: 'https://example.test/file.txt',
                    filename: 'file.txt',
                },
            ],
            metadata: {
                channelUserId: 'user-1',
                channelMessageId: 'client-msg-2',
                timestamp: new Date('2026-03-12T12:00:00.000Z'),
            },
            authContext: {
                transport: 'websocket_jwt',
                authenticatedUserId: 'user-1',
                sessionId: 'sess-2',
                isProvisionalUser: false,
            },
        });
        const command = buildSessionCommand(event, {
            sessionId: 'sess-2',
            conversationId: 'conv-1',
        });

        const request = buildBrainStreamChatRequest(command, {
            provider: 'openai',
            model: 'gpt-5-nano',
            conversationId: 'conv-1',
        });

        expect(request.userId).toBe('user-1');
        expect(request.parameters.channel).toBe('web');
        expect(request.parameters.correlation_id).toBe(event.correlationId);
        expect(request.parameters.ingress_dedupe_key).toBe(event.dedupeKey);
        expect(request.parameters.auth_transport).toBe('websocket_jwt');
        expect(request.parameters.attachments).toContain('file.txt');
    });
});
