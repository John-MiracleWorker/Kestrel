import { Attachment } from '../base';
import { createNormalizedIngressEvent, type NormalizedIngressPayloadKind } from '../ingress';
import { DiscordAdapter } from './index';
import { DiscordInteraction, DiscordMessagePayload } from './types';
import { COLORS } from './constants';
import { parseTaskRequest } from '../orchestration/intents';
import { logger } from '../../utils/logger';

type DiscordIngressSeed = {
    userId: string;
    content: string;
    conversationId: string;
    channelId: string;
    guildId?: string;
    channelMessageId: string;
    username: string;
    externalUserId: string;
    timestamp: Date;
    authTransport: string;
    attachments?: Attachment[];
    payloadKind?: NormalizedIngressPayloadKind;
    externalConversationId?: string;
    externalThreadId?: string;
    metadata?: Record<string, unknown>;
};

function emitDiscordIngress(adapter: DiscordAdapter, seed: DiscordIngressSeed): void {
    const event = createNormalizedIngressEvent({
        channel: 'discord',
        userId: seed.userId,
        workspaceId: adapter.config.defaultWorkspaceId,
        conversationId: seed.conversationId,
        content: seed.content,
        attachments: seed.attachments,
        metadata: {
            channelUserId: seed.externalUserId,
            channelMessageId: seed.channelMessageId,
            timestamp: seed.timestamp,
            discordChannelId: seed.channelId,
            discordGuildId: seed.guildId,
            discordUsername: seed.username,
            ...seed.metadata,
        },
        externalUserId: seed.externalUserId,
        externalConversationId: seed.externalConversationId || seed.channelId,
        externalThreadId: seed.externalThreadId,
        authContext: {
            transport: seed.authTransport,
            authenticatedUserId: seed.userId,
            isProvisionalUser: false,
        },
        payloadKind: seed.payloadKind,
    });

    (adapter as any).emit('message', event);
}

export function handleMessage(adapter: DiscordAdapter, msg: DiscordMessagePayload): void {
    // Ignore bot messages
    if (msg.author.bot) return;

    const text = msg.content;

    // Handle task mode (!goal prefix)
    const taskGoal = parseTaskRequest(text);
    if (taskGoal) {
        handleTaskMessage(adapter, msg, taskGoal);
        return;
    }

    // Start typing indicator
    adapter.startTyping(msg.channel_id);

    const userId = adapter.resolveUserId(msg.author);
    adapter.userChannelMap.set(userId, msg.channel_id);

    const attachments: Attachment[] = (msg.attachments || []).map((att) => {
        let type: Attachment['type'] = 'file';
        if (att.content_type?.startsWith('image/')) type = 'image';
        else if (att.content_type?.startsWith('audio/')) type = 'audio';
        else if (att.content_type?.startsWith('video/')) type = 'video';

        return {
            type,
            url: att.url,
            filename: att.filename,
            mimeType: att.content_type,
            size: att.size,
        };
    });

    emitDiscordIngress(adapter, {
        userId,
        content: text,
        conversationId: `dc-${msg.channel_id}`,
        channelId: msg.channel_id,
        guildId: msg.guild_id,
        channelMessageId: msg.id,
        username: msg.author.username,
        externalUserId: msg.author.id,
        timestamp: new Date(msg.timestamp),
        authTransport: 'discord_gateway',
        attachments: attachments.length ? attachments : undefined,
    });
}

/**
 * Handle a !goal message — create a thread and launch the task.
 */
