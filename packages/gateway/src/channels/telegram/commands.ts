import { fetchAndShowOllamaModels } from './models';
import { handleApproval } from './approvals';
import { emitTelegramIngress } from './events';
import { parseTaskRequest } from '../orchestration/intents';

import type { TelegramAdapter } from './index';
import type { TelegramMessage, TelegramUser } from './types';

export async function handleTaskRequest(
    adapter: TelegramAdapter,
    msg: TelegramMessage,
    from: TelegramUser,
    goal: string,
): Promise<void> {
    const chatId = msg.chat.id;
    const threadId = msg.message_thread_id;
    const userId = adapter.resolveUserId(from, chatId);

    adapter.rememberThread(userId, threadId);

    const confirmParams: Record<string, any> = {
        chat_id: chatId,
        text:
            '🦅 *Starting autonomous task...*\n\n' +
            `📋 *Goal:* ${adapter.escapeMarkdown(goal)}\n\n` +
            "_I'll work on this and send you updates._",
        parse_mode: 'Markdown',
    };
    if (threadId !== undefined) {
        confirmParams.message_thread_id = threadId;
    }
    await adapter.api('sendMessage', confirmParams);

    adapter.startTyping(chatId, threadId);
    emitTelegramIngress(adapter, {
        userId,
        from,
        chatId,
        conversationId: adapter.resolveConversationId(msg.chat.id, `task-${Date.now()}`, threadId),
        content: goal,
        threadId,
        channelMessageId: String(msg.message_id),
        timestamp: new Date(msg.date * 1000),
        payloadKind: 'task',
        metadata: {
            isTaskRequest: true,
        },
    });
}

