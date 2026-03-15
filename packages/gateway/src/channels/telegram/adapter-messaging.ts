import type { Attachment, OutgoingMessage } from '../base';

import { logger } from '../../utils/logger';

type TelegramAdapterRuntime = any;

export async function sendMessageForUser(
    adapter: TelegramAdapterRuntime,
    userId: string,
    message: OutgoingMessage,
): Promise<void> {
    const chatId = adapter.chatIdMap.get(userId);
    if (!chatId) {
        logger.warn('Cannot send Telegram message — no chat ID for user', { userId });
        return;
    }

    const threadId = adapter.userThreadMap.get(userId);
    await sendToChat(adapter, chatId, message, threadId);
}

export async function sendToChat(
    adapter: TelegramAdapterRuntime,
    chatId: number,
    message: OutgoingMessage,
    threadId?: number,
): Promise<void> {
    adapter.stopTyping(chatId);

    const content = sanitizeForTelegram(adapter, message.content);
    const chunks = chunkMessage(content, 4000);
    for (let index = 0; index < chunks.length; index += 1) {
        const params: Record<string, any> = {
            chat_id: chatId,
            text: chunks[index],
            parse_mode: 'Markdown',
            disable_web_page_preview: true,
        };
        if (threadId !== undefined) {
            params.message_thread_id = threadId;
        }
        if (index === chunks.length - 1 && message.options?.buttons?.length) {
            params.reply_markup = JSON.stringify({
                inline_keyboard: [
                    message.options.buttons.map((button) => ({
                        text: button.label,
                        callback_data: button.action,
                    })),
                ],
            });
        }

        try {
            await adapter.api('sendMessage', params);
        } catch {
            logger.warn('Telegram Markdown parse failed, retrying as plain text');
            delete params.parse_mode;
            await adapter.api('sendMessage', params);
        }
    }

    if (message.attachments?.length) {
        for (const attachment of message.attachments) {
            await sendAttachment(adapter, chatId, attachment, threadId);
        }
    }
}

export function chunkMessage(text: string, maxLength: number): string[] {
    if (text.length <= maxLength) {
        return [text];
    }

    const chunks: string[] = [];
    let remaining = text;
    while (remaining.length > 0) {
        if (remaining.length <= maxLength) {
            chunks.push(remaining);
            break;
        }

        let splitAt = remaining.lastIndexOf('\n', maxLength);
        if (splitAt < maxLength / 2) {
            splitAt = remaining.lastIndexOf('. ', maxLength);
        }
        if (splitAt < maxLength / 2) {
            splitAt = remaining.lastIndexOf(' ', maxLength);
        }
        if (splitAt < maxLength / 2) {
            splitAt = maxLength;
        }

        chunks.push(remaining.substring(0, splitAt));
        remaining = remaining.substring(splitAt).trimStart();
    }

    return chunks;
}

export async function sendAttachment(
    adapter: TelegramAdapterRuntime,
    chatId: number,
    attachment: Attachment,
    threadId?: number,
): Promise<void> {
    const thread = threadId !== undefined ? { message_thread_id: threadId } : {};
    switch (attachment.type) {
        case 'image':
            await adapter.api('sendPhoto', { chat_id: chatId, photo: attachment.url, ...thread });
            return;
        case 'audio':
            await adapter.api('sendAudio', { chat_id: chatId, audio: attachment.url, ...thread });
            return;
        case 'video':
            await adapter.api('sendVideo', { chat_id: chatId, video: attachment.url, ...thread });
            return;
        case 'file':
            await adapter.api('sendDocument', {
                chat_id: chatId,
                document: attachment.url,
                ...thread,
            });
    }
}

export function formatOutgoingMessage(
    _adapter: TelegramAdapterRuntime,
    message: OutgoingMessage,
): OutgoingMessage {
    return message;
}

export function sanitizeForTelegram(_adapter: TelegramAdapterRuntime, text: string): string {
    let result = text;
    result = result.replace(/\*\*(.+?)\*\*/g, '*$1*');
    result = result.replace(/^#{1,6}\s+(.+)$/gm, '*$1*');
    result = result.replace(/^>\s+(.+)$/gm, '│ $1');
    result = result.replace(/^[-*_]{3,}$/gm, '————————');
    result = result.replace(/<[^>]+>/g, '');
    return result;
}

export async function handleAttachment(
    adapter: TelegramAdapterRuntime,
    attachment: Attachment,
): Promise<Attachment> {
    if (attachment.url.startsWith('tg://')) {
        const fileId = attachment.url.replace('tg://', '');
        const file = await adapter.api('getFile', { file_id: fileId });
        attachment.url = `https://api.telegram.org/file/bot${adapter.config.botToken}/${file.file_path}`;
    }
    return attachment;
}