export async function handleTaskMessage(
    adapter: DiscordAdapter,
    msg: DiscordMessagePayload,
    goal: string,
): Promise<void> {
    const userId = adapter.resolveUserId(msg.author);
    adapter.userChannelMap.set(userId, msg.channel_id);

    // Create a thread for this task
    const threadName = `🦅 Task: ${goal.substring(0, 90)}`;
    try {
        const thread = await adapter.apiRequest(
            'POST',
            `/channels/${msg.channel_id}/messages/${msg.id}/threads`,
            {
                name: threadName,
                auto_archive_duration: 1440, // 24 hours
            },
        );

        const conversationId = `dc-task-${thread.id}`;
        adapter.taskThreads.set(conversationId, thread.id);

        // Send initial message in thread
        await adapter.apiRequest('POST', `/channels/${thread.id}/messages`, {
            embeds: [
                {
                    title: '🦅 Autonomous Task Started',
                    description: goal,
                    color: COLORS.task,
                    fields: [
                        { name: 'Status', value: '⏳ Working...', inline: true },
                        { name: 'Requested By', value: `<@${msg.author.id}>`, inline: true },
                    ],
                    timestamp: new Date().toISOString(),
                },
            ],
        });

        emitDiscordIngress(adapter, {
            userId,
            content: goal,
            conversationId,
            channelId: thread.id,
            guildId: msg.guild_id,
            channelMessageId: msg.id,
            username: msg.author.username,
            externalUserId: msg.author.id,
            timestamp: new Date(msg.timestamp),
            authTransport: 'discord_gateway',
            externalConversationId: msg.channel_id,
            externalThreadId: thread.id,
            payloadKind: 'task',
            metadata: {
                isTaskRequest: true,
                taskThreadId: thread.id,
                discordThreadId: thread.id,
                discordParentChannelId: msg.channel_id,
            },
        });
    } catch (err) {
        // Fallback: no thread, just reply inline
        logger.warn('Could not create Discord thread for task', { error: (err as Error).message });
        await adapter.apiRequest('POST', `/channels/${msg.channel_id}/messages`, {
            embeds: [
                {
                    title: '🦅 Task Started',
                    description: goal,
                    color: COLORS.task,
                },
            ],
        });

        emitDiscordIngress(adapter, {
            userId,
            content: goal,
            conversationId: `dc-${msg.channel_id}`,
            channelId: msg.channel_id,
            guildId: msg.guild_id,
            channelMessageId: msg.id,
            username: msg.author.username,
            externalUserId: msg.author.id,
            timestamp: new Date(msg.timestamp),
            authTransport: 'discord_gateway',
            payloadKind: 'task',
            metadata: {
                isTaskRequest: true,
            },
        });
    }
}

// ── Interaction Handling ────────────────────────────────────────