export async function handleCommand(
    adapter: TelegramAdapter,
    msg: TelegramMessage,
    text: string,
): Promise<void> {
    const chatId = msg.chat.id;
    const threadId = msg.message_thread_id;
    const parts = text.split(/\s+/);
    const command = parts[0].replace(/@\w+$/, '');
    const args = parts.slice(1);

    const withThread = (params: Record<string, any>): Record<string, any> => {
        if (threadId !== undefined) {
            params.message_thread_id = threadId;
        }
        return params;
    };

    switch (command) {
        case '/start':
            await adapter.api(
                'sendMessage',
                withThread({
                    chat_id: chatId,
                    text:
                        '🦅 *Welcome to Kestrel\\!*\n\n' +
                        "I'm your autonomous AI agent\\. Here's what I can do:\n\n" +
                        '*💬 Chat Mode*\n' +
                        'Just send me a message to chat\\.\n\n' +
                        '*🤖 Task Mode*\n' +
                        'Start a message with `\\!` to launch an autonomous task:\n' +
                        '`\\!review the auth module for security issues`\n\n' +
                        '*Commands:*\n' +
                        '/help — Show all commands\n' +
                        '/task \\<goal\\> — Start an autonomous task\n' +
                        '/tasks — List active tasks\n' +
                        '/status — System status\n' +
                        '/cancel \\<id\\> — Cancel a task\n' +
                        '/model \\<name\\> — Switch AI model\n' +
                        '/new — Start a new conversation\n' +
                        '/newthread \\[name\\] — Start a new topic thread',
                    parse_mode: 'MarkdownV2',
                }),
            );
            return;

        case '/help':
            await adapter.api(
                'sendMessage',
                withThread({
                    chat_id: chatId,
                    text:
                        '*🦅 Kestrel Commands*\n\n' +
                        '*Communication:*\n' +
                        '  Just type — Chat with the AI\n' +
                        '  `!goal` — Launch autonomous task\n\n' +
                        '*Commands:*\n' +
                        '  /task `<goal>` — Start an autonomous task\n' +
                        '  /tasks — List your active tasks\n' +
                        '  /status — Show system status\n' +
                        '  /cancel `<id>` — Cancel a running task\n' +
                        '  /approve `<id>` — Approve a pending action\n' +
                        '  /reject `<id>` — Reject a pending action\n' +
                        '  /model `<name>` — Switch AI model\n' +
                        '  /workspace — Show current workspace\n' +
                        '  /new — Start a new conversation\n' +
                        '  /newthread `[name]` — Start a new topic thread\n' +
                        '  /stop — Stop current response and typing indicator\n',
                    parse_mode: 'Markdown',
                }),
            );
            return;

        case '/task': {
            const goal = args.join(' ').trim();
            if (!goal) {
                await adapter.api(
                    'sendMessage',
                    withThread({
                        chat_id: chatId,
                        text: '❓ Usage: `/task <your goal>`\n\nExample: `/task review the database schema for performance issues`',
                        parse_mode: 'Markdown',
                    }),
                );
                return;
            }
            if (msg.from) {
                await handleTaskRequest(adapter, msg, msg.from, goal);
            }
            return;
        }

        case '/tasks':
            await adapter.api(
                'sendMessage',
                withThread({
                    chat_id: chatId,
                    text:
                        '📋 *Your Tasks*\n\n' +
                        '_Task listing requires the web dashboard or CLI._\n' +
                        '_Use_ `kestrel tasks` _in the CLI to view all tasks._',
                    parse_mode: 'Markdown',
                }),
            );
            return;

        case '/status':
            await adapter.api(
                'sendMessage',
                withThread({
                    chat_id: chatId,
                    text:
                        '🦅 *Kestrel Status*\n\n' +
                        `✅ Bot: Online\n` +
                        `📡 Mode: ${adapter.config.mode}\n` +
                        `🏢 Workspace: \`${adapter.config.defaultWorkspaceId}\`\n` +
                        `👤 Your ID: \`${msg.from?.id || 'unknown'}\`\n` +
                        `💬 Chat: \`${chatId}\``,
                    parse_mode: 'Markdown',
                }),
            );
            return;

        case '/cancel': {
            const taskId = args[0];
            if (!taskId) {
                await adapter.api(
                    'sendMessage',
                    withThread({
                        chat_id: chatId,
                        text: '❓ Usage: `/cancel <task_id>`',
                        parse_mode: 'Markdown',
                    }),
                );
                return;
            }
            if (msg.from) {
                const userId = adapter.resolveUserId(msg.from, chatId);
                emitTelegramIngress(adapter, {
                    userId,
                    from: msg.from,
                    chatId,
                    conversationId: adapter.resolveConversationId(chatId, undefined, threadId),
                    content: `/cancel ${taskId}`,
                    threadId,
                    channelMessageId: String(msg.message_id),
                    timestamp: new Date(msg.date * 1000),
                    payloadKind: 'command',
                    metadata: {
                        isCommand: true,
                    },
                });
            }
            return;
        }

        case '/approve': {
            const approvalId = args[0];
            if (!approvalId) {
                await adapter.api(
                    'sendMessage',
                    withThread({
                        chat_id: chatId,
                        text: '❓ Usage: `/approve <approval_id>`',
                        parse_mode: 'Markdown',
                    }),
                );
                return;
            }
            const actorUserId = msg.from ? adapter.resolveUserId(msg.from, chatId) : undefined;
            await handleApproval(adapter, chatId, approvalId, true, actorUserId, threadId);
            return;
        }

        case '/reject': {
            const approvalId = args[0];
            if (!approvalId) {
                await adapter.api(
                    'sendMessage',
                    withThread({
                        chat_id: chatId,
                        text: '❓ Usage: `/reject <approval_id>`',
                        parse_mode: 'Markdown',
                    }),
                );
                return;
            }
            const actorUserId = msg.from ? adapter.resolveUserId(msg.from, chatId) : undefined;
            await handleApproval(adapter, chatId, approvalId, false, actorUserId, threadId);
            return;
        }

        case '/model': {
            const modelQuery = args.join(' ').trim();
            if (modelQuery) {
                await adapter.api(
                    'sendMessage',
                    withThread({
                        chat_id: chatId,
                        text: `🔍 Searching for model \`${modelQuery}\`...`,
                        parse_mode: 'Markdown',
                    }),
                );
                adapter.startTyping(chatId, threadId);

                if (msg.from) {
                    const userId = adapter.resolveUserId(msg.from, chatId);
                    adapter.rememberThread(userId, threadId);
                    emitTelegramIngress(adapter, {
                        userId,
                        from: msg.from,
                        chatId,
                        conversationId: adapter.resolveConversationId(chatId, undefined, threadId),
                        content: `Search all available Ollama and cloud models for "${modelQuery}" and switch to the best match. Use the model_swap tool with action="swap" and query="${modelQuery}".`,
                        threadId,
                        channelMessageId: String(msg.message_id),
                        timestamp: new Date(msg.date * 1000),
                        payloadKind: 'command',
                        metadata: {
                            isCommand: true,
                        },
                    });
                }
            } else {
                await fetchAndShowOllamaModels(adapter, chatId, threadId, withThread);
            }
            return;
        }

        case '/workspace':
            await adapter.api(
                'sendMessage',
                withThread({
                    chat_id: chatId,
                    text: `🏢 Current workspace: \`${adapter.config.defaultWorkspaceId}\``,
                    parse_mode: 'Markdown',
                }),
            );
            return;

        case '/pair': {
            if (!msg.from) {
                return;
            }
            const userId = adapter.resolveUserId(msg.from, chatId);
            adapter.rememberThread(userId, threadId);
            await adapter.api(
                'sendMessage',
                withThread({
                    chat_id: chatId,
                    text:
                        '*ðŸ”— Telegram Pairing*\n\n' +
                        `Workspace: \`${adapter.config.defaultWorkspaceId}\`\n` +
                        `Chat ID: \`${chatId}\`\n` +
                        `User ID: \`${userId}\`\n` +
                        `Thread: \`${threadId ?? 'main'}\`\n\n` +
                        'This Telegram chat is now the primary companion surface for this operator session.',
                    parse_mode: 'Markdown',
                }),
            );
            return;
        }

        case '/channels':
            await adapter.api(
                'sendMessage',
                withThread({
                    chat_id: chatId,
                    text:
                        '*ðŸ“¡ Channel Status*\n\n' +
                        `Primary: *Telegram*\n` +
                        `Workspace: \`${adapter.config.defaultWorkspaceId}\`\n` +
                        `Known pairings: \`${adapter.chatIdMap.size}\`\n` +
                        'Companion surfaces: desktop, CLI, and web attach to the same local Kestrel state.',
                    parse_mode: 'Markdown',
                }),
            );
            return;

        case '/doctor': {
            const userId = msg.from ? adapter.resolveUserId(msg.from, chatId) : '';
            const approvals = userId ? await adapter.listPendingApprovalsForUser(userId) : [];
            await adapter.api(
                'sendMessage',
                withThread({
                    chat_id: chatId,
                    text:
                        '*ðŸ©º Telegram Doctor*\n\n' +
                        `Bot: @${adapter.botInfo?.username || 'unknown'}\n` +
                        `Workspace: \`${adapter.config.defaultWorkspaceId}\`\n` +
                        `Pending approvals: \`${approvals.length}\`\n` +
                        `Routing mode: \`${adapter.config.mode}\`\n` +
                        'Media delivery: shared channel artifacts via Gateway.',
                    parse_mode: 'Markdown',
                }),
            );
            return;
        }

        case '/memory':
            await adapter.api(
                'sendMessage',
                withThread({
                    chat_id: chatId,
                    text:
                        '*ðŸ§  Memory Sync*\n\n' +
                        'Kestrel keeps shared markdown memory under `~/.kestrel/memory` and syncs it into the local index. ' +
                        'Use the desktop observer or `kestrel memory` from the CLI to inspect and edit it.',
                    parse_mode: 'Markdown',
                }),
            );
            return;

        case '/canvas':
            await adapter.api(
                'sendMessage',
                withThread({
                    chat_id: chatId,
                    text:
                        '*ðŸ›°ï¸ Flight Deck*\n\n' +
                        'Use the desktop or web observer to inspect task graphs, approvals, artifacts, and live progress while continuing the conversation here in Telegram.',
                    parse_mode: 'Markdown',
                }),
            );
            return;

        case '/new': {
            if (msg.chat.type === 'supergroup' && threadId !== undefined) {
                try {
                    const topic = await adapter.api('createForumTopic', {
                        chat_id: chatId,
                        name: 'New Conversation',
                    });
                    if (msg.from) {
                        const userId = adapter.resolveUserId(msg.from, chatId);
                        adapter.rememberThread(userId, topic.message_thread_id);
                    }
                    await adapter.api('sendMessage', {
                        chat_id: chatId,
                        message_thread_id: topic.message_thread_id,
                        text: '✨ New thread started! Send your first message here.',
                    });
                } catch {
                    await adapter.api(
                        'sendMessage',
                        withThread({
                            chat_id: chatId,
                            text: '✨ New conversation started! Send your first message.',
                        }),
                    );
                }
            } else {
                await adapter.api(
                    'sendMessage',
                    withThread({
                        chat_id: chatId,
                        text: '✨ New conversation started! Send your first message.',
                        parse_mode: 'Markdown',
                    }),
                );
            }
            return;
        }

        case '/newthread': {
            const topicName = (args.join(' ').trim() || 'New Conversation').substring(0, 128);
            if (msg.chat.type === 'supergroup') {
                try {
                    const topic = await adapter.api('createForumTopic', {
                        chat_id: chatId,
                        name: topicName,
                    });
                    if (msg.from) {
                        const userId = adapter.resolveUserId(msg.from, chatId);
                        adapter.rememberThread(userId, topic.message_thread_id);
                    }
                    await adapter.api('sendMessage', {
                        chat_id: chatId,
                        message_thread_id: topic.message_thread_id,
                        text: `✨ *New thread started:* ${adapter.escapeMarkdown(topicName)}\n\nSend your first message here.`,
                        parse_mode: 'Markdown',
                    });
                } catch {
                    await adapter.api(
                        'sendMessage',
                        withThread({
                            chat_id: chatId,
                            text: '✨ New conversation started! Send your first message.',
                        }),
                    );
                }
            } else {
                await adapter.api('sendMessage', {
                    chat_id: chatId,
                    text: '✨ New conversation started! Send your first message.',
                });
            }
            return;
        }

        case '/stop':
            adapter.stopTyping(chatId);
            if (msg.from) {
                const userId = adapter.resolveUserId(msg.from, chatId);
                adapter.cancelActiveStream(userId);
            }
            await adapter.api(
                'sendMessage',
                withThread({
                    chat_id: chatId,
                    text: '⏹ Stopped.',
                }),
            );
            return;

        default:
            await adapter.api(
                'sendMessage',
                withThread({
                    chat_id: chatId,
                    text: `Unknown command: ${command}. Use /help for available commands.`,
                }),
            );
    }
}

export function resolveTaskRequest(text: string): string | null {
    return parseTaskRequest(text);
}
