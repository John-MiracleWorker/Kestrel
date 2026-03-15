import { existsSync } from 'fs';
import path from 'path';

import type { StreamHandle, ToolActivity } from '../base';

import { logger } from '../../utils/logger';
import { sendToChat, sanitizeForTelegram } from './adapter-messaging';

type TelegramAdapterRuntime = any;

export async function sendStreamStart(
    adapter: TelegramAdapterRuntime,
    userId: string,
    _meta: { conversationId: string },
): Promise<StreamHandle> {
    const chatId = adapter.chatIdMap.get(userId);
    if (!chatId) {
        throw new Error(`No chat ID mapped for user ${userId}`);
    }

    const threadId = adapter.userThreadMap.get(userId);
    adapter.lastHeartbeatAt.delete(chatId);

    const params: Record<string, any> = { chat_id: chatId, text: '🤔 Thinking...' };
    if (threadId !== undefined) {
        params.message_thread_id = threadId;
    }
    const result = await adapter.api('sendMessage', params);

    adapter.startTyping(chatId, threadId);
    return {
        messageId: String(result.message_id),
        chatContext: { chatId, threadId },
    };
}

export async function sendStreamUpdate(
    adapter: TelegramAdapterRuntime,
    handle: StreamHandle,
    accumulatedContent: string,
): Promise<void> {
    const chatId = handle.chatContext.chatId as number;
    const { textContent, mediaFiles } = extractMediaFromMarkdown(adapter, accumulatedContent);
    if (mediaFiles.length > 0 && !textContent.trim()) {
        return;
    }
    const contentToRender = textContent.trim() ? textContent : accumulatedContent;
    if (!contentToRender.trim()) {
        return;
    }

    const sanitized = sanitizeForTelegram(adapter, contentToRender);
    const display = sanitized.length > 4000 ? sanitized.slice(-4000) + '▌' : sanitized + ' ▌';
    try {
        await adapter.api('editMessageText', {
            chat_id: chatId,
            message_id: Number(handle.messageId),
            text: display,
            parse_mode: 'Markdown',
            disable_web_page_preview: true,
        });
    } catch (error) {
        const message = (error as Error).message || '';
        if (!message.includes('message is not modified')) {
            try {
                await adapter.api('editMessageText', {
                    chat_id: chatId,
                    message_id: Number(handle.messageId),
                    text: display.replace(/[_*[\]()~`>#+\-=|{}.!\\]/g, ''),
                });
            } catch {
                // Best effort only for partial updates.
            }
        }
    }
}

export function extractMediaFromMarkdown(
    adapter: TelegramAdapterRuntime,
    content: string,
): {
    textContent: string;
    mediaFiles: Array<{ alt: string; filePath: string; type: 'photo' | 'video' }>;
} {
    const mediaFiles: Array<{ alt: string; filePath: string; type: 'photo' | 'video' }> = [];
    const videoExts = ['.mp4', '.webm', '.mov'];
    const mediaRegex = /!\[([^\]]*)\]\((\/media\/[^)]+)\)/g;

    const textContent = content
        .replace(mediaRegex, (_match, alt: string, url: string) => {
            const encodedName = url.replace('/media/', '');
            const filename = path.basename(decodeURIComponent(encodedName));
            const filePath = path.join(adapter.mediaDir, filename);
            const ext = path.extname(filename).toLowerCase();
            const type = videoExts.includes(ext) ? 'video' : 'photo';
            mediaFiles.push({ alt: alt || 'Generated media', filePath, type });
            return '';
        })
        .replace(/\n{3,}/g, '\n\n')
        .trim();

    return { textContent, mediaFiles };
}

export async function sendMediaFile(
    adapter: TelegramAdapterRuntime,
    chatId: number,
    media: { alt: string; filePath: string; type: 'photo' | 'video' },
    threadId?: number,
): Promise<void> {
    if (!existsSync(media.filePath)) {
        logger.warn('Media file not found for Telegram upload', { path: media.filePath });
        return;
    }

    const dedupeKey = `${chatId}:${media.filePath}`;
    if (adapter.sentMedia.has(dedupeKey)) {
        logger.info('Media already sent, skipping duplicate', { path: media.filePath });
        return;
    }
    adapter.sentMedia.add(dedupeKey);

    const method = media.type === 'video' ? 'sendVideo' : 'sendDocument';
    const fieldName = media.type === 'video' ? 'video' : 'document';
    const url = `${adapter.apiBase}/${method}`;

    try {
        const formData = new FormData();
        const { readFileSync } = await import('fs');
        const fileBuffer = readFileSync(media.filePath);
        const ext = path.extname(media.filePath).toLowerCase();
        const mimeMap: Record<string, string> = {
            '.png': 'image/png',
            '.jpg': 'image/jpeg',
            '.jpeg': 'image/jpeg',
            '.webp': 'image/webp',
            '.gif': 'image/gif',
            '.mp4': 'video/mp4',
            '.webm': 'video/webm',
            '.mov': 'video/quicktime',
        };
        const blob = new Blob([fileBuffer], { type: mimeMap[ext] || 'application/octet-stream' });
        formData.append(fieldName, blob, path.basename(media.filePath));
        formData.append('chat_id', String(chatId));
        if (media.alt) {
            formData.append('caption', media.alt);
        }
        if (threadId !== undefined) {
            formData.append('message_thread_id', String(threadId));
        }

        const response = await fetch(url, { method: 'POST', body: formData });
        const data = (await response.json()) as { ok: boolean; description?: string };
        if (!data.ok) {
            logger.warn('Telegram media upload failed', {
                error: data.description,
                path: media.filePath,
            });
        }
    } catch (error) {
        logger.warn('Failed to send media to Telegram', {
            error: (error as Error).message,
            path: media.filePath,
        });
    }
}

export async function sendStreamEnd(
    adapter: TelegramAdapterRuntime,
    handle: StreamHandle,
    finalContent: string,
): Promise<void> {
    const chatId = handle.chatContext.chatId as number;
    const threadId = handle.chatContext.threadId as number | undefined;
    adapter.lastHeartbeatAt.delete(chatId);
    adapter.stopTyping(chatId);

    const { textContent, mediaFiles } = extractMediaFromMarkdown(adapter, finalContent);
    const sanitizedFinal = sanitizeForTelegram(adapter, textContent);

    if (sanitizedFinal.length <= 4000 && sanitizedFinal.trim()) {
        try {
            await adapter.api('editMessageText', {
                chat_id: chatId,
                message_id: Number(handle.messageId),
                text: sanitizedFinal,
                parse_mode: 'Markdown',
                disable_web_page_preview: true,
            });
        } catch {
            try {
                await adapter.api('editMessageText', {
                    chat_id: chatId,
                    message_id: Number(handle.messageId),
                    text: textContent,
                });
            } catch {
                // Best effort only for finalization fallback.
            }
        }
    } else if (sanitizedFinal.trim()) {
        try {
            await adapter.api('deleteMessage', {
                chat_id: chatId,
                message_id: Number(handle.messageId),
            });
        } catch {
            // Ignore placeholder cleanup failures.
        }

        await sendToChat(
            adapter,
            chatId,
            { conversationId: '', content: textContent, options: { markdown: true } },
            threadId,
        );
    } else {
        try {
            await adapter.api('deleteMessage', {
                chat_id: chatId,
                message_id: Number(handle.messageId),
            });
        } catch {
            // Ignore placeholder cleanup failures.
        }
    }

    for (const media of mediaFiles) {
        await sendMediaFile(adapter, chatId, media, threadId);
    }
}

export async function sendToolActivity(
    adapter: TelegramAdapterRuntime,
    _userId: string,
    handle: StreamHandle,
    activity: ToolActivity,
): Promise<void> {
    const chatId = handle.chatContext.chatId as number;
    const threadId = handle.chatContext.threadId as number | undefined;

    if (activity.status !== 'thinking') {
        return;
    }

    const thinkingText = activity.thinking || '';
    if (!thinkingText.startsWith('Still working...')) {
        return;
    }
    const now = Date.now();
    const lastHeartbeat = adapter.lastHeartbeatAt.get(chatId) || 0;
    if (now - lastHeartbeat < 300000) {
        return;
    }
    adapter.lastHeartbeatAt.set(chatId, now);

    try {
        const params: Record<string, any> = {
            chat_id: chatId,
            text: `⏳ ${thinkingText}`,
            parse_mode: 'Markdown',
            disable_notification: true,
        };
        if (threadId !== undefined) {
            params.message_thread_id = threadId;
        }
        await adapter.api('sendMessage', params);
    } catch {
        // Best effort heartbeat only.
    }
}