export async function handleInteraction(
    adapter: DiscordAdapter,
    interaction: DiscordInteraction,
): Promise<void> {
    // Handle button clicks (MESSAGE_COMPONENT)
    if (interaction.type === 3) {
        await handleComponentInteraction(adapter, interaction);
        return;
    }

    // Only handle APPLICATION_COMMAND
    if (interaction.type !== 2) return;

    const user = interaction.member?.user || interaction.user;
    if (!user) return;

    // Access control
    if (!adapter.isAllowed(interaction.member?.roles)) {
        await adapter.respondToInteraction(interaction.id, interaction.token, {
            type: 4,
            data: {
                content: "🔒 Access denied. You don't have the required role to use Kestrel.",
                flags: 64,
            },
        });
        return;
    }

    const commandName = interaction.data?.name;
    const options = interaction.data?.options || [];

    switch (commandName) {
        case 'chat': {
            const messageOpt = options.find((o) => o.name === 'message');
            const content = messageOpt?.value || '';

            // Acknowledge with "thinking"
            await adapter.respondToInteraction(interaction.id, interaction.token, {
                type: 5,
            });

            // Start typing
            adapter.startTyping(interaction.channel_id);

            const userId = adapter.resolveUserId(user);
            adapter.userChannelMap.set(userId, interaction.channel_id);

            emitDiscordIngress(adapter, {
                userId,
                content,
                conversationId: `dc-${interaction.channel_id}`,
                channelId: interaction.channel_id,
                guildId: interaction.guild_id,
                channelMessageId: interaction.id,
                username: user.username,
                externalUserId: user.id,
                timestamp: new Date(),
                authTransport: 'discord_interaction',
                metadata: {
                    interactionToken: interaction.token,
                    isSlashCommand: true,
                    slashCommandName: 'chat',
                },
            });
            break;
        }

        case 'task': {
            const goalOpt = options.find((o) => o.name === 'goal');
            const goal = goalOpt?.value || '';

            // Acknowledge
            await adapter.respondToInteraction(interaction.id, interaction.token, {
                type: 4,
                data: {
                    embeds: [
                        {
                            title: '🦅 Launching Autonomous Task',
                            description: goal,
                            color: COLORS.task,
                            fields: [{ name: 'Status', value: '⏳ Initializing...', inline: true }],
                            timestamp: new Date().toISOString(),
                        },
                    ],
                },
            });

            const userId = adapter.resolveUserId(user);
            adapter.userChannelMap.set(userId, interaction.channel_id);

            emitDiscordIngress(adapter, {
                userId,
                content: goal,
                conversationId: `dc-task-${interaction.channel_id}-${Date.now()}`,
                channelId: interaction.channel_id,
                guildId: interaction.guild_id,
                channelMessageId: interaction.id,
                username: user.username,
                externalUserId: user.id,
                timestamp: new Date(),
                authTransport: 'discord_interaction',
                payloadKind: 'task',
                metadata: {
                    discordChannelId: interaction.channel_id,
                    isTaskRequest: true,
                    interactionToken: interaction.token,
                    slashCommandName: 'task',
                },
            });
            break;
        }

        case 'tasks':
            await adapter.respondToInteraction(interaction.id, interaction.token, {
                type: 4,
                data: {
                    embeds: [
                        {
                            title: '📋 Your Tasks',
                            description:
                                'Task listing is available via the CLI:\n```\nkestrel tasks\n```',
                            color: COLORS.info,
                        },
                    ],
                    flags: 64,
                },
            });
            break;

        case 'status':
            await adapter.respondToInteraction(interaction.id, interaction.token, {
                type: 4,
                data: {
                    embeds: [
                        {
                            title: '🦅 Kestrel Status',
                            color: COLORS.success,
                            fields: [
                                { name: '🟢 Bot', value: 'Online', inline: true },
                                {
                                    name: '🏢 Workspace',
                                    value: `\`${adapter.config.defaultWorkspaceId}\``,
                                    inline: true,
                                },
                                { name: '👤 User', value: `<@${user.id}>`, inline: true },
                                {
                                    name: '📡 Gateway',
                                    value: adapter.sessionId ? 'Connected' : 'Disconnected',
                                    inline: true,
                                },
                            ],
                            timestamp: new Date().toISOString(),
                        },
                    ],
                    flags: 64,
                },
            });
            break;

        case 'cancel': {
            const taskIdOpt = options.find((o) => o.name === 'task_id');
            const taskId = taskIdOpt?.value || '';

            const userId = adapter.resolveUserId(user);
            emitDiscordIngress(adapter, {
                userId,
                content: `/cancel ${taskId}`,
                conversationId: `dc-${interaction.channel_id}`,
                channelId: interaction.channel_id,
                guildId: interaction.guild_id,
                channelMessageId: interaction.id,
                username: user.username,
                externalUserId: user.id,
                timestamp: new Date(),
                authTransport: 'discord_interaction',
                payloadKind: 'command',
                metadata: {
                    interactionToken: interaction.token,
                    isCommand: true,
                    isSlashCommand: true,
                    slashCommandName: 'cancel',
                },
            });

            await adapter.respondToInteraction(interaction.id, interaction.token, {
                type: 4,
                data: {
                    embeds: [
                        {
                            title: '⏹ Cancellation Requested',
                            description: `Task \`${taskId}\` cancellation sent.`,
                            color: COLORS.warning,
                        },
                    ],
                    flags: 64,
                },
            });
            break;
        }

        case 'model': {
            const nameOpt = options.find((o) => o.name === 'name');
            const modelName = nameOpt?.value || '';

            await adapter.respondToInteraction(interaction.id, interaction.token, {
                type: 4,
                data: {
                    embeds: [
                        {
                            title: '🔄 Model Switched',
                            description: `Now using \`${modelName}\``,
                            color: COLORS.info,
                        },
                    ],
                    flags: 64,
                },
            });
            break;
        }

        case 'workspace':
            await adapter.respondToInteraction(interaction.id, interaction.token, {
                type: 4,
                data: {
                    content: `🏢 Current workspace: \`${adapter.config.defaultWorkspaceId}\``,
                    flags: 64,
                },
            });
            break;

        case 'help':
            await adapter.respondToInteraction(interaction.id, interaction.token, {
                type: 4,
                data: {
                    embeds: [
                        {
                            title: '🦅 Kestrel — Autonomous AI Agent',
                            description:
                                'Your AI assistant on Discord. Chat naturally or launch autonomous tasks.',
                            color: COLORS.primary,
                            fields: [
                                {
                                    name: '💬 Chat',
                                    value: '`/chat` or just type in a DM',
                                    inline: false,
                                },
                                {
                                    name: '🤖 Task',
                                    value: '`/task` or `!goal` to launch autonomous agent',
                                    inline: false,
                                },
                                {
                                    name: '📋 Tasks',
                                    value: '`/tasks` — List active tasks',
                                    inline: true,
                                },
                                {
                                    name: '⏹ Cancel',
                                    value: '`/cancel` — Cancel a task',
                                    inline: true,
                                },
                                {
                                    name: '📊 Status',
                                    value: '`/status` — System status',
                                    inline: true,
                                },
                                {
                                    name: '🔄 Model',
                                    value: '`/model` — Switch AI model',
                                    inline: true,
                                },
                                {
                                    name: '🏢 Workspace',
                                    value: '`/workspace` — Show workspace',
                                    inline: true,
                                },
                            ],
                        },
                    ],
                    flags: 64,
                },
            });
            break;
    }
}

