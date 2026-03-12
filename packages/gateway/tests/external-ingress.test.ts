import { afterEach, describe, expect, it, vi } from 'vitest';
import { TelegramAdapter } from '../src/channels/telegram';
import { processUpdate } from '../src/channels/telegram/handlers';
import { DiscordAdapter } from '../src/channels/discord';
import { handleInteraction } from '../src/channels/discord/handlers';
import { WhatsAppAdapter } from '../src/channels/whatsapp';

describe('external channel ingress normalization', () => {
    afterEach(() => {
        vi.restoreAllMocks();
    });

    it('emits normalized Telegram task ingress with external thread metadata', async () => {
        const adapter = new TelegramAdapter({
            botToken: 'test-token',
            mode: 'polling',
            defaultWorkspaceId: 'ws-1',
        });
        const events: any[] = [];

        adapter.on('message', (event) => events.push(event));
        vi.spyOn(adapter, 'startTyping').mockImplementation(() => {});
        vi.spyOn(adapter, 'api').mockResolvedValue({});

        await processUpdate(adapter, {
            update_id: 1,
            message: {
                message_id: 77,
                message_thread_id: 991,
                date: 1710000000,
                text: '!review the auth module',
                chat: {
                    id: 12345,
                    type: 'supergroup',
                },
                from: {
                    id: 42,
                    first_name: 'Tess',
                    username: 'tess',
                },
            },
        });

        expect(events).toHaveLength(1);
        expect(events[0]).toMatchObject({
            channel: 'telegram',
            payload: {
                kind: 'task',
                content: 'review the auth module',
            },
            authContext: {
                transport: 'telegram_polling',
                isProvisionalUser: false,
            },
            externalUserId: '42',
            externalConversationId: '12345',
            externalThreadId: '991',
        });
        expect(events[0].rawMetadata.isTaskRequest).toBe(true);
    });

    it('emits normalized Discord slash-command ingress for task cancellation', async () => {
        const adapter = new DiscordAdapter({
            botToken: 'bot-token',
            clientId: 'client-id',
            defaultWorkspaceId: 'ws-1',
        });
        const events: any[] = [];

        adapter.on('message', (event) => events.push(event));
        vi.spyOn(adapter, 'respondToInteraction').mockResolvedValue(undefined);

        await handleInteraction(adapter, {
            id: 'interaction-1',
            type: 2,
            data: {
                name: 'cancel',
                options: [{ name: 'task_id', value: 'task-42', type: 3 }],
            },
            channel_id: 'channel-1',
            guild_id: 'guild-1',
            token: 'interaction-token',
            user: {
                id: 'discord-user-1',
                username: 'alice',
                discriminator: '0001',
            },
        });

        expect(events).toHaveLength(1);
        expect(events[0]).toMatchObject({
            channel: 'discord',
            content: '/cancel task-42',
            payload: {
                kind: 'command',
            },
            authContext: {
                transport: 'discord_interaction',
                isProvisionalUser: false,
            },
            externalUserId: 'discord-user-1',
            externalConversationId: 'channel-1',
        });
        expect(events[0].rawMetadata.isCommand).toBe(true);
        expect(events[0].rawMetadata.slashCommandName).toBe('cancel');
    });

    it('emits normalized WhatsApp webhook ingress with attachment metadata', async () => {
        const adapter = new WhatsAppAdapter({
            accountSid: 'AC123',
            authToken: 'token',
            fromNumber: 'whatsapp:+14155238886',
            defaultWorkspaceId: 'ws-1',
        });
        const events: any[] = [];

        adapter.on('message', (event) => events.push(event));

        await adapter.processWebhook({
            From: 'whatsapp:+15551234567',
            Body: 'hello from whatsapp',
            MessageSid: 'SM123',
            NumMedia: '1',
            MediaUrl0: 'https://example.test/media.png',
            MediaContentType0: 'image/png',
        });

        expect(events).toHaveLength(1);
        expect(events[0]).toMatchObject({
            channel: 'whatsapp',
            payload: {
                kind: 'message',
                content: 'hello from whatsapp',
            },
            authContext: {
                transport: 'whatsapp_webhook',
                isProvisionalUser: false,
            },
            externalUserId: '+15551234567',
            externalConversationId: '+15551234567',
        });
        expect(events[0].attachments).toEqual([
            expect.objectContaining({
                type: 'image',
                url: 'https://example.test/media.png',
                mimeType: 'image/png',
            }),
        ]);
    });
});
