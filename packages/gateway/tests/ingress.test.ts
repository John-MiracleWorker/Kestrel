import { describe, expect, it } from 'vitest';
import type { IncomingMessage } from '../src/channels/base';
import {
    buildBrainStreamChatRequest,
    createNormalizedIngressEvent,
    normalizeIngressEvent,
} from '../src/channels/ingress';

describe('normalized ingress contract', () => {
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

    it('upgrades a legacy incoming message emitted by existing adapters', () => {
        const legacy: IncomingMessage = {
            id: 'legacy-1',
            channel: 'telegram',
            userId: 'resolved-user',
            workspaceId: 'ws-1',
            conversationId: 'tg-chat-99',
            content: 'deploy it',
            metadata: {
                channelUserId: 'tg-user-42',
                channelMessageId: '99',
                timestamp: new Date('2026-03-12T11:00:00.000Z'),
                isTaskRequest: true,
                telegramThreadId: 777,
            },
        };

        const event = normalizeIngressEvent(legacy);

        expect(event.externalUserId).toBe('tg-user-42');
        expect(event.externalConversationId).toBe('tg-chat-99');
        expect(event.externalThreadId).toBe('777');
        expect(event.payload.kind).toBe('task');
        expect(event.authContext.transport).toBe('legacy_adapter');
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

        const request = buildBrainStreamChatRequest(event, {
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