// ── Component (Button) Interactions ────────────────────────────

export async function handleComponentInteraction(
    adapter: DiscordAdapter,
    interaction: DiscordInteraction,
): Promise<void> {
    const customId = interaction.data?.custom_id || '';
    const user = interaction.member?.user || interaction.user;

    if (customId.startsWith('approve:')) {
        const approvalId = customId.substring('approve:'.length);
        await adapter.respondToInteraction(interaction.id, interaction.token, {
            type: 4,
            data: {
                embeds: [
                    {
                        title: '✅ Approved',
                        description: `Approval \`${approvalId}\` confirmed by <@${user?.id}>`,
                        color: COLORS.success,
                    },
                ],
            },
        });
        adapter.pendingApprovals.delete(approvalId);
    } else if (customId.startsWith('reject:')) {
        const approvalId = customId.substring('reject:'.length);
        await adapter.respondToInteraction(interaction.id, interaction.token, {
            type: 4,
            data: {
                embeds: [
                    {
                        title: '❌ Rejected',
                        description: `Approval \`${approvalId}\` rejected by <@${user?.id}>`,
                        color: COLORS.error,
                    },
                ],
            },
        });
        adapter.pendingApprovals.delete(approvalId);
    } else {
        // Treat as a generic callback → route as message
        await adapter.respondToInteraction(interaction.id, interaction.token, {
            type: 6, // DEFERRED_UPDATE_MESSAGE
        });

        if (user) {
            const userId = adapter.resolveUserId(user);
            emitDiscordIngress(adapter, {
                userId,
                content: customId,
                conversationId: `dc-${interaction.channel_id}`,
                channelId: interaction.channel_id,
                guildId: interaction.guild_id,
                channelMessageId: interaction.id,
                username: user.username,
                externalUserId: user.id,
                timestamp: new Date(),
                authTransport: 'discord_interaction',
                metadata: {
                    isComponentInteraction: true,
                    interactionToken: interaction.token,
                },
            });
        }
    }
}
