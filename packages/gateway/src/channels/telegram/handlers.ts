import { randomUUID } from 'crypto';
import { IncomingMessage, Attachment } from '../base';
import { TelegramAdapter } from './index';
import { TelegramUpdate, TelegramMessage, TelegramUser } from './types';
import { logger } from '../../utils/logger';

// ── Webhook Handler ────────────────────────────────────────────

    /**
     * Process an incoming Telegram update (called by the webhook route).
     */
    export async function processUpdate(adapter: TelegramAdapter, update: TelegramUpdate): Promise<void> {
        if (update.callback_query) {
            await handleCallbackQuery(adapter, update.callback_query);
            return;
        }

        if (!update.message) return;

        const msg = update.message;
        const from = msg.from;
        if (!from) return;

        // Access control check
        if (!adapter.isAllowed(from.id)) {
            await adapter.api('sendMessage', {
                chat_id: msg.chat.id,
                text: '🔒 Access denied. You are not authorized to use this bot.',
            });
            return;
        }

        const text = msg.text || msg.caption || '';

        // Handle Telegram-specific commands
        if (text.startsWith('/')) {
            await handleCommand(adapter, msg, text);
            return;
        }

        // Handle task mode (!goal prefix)
        if (text.startsWith('!')) {
            const goal = text.substring(1).trim();
            if (goal) {
                await handleTaskRequest(adapter, msg, from, goal);
                return;
            }
        }

        // ── Check for pending approval text response ────────────────
        // When a user types "I approve" / "yes" / "reject" etc. while
        // there is a pending approval for their chat, resolve it instead
        // of creating a brand-new agent task.
        {
            const APPROVE_KEYWORDS = ['approve', 'approved', 'yes', 'go ahead', 'do it', 'proceed', 'confirm', 'i approve'];
            const DENY_KEYWORDS = ['deny', 'denied', 'reject', 'no', 'cancel', 'stop', 'abort'];
            const textLower = text.toLowerCase().trim();
            const resolvedUser = adapter.resolveUserId(from, msg.chat.id);

            const pendingForChat = [...adapter.pendingApprovals.entries()]
                .filter(([, v]) => v.chatId === msg.chat.id);

            if (pendingForChat.length > 0) {
                const isApproval = APPROVE_KEYWORDS.some(kw => textLower.includes(kw));
                const isDenial = DENY_KEYWORDS.some(kw => textLower.includes(kw));

                if (isApproval || isDenial) {
                    const [approvalId] = pendingForChat[pendingForChat.length - 1];
                    const threadId = msg.message_thread_id;
                    await handleApproval(adapter, msg.chat.id, approvalId, isApproval, resolvedUser, threadId);
                    return;
                }
            }
        }

        const threadId = msg.message_thread_id;

        // Start typing indicator
        adapter.startTyping(msg.chat.id, threadId);

        // Map Telegram user → Kestrel user
        const userId = adapter.resolveUserId(from, msg.chat.id);

        // Record the thread this user is currently active in
        adapter.userThreadMap.set(userId, threadId);

        // Build attachments
        const attachments: Attachment[] = [];
        if (msg.photo?.length) {
            const largest = msg.photo[msg.photo.length - 1];
            attachments.push({
                type: 'image',
                url: `tg://${largest.file_id}`,
                mimeType: 'image/jpeg',
                size: largest.file_size,
            });
        }
        if (msg.document) {
            attachments.push({
                type: 'file',
                url: `tg://${msg.document.file_id}`,
                filename: msg.document.file_name,
                mimeType: msg.document.mime_type,
                size: msg.document.file_size,
            });
        }
        if (msg.voice) {
            attachments.push({
                type: 'audio',
                url: `tg://${msg.voice.file_id}`,
                mimeType: msg.voice.mime_type,
                size: msg.voice.file_size,
            });
        }
        if (msg.audio) {
            attachments.push({
                type: 'audio',
                url: `tg://${msg.audio.file_id}`,
                filename: msg.audio.file_name,
                mimeType: msg.audio.mime_type,
                size: msg.audio.file_size,
            });
        }
        if (msg.video) {
            attachments.push({
                type: 'video',
                url: `tg://${msg.video.file_id}`,
                mimeType: msg.video.mime_type,
                size: msg.video.file_size,
            });
        }

        // Emit normalized message
        const incoming: IncomingMessage = {
            id: randomUUID(),
            channel: 'telegram',
            userId,
            workspaceId: adapter.config.defaultWorkspaceId,
            conversationId: adapter.resolveConversationId(msg.chat.id, undefined, threadId),
            content: text,
            attachments: attachments.length ? attachments : undefined,
            metadata: {
                channelUserId: String(from.id),
                channelMessageId: String(msg.message_id),
                timestamp: new Date(msg.date * 1000),
                telegramChatId: msg.chat.id,
                telegramThreadId: threadId,
                telegramUsername: from.username,
            },
        };

        (adapter as any).emit('message', incoming);
    }

    // ── Task Mode ─────────────────────────────────────────────────

    /**
     * Handle a task request (message starting with !).
     * Sends the goal to the agent as an autonomous task and streams
     * progress updates back to the Telegram chat.
     */
    export async function handleTaskRequest(adapter: TelegramAdapter, msg: TelegramMessage, from: TelegramUser, goal: string): Promise<void> {
        const chatId = msg.chat.id;
        const threadId = msg.message_thread_id;
        const userId = adapter.resolveUserId(from, chatId);

        // Record the thread this user is currently active in
        adapter.userThreadMap.set(userId, threadId);

        const confirmParams: Record<string, any> = {
            chat_id: chatId,
            text:
                '🦅 *Starting autonomous task...*\n\n' +
                `📋 *Goal:* ${adapter.escapeMarkdown(goal)}\n\n` +
                '_I\'ll work on this and send you updates._',
            parse_mode: 'Markdown',
        };
        if (threadId !== undefined) confirmParams.message_thread_id = threadId;
        await adapter.api('sendMessage', confirmParams);

        adapter.startTyping(chatId, threadId);

        // Emit as a task-type message
        const incoming: IncomingMessage = {
            id: randomUUID(),
            channel: 'telegram',
            userId,
            workspaceId: adapter.config.defaultWorkspaceId,
            conversationId: adapter.resolveConversationId(msg.chat.id, `task-${Date.now()}`, threadId),
            content: goal,
            metadata: {
                channelUserId: String(from.id),
                channelMessageId: String(msg.message_id),
                timestamp: new Date(msg.date * 1000),
                telegramChatId: chatId,
                telegramThreadId: threadId,
                telegramUsername: from.username,
                isTaskRequest: true,
            },
        };

        (adapter as any).emit('message', incoming);
    }

    // ── Commands ───────────────────────────────────────────────────

    export async function handleCommand(adapter: TelegramAdapter, msg: TelegramMessage, text: string): Promise<void> {
        const chatId = msg.chat.id;
        const threadId = msg.message_thread_id;
        const parts = text.split(/\s+/);
        const command = parts[0].replace(/@\w+$/, ''); // Strip @botname
        const args = parts.slice(1);

        // Helper to inject message_thread_id into sendMessage params for forum topics
        const withThread = (params: Record<string, any>): Record<string, any> => {
            if (threadId !== undefined) params.message_thread_id = threadId;
            return params;
        };

        switch (command) {
            case '/start':
                await adapter.api('sendMessage', withThread({
                    chat_id: chatId,
                    text:
                        '🦅 *Welcome to Kestrel\\!*\n\n' +
                        'I\'m your autonomous AI agent\\. Here\'s what I can do:\n\n' +
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
                }));
                break;

            case '/help':
                await adapter.api('sendMessage', withThread({
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
                        '  /stop — Stop all typing indicators\n',
                    parse_mode: 'Markdown',
                }));
                break;

            case '/task': {
                const goal = args.join(' ').trim();
                if (!goal) {
                    await adapter.api('sendMessage', withThread({
                        chat_id: chatId,
                        text: '❓ Usage: `/task <your goal>`\n\nExample: `/task review the database schema for performance issues`',
                        parse_mode: 'Markdown',
                    }));
                    return;
                }
                if (msg.from) {
                    await handleTaskRequest(adapter, msg, msg.from, goal);
                }
                break;
            }

            case '/tasks':
                await adapter.api('sendMessage', withThread({
                    chat_id: chatId,
                    text:
                        '📋 *Your Tasks*\n\n' +
                        '_Task listing requires the web dashboard or CLI._\n' +
                        '_Use_ `kestrel tasks` _in the CLI to view all tasks._',
                    parse_mode: 'Markdown',
                }));
                break;

            case '/status':
                await adapter.api('sendMessage', withThread({
                    chat_id: chatId,
                    text:
                        '🦅 *Kestrel Status*\n\n' +
                        `✅ Bot: Online\n` +
                        `📡 Mode: ${adapter.config.mode}\n` +
                        `🏢 Workspace: \`${adapter.config.defaultWorkspaceId}\`\n` +
                        `👤 Your ID: \`${msg.from?.id || 'unknown'}\`\n` +
                        `💬 Chat: \`${chatId}\``,
                    parse_mode: 'Markdown',
                }));
                break;

            case '/cancel': {
                const taskId = args[0];
                if (!taskId) {
                    await adapter.api('sendMessage', withThread({
                        chat_id: chatId,
                        text: '❓ Usage: `/cancel <task_id>`',
                        parse_mode: 'Markdown',
                    }));
                    return;
                }
                // Emit as a cancel command
                if (msg.from) {
                    const userId = adapter.resolveUserId(msg.from, chatId);
                    (adapter as any).emit('message', {
                        id: randomUUID(),
                        channel: 'telegram',
                        userId,
                        workspaceId: adapter.config.defaultWorkspaceId,
                        conversationId: adapter.resolveConversationId(chatId, undefined, threadId),
                        content: `/cancel ${taskId}`,
                        metadata: {
                            channelUserId: String(msg.from.id),
                            channelMessageId: String(msg.message_id),
                            timestamp: new Date(msg.date * 1000),
                            telegramThreadId: threadId,
                            isCommand: true,
                        },
                    });
                }
                break;
            }

            case '/approve': {
                const approvalId = args[0];
                if (!approvalId) {
                    await adapter.api('sendMessage', withThread({
                        chat_id: chatId,
                        text: '❓ Usage: `/approve <approval_id>`',
                        parse_mode: 'Markdown',
                    }));
                    return;
                }
                const actorUserId = msg.from ? adapter.resolveUserId(msg.from, chatId) : undefined;
                await handleApproval(adapter, chatId, approvalId, true, actorUserId, threadId);
                break;
            }

            case '/reject': {
                const rejectId = args[0];
                if (!rejectId) {
                    await adapter.api('sendMessage', withThread({
                        chat_id: chatId,
                        text: '❓ Usage: `/reject <approval_id>`',
                        parse_mode: 'Markdown',
                    }));
                    return;
                }
                const actorUserId = msg.from ? adapter.resolveUserId(msg.from, chatId) : undefined;
                await handleApproval(adapter, chatId, rejectId, false, actorUserId, threadId);
                break;
            }

            case '/model':
                if (args[0]) {
                    await adapter.api('sendMessage', withThread({
                        chat_id: chatId,
                        text: `🔄 Model switched to \`${args[0]}\``,
                        parse_mode: 'Markdown',
                    }));
                } else {
                    await adapter.api('sendMessage', withThread({
                        chat_id: chatId,
                        text: '❓ Usage: `/model <model_name>`\n\nExamples:\n`/model gpt-4o`\n`/model claude-sonnet-4-20250514`\n`/model gemini-2.5-pro`',
                        parse_mode: 'Markdown',
                    }));
                }
                break;

            case '/workspace':
                await adapter.api('sendMessage', withThread({
                    chat_id: chatId,
                    text: `🏢 Current workspace: \`${adapter.config.defaultWorkspaceId}\``,
                    parse_mode: 'Markdown',
                }));
                break;

            case '/new': {
                // In forum supergroups, create a new topic; otherwise just acknowledge
                if (msg.chat.type === 'supergroup' && threadId !== undefined) {
                    try {
                        const topic = await adapter.api('createForumTopic', {
                            chat_id: chatId,
                            name: 'New Conversation',
                        });
                        if (msg.from) {
                            const userId = adapter.resolveUserId(msg.from, chatId);
                            adapter.userThreadMap.set(userId, topic.message_thread_id);
                        }
                        await adapter.api('sendMessage', {
                            chat_id: chatId,
                            message_thread_id: topic.message_thread_id,
                            text: '✨ New thread started! Send your first message here.',
                        });
                    } catch {
                        await adapter.api('sendMessage', withThread({
                            chat_id: chatId,
                            text: '✨ New conversation started! Send your first message.',
                        }));
                    }
                } else {
                    await adapter.api('sendMessage', withThread({
                        chat_id: chatId,
                        text: '✨ New conversation started! Send your first message.',
                        parse_mode: 'Markdown',
                    }));
                }
                break;
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
                            adapter.userThreadMap.set(userId, topic.message_thread_id);
                        }
                        await adapter.api('sendMessage', {
                            chat_id: chatId,
                            message_thread_id: topic.message_thread_id,
                            text: `✨ *New thread started:* ${adapter.escapeMarkdown(topicName)}\n\nSend your first message here.`,
                            parse_mode: 'Markdown',
                        });
                    } catch {
                        await adapter.api('sendMessage', withThread({
                            chat_id: chatId,
                            text: '✨ New conversation started! Send your first message.',
                        }));
                    }
                } else {
                    await adapter.api('sendMessage', {
                        chat_id: chatId,
                        text: '✨ New conversation started! Send your first message.',
                    });
                }
                break;
            }

            case '/stop':
                adapter.stopTyping(chatId);
                await adapter.api('sendMessage', withThread({
                    chat_id: chatId,
                    text: '⏹ Stopped.',
                }));
                break;

            default:
                await adapter.api('sendMessage', withThread({
                    chat_id: chatId,
                    text: `Unknown command: ${command}. Use /help for available commands.`,
                }));
        }
    }

    // ── Approval Handling ──────────────────────────────────────────

    export async function handleApproval(adapter: TelegramAdapter, chatId: number, approvalId: string, approved: boolean, actorUserId?: string, threadId?: number): Promise<void> {
        const icon = approved ? '✅' : '❌';
        const action = approved ? 'Approved' : 'Rejected';

        const result = await adapter.resolvePendingApproval(approvalId, approved, actorUserId);
        if (!result.success) {
            const errParams: Record<string, any> = {
                chat_id: chatId,
                text: `⚠️ Could not process approval \`${approvalId}\`: ${adapter.escapeMarkdown(result.error || 'unknown error')}`,
                parse_mode: 'Markdown',
            };
            if (threadId !== undefined) errParams.message_thread_id = threadId;
            await adapter.api('sendMessage', errParams);
            return;
        }

        const params: Record<string, any> = {
            chat_id: chatId,
            text: `${icon} *${action}* approval \`${approvalId}\``,
            parse_mode: 'Markdown',
        };
        if (threadId !== undefined) params.message_thread_id = threadId;
        await adapter.api('sendMessage', params);
    }

    /**
     * Send an approval request to a Telegram chat with inline buttons.
     */
    export async function sendApprovalRequest(adapter: TelegramAdapter, chatId: number, approvalId: string, description: string, taskId: string, userId: string, threadId?: number): Promise<void> {
        adapter.pendingApprovals.set(approvalId, { taskId, chatId, userId, threadId });

        const params: Record<string, any> = {
            chat_id: chatId,
            text:
                '⚠️ *Approval Required*\n\n' +
                `${description}\n\n` +
                `ID: \`${approvalId}\``,
            parse_mode: 'Markdown',
            reply_markup: JSON.stringify({
                inline_keyboard: [[
                    { text: '✅ Approve', callback_data: `approve:${approvalId}` },
                    { text: '❌ Reject', callback_data: `reject:${approvalId}` },
                ]],
            }),
        };
        if (threadId !== undefined) params.message_thread_id = threadId;
        await adapter.api('sendMessage', params);
    }

    /**
     * Send a progress update for an active task.
     */
    export async function sendTaskProgress(adapter: TelegramAdapter, chatId: number, step: string, detail: string = '', threadId?: number): Promise<void> {
        const text = detail
            ? `🔧 *${adapter.escapeMarkdown(step)}*\n${adapter.escapeMarkdown(detail)}`
            : `🔧 ${adapter.escapeMarkdown(step)}`;

        const params: Record<string, any> = {
            chat_id: chatId,
            text,
            parse_mode: 'Markdown',
            disable_notification: true,  // Silent for progress updates
        };
        if (threadId !== undefined) params.message_thread_id = threadId;
        await adapter.api('sendMessage', params);
    }

    // ── Callback Queries ───────────────────────────────────────────

    export async function handleCallbackQuery(adapter: TelegramAdapter, query: {
        id: string;
        from: TelegramUser;
        message?: TelegramMessage;
        data?: string;
    }): Promise<void> {
        // Acknowledge the callback
        await adapter.api('answerCallbackQuery', { callback_query_id: query.id });

        if (!query.data || !query.message) return;
        const chatId = query.message.chat.id;
        // Extract thread context from the button's parent message
        const threadId = query.message.message_thread_id;

        // Handle self-improvement callbacks (si_approve / si_deny)
        if (query.data.startsWith('si_approve:') || query.data.startsWith('si_deny:')) {
            const isApprove = query.data.startsWith('si_approve:');
            const proposalId = query.data.substring(query.data.indexOf(':') + 1);
            const action = isApprove ? 'approve' : 'deny';
            const icon = isApprove ? '✅' : '❌';

            const processingParams: Record<string, any> = {
                chat_id: chatId,
                text: `${icon} Processing ${action} for proposal \`${proposalId.substring(0, 8)}...\``,
                parse_mode: 'Markdown',
            };
            if (threadId !== undefined) processingParams.message_thread_id = threadId;
            await adapter.api('sendMessage', processingParams);

            try {
                // Call brain's self_improve handler via docker exec
                const { execSync } = await import('child_process');
                const cmd = `docker exec littlebirdalt-brain-1 python3 -c "
import sys, json
sys.path.insert(0, '/app')
from agent.tools.self_improve import _handle_approval
result = _handle_approval('${proposalId}', approved=${isApprove ? 'True' : 'False'})
print(json.dumps(result))
"`;
                const output = execSync(cmd, { timeout: 15000, encoding: 'utf-8' }).trim();
                const result = JSON.parse(output);

                if (result.error) {
                    const errParams: Record<string, any> = { chat_id: chatId, text: `⚠️ ${result.error}` };
                    if (threadId !== undefined) errParams.message_thread_id = threadId;
                    await adapter.api('sendMessage', errParams);
                } else {
                    const resultParams: Record<string, any> = {
                        chat_id: chatId,
                        text: `${icon} *${result.status === 'approved' ? 'Approved' : 'Denied'}*\n${result.message || ''}`,
                        parse_mode: 'Markdown',
                    };
                    if (threadId !== undefined) resultParams.message_thread_id = threadId;
                    await adapter.api('sendMessage', resultParams);
                }
            } catch (err) {
                logger.error('Self-improve callback failed', { error: (err as Error).message });
                const failParams: Record<string, any> = {
                    chat_id: chatId,
                    text: `⚠️ Failed to process: ${(err as Error).message?.substring(0, 200)}`,
                };
                if (threadId !== undefined) failParams.message_thread_id = threadId;
                await adapter.api('sendMessage', failParams);
            }
            return;
        }

        // Handle approval callbacks
        if (query.data.startsWith('approve:')) {
            const approvalId = query.data.substring('approve:'.length);
            const actorUserId = adapter.resolveUserId(query.from, chatId);
            await handleApproval(adapter, chatId, approvalId, true, actorUserId, threadId);
            return;
        }
        if (query.data.startsWith('reject:')) {
            const approvalId = query.data.substring('reject:'.length);
            const actorUserId = adapter.resolveUserId(query.from, chatId);
            await handleApproval(adapter, chatId, approvalId, false, actorUserId, threadId);
            return;
        }

        // Treat other callback data as a message
        const userId = adapter.resolveUserId(query.from, chatId);
        const incoming: IncomingMessage = {
            id: randomUUID(),
            channel: 'telegram',
            userId,
            workspaceId: adapter.config.defaultWorkspaceId,
            conversationId: adapter.resolveConversationId(chatId, undefined, threadId),
            content: query.data,
            metadata: {
                channelUserId: String(query.from.id),
                channelMessageId: String(query.message.message_id),
                timestamp: new Date(),
                telegramThreadId: threadId,
                isCallbackQuery: true,
            },
        };

        (adapter as any).emit('message', incoming);
    }
