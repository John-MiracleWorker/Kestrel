import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import path from 'path';
import { TelegramAdapter } from '../src/channels/telegram';

describe('TelegramAdapter media handling', () => {
    const originalMediaDir = process.env.MEDIA_DIR;

    beforeEach(() => {
        process.env.MEDIA_DIR = path.join(process.cwd(), 'tmp-media-tests');
    });

    afterEach(() => {
        if (originalMediaDir === undefined) delete process.env.MEDIA_DIR;
        else process.env.MEDIA_DIR = originalMediaDir;
    });

    it('deletes the placeholder instead of rendering raw markdown for media-only replies', async () => {
        const adapter = new TelegramAdapter({
            botToken: 'test-token',
            mode: 'polling',
            defaultWorkspaceId: 'ws-1',
        });
        const api = vi.fn(async () => ({}));
        const sendMediaFile = vi
            .spyOn(adapter as any, 'sendMediaFile')
            .mockResolvedValue(undefined);

        (adapter as any).api = api;

        await adapter.sendStreamEnd(
            {
                messageId: '42',
                chatContext: { chatId: 12345 },
            },
            '![Generated image](/media/example.png)',
        );

        expect(api).toHaveBeenCalledWith('deleteMessage', {
            chat_id: 12345,
            message_id: 42,
        });
        expect(api).not.toHaveBeenCalledWith('editMessageText', expect.anything());
        expect(sendMediaFile).toHaveBeenCalledWith(
            12345,
            expect.objectContaining({
                filePath: path.join(process.env.MEDIA_DIR!, 'example.png'),
                type: 'photo',
            }),
            undefined,
        );
    });

    it('suppresses streaming updates that contain only media markdown', async () => {
        const adapter = new TelegramAdapter({
            botToken: 'test-token',
            mode: 'polling',
            defaultWorkspaceId: 'ws-1',
        });
        const api = vi.fn(async () => ({}));

        (adapter as any).api = api;

        await adapter.sendStreamUpdate(
            {
                messageId: '42',
                chatContext: { chatId: 12345 },
            },
            '![Generated image](/media/example.png)',
        );

        expect(api).not.toHaveBeenCalled();
    });
});
