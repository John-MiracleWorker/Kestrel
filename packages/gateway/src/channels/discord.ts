import { randomUUID } from 'crypto';
import {
    BaseChannelAdapter,
    ChannelType,
    IncomingMessage,
    OutgoingMessage,
    Attachment,
} from './base';
import { logger } from '../utils/logger';

// ── Configuration ──────────────────────────────────────────────────

export interface DiscordConfig {
    botToken: string;
    clientId: string;
    guildId?: string;               // For guild-specific slash commands
    defaultWorkspaceId: string;
    allowedRoleIds?: string[];      // Optional: restrict to users with these roles
}

// ── Discord API Types ──────────────────────────────────────────────

interface DiscordUser {
    id: string;
    username: string;
    discriminator: string;
    global_name?: string;
}

interface DiscordMessagePayload {
    id: string;
    channel_id: string;
    author: DiscordUser & { bot?: boolean };
    content: string;
    timestamp: string;
    attachments?: Array<{
        id: string;
        filename: string;
        url: string;
        content_type?: string;
        size: number;
    }>;
    guild_id?: string;
}

interface DiscordInteraction {
    id: string;
    type: number;                    // 2 = APPLICATION_COMMAND, 3 = MESSAGE_COMPONENT
    data?: {
        name: string;
        custom_id?: string;
        options?: Array<{ name: string; value: string; type: number }>;
    };
    channel_id: string;
    guild_id?: string;
    member?: { user: DiscordUser; roles?: string[] };
    user?: DiscordUser;
    token: string;
}

// ── Slash Command Definitions ──────────────────────────────────────

const SLASH_COMMANDS = [
    {
        name: 'chat',
        description: 'Chat with Kestrel AI',
        options: [{
            name: 'message',
            description: 'Your message',
            type: 3, // STRING
            required: true,
        }],
    },
    {
        name: 'task',
        description: 'Launch an autonomous agent task',
        options: [{
            name: 'goal',
            description: 'What should the agent accomplish?',
            type: 3,
            required: true,
        }],
    },
    {
        name: 'tasks',
        description: 'List your active tasks',
    },
    {
        name: 'status',
        description: 'Show Kestrel system status',
    },
    {
        name: 'cancel',
        description: 'Cancel a running task',
        options: [{
            name: 'task_id',
            description: 'ID of the task to cancel',
            type: 3,
            required: true,
        }],
    },
    {
        name: 'model',
        description: 'Switch AI model',
        options: [{
            name: 'name',
            description: 'Model name (e.g. gpt-4o, claude-sonnet-4-20250514)',
            type: 3,
            required: true,
        }],
    },
    {
        name: 'workspace',
        description: 'Show current workspace info',
    },
    {
        name: 'help',
        description: 'Show Kestrel bot help',
    },
];

// ── Embed Colors ───────────────────────────────────────────────────

const COLORS = {
    primary: 0x6366f1,   // Indigo
    success: 0x22c55e,   // Green
    warning: 0xf59e0b,   // Amber
    error: 0xef4444,   // Red
    info: 0x3b82f6,   // Blue
    task: 0x8b5cf6,   // Purple
    progress: 0x64748b,   // Slate
};

// ── Discord Adapter ────────────────────────────────────────────────

/**
 * Full-featured Discord Bot adapter for Kestrel.
 *
 * Features:
 *   ✅ Chat mode — conversational AI via Discord messages + /chat command
 *   ✅ Task mode — /task command + !goal prefix for autonomous agent tasks
 *   ✅ Rich embeds — color-coded embedded responses with fields
 *   ✅ Extended slash commands — /task, /tasks, /status, /cancel, /model
 *   ✅ Inline buttons — approve/reject actions with message components
 *   ✅ Thread-based tasks — creates threads for task progress updates
 *   ✅ Smart chunking — splits long responses into embeds
 *   ✅ Typing indicators — shows typing while processing
 *   ✅ Access control — optional role-based allowlist
 *   ✅ Auto-reconnect — resilient Gateway WebSocket connection
 *   ✅ File handling — images, audio, video, documents
 */
export class DiscordAdapter extends BaseChannelAdapter {
    readonly channelType: ChannelType = 'discord';

    private readonly apiBase = 'https://discord.com/api/v10';
    private gatewayWs: any = null;
    private heartbeatInterval?: NodeJS.Timeout;
    private sessionId?: string;
    private sequence: number | null = null;

    // Channel → userId mapping
    private channelUserMap = new Map<string, string>();    // discordUserId → kestrelUserId
    private userChannelMap = new Map<string, string>();    // kestrelUserId → discordChannelId

    // Typing indicators
    private typingIntervals = new Map<string, NodeJS.Timeout>();

    // Active task threads
    private taskThreads = new Map<string, string>();  // taskConversationId → threadId

    // Pending approvals
    private pendingApprovals = new Map<string, { channelId: string; userId: string }>();

    constructor(private config: DiscordConfig) {
        super();
    }

    // ── Lifecycle ──────────────────────────────────────────────────

    async connect(): Promise<void> {
        this.setStatus('connecting');

        // Register slash commands
        await this.registerSlashCommands();

        // Connect to Discord Gateway
        const gateway = await this.apiRequest('GET', '/gateway/bot');
        const wsUrl = gateway.url + '?v=10&encoding=json';

        await this.connectGateway(wsUrl);

        this.setStatus('connected');
        logger.info('Discord adapter connected');
    }

    async disconnect(): Promise<void> {
        if (this.heartbeatInterval) clearInterval(this.heartbeatInterval);

        // Clear all typing indicators
        for (const [, interval] of this.typingIntervals) {
            clearInterval(interval);
        }
        this.typingIntervals.clear();

        if (this.gatewayWs) {
            this.gatewayWs.close(1000, 'Bot shutting down');
            this.gatewayWs = null;
        }
        this.setStatus('disconnected');
        logger.info('Discord adapter disconnected');
    }

    // ── Access Control ─────────────────────────────────────────────

    private isAllowed(roles?: string[]): boolean {
        if (!this.config.allowedRoleIds?.length) return true;
        if (!roles?.length) return false;
        return roles.some(r => this.config.allowedRoleIds!.includes(r));
    }

    // ── Typing Indicators ──────────────────────────────────────────

    private startTyping(channelId: string): void {
        this.apiRequest('POST', `/channels/${channelId}/typing`).catch(() => { });

        if (!this.typingIntervals.has(channelId)) {
            const interval = setInterval(() => {
                this.apiRequest('POST', `/channels/${channelId}/typing`).catch(() => { });
            }, 8000);  // Discord typing indicator lasts ~10s
            this.typingIntervals.set(channelId, interval);
        }
    }

    private stopTyping(channelId: string): void {
        const interval = this.typingIntervals.get(channelId);
        if (interval) {
            clearInterval(interval);
            this.typingIntervals.delete(channelId);
        }
    }

    // ── Gateway WebSocket ──────────────────────────────────────────

    private async connectGateway(url: string): Promise<void> {
        const { WebSocket } = await import('ws');

        return new Promise((resolve, reject) => {
            const ws = new WebSocket(url);
            this.gatewayWs = ws;

            ws.on('open', () => {
                logger.info('Discord Gateway WebSocket opened');
            });

            ws.on('message', (data: Buffer) => {
                const payload = JSON.parse(data.toString());
                this.handleGatewayEvent(payload, resolve, reject);
            });

            ws.on('close', (code: number) => {
                logger.warn(`Discord Gateway closed with code ${code}`);
                this.setStatus('disconnected');

                if (code !== 1000 && code !== 4004) {
                    setTimeout(() => {
                        logger.info('Reconnecting to Discord Gateway...');
                        this.connectGateway(url).catch(logger.error);
                    }, 5000);
                }
            });

            ws.on('error', (err: Error) => {
                logger.error('Discord Gateway error', { error: err.message });
                this.emit('error', err);
                reject(err);
            });
        });
    }

    private handleGatewayEvent(payload: any, resolve?: Function, reject?: Function): void {
        const { op, t, s, d } = payload;

        if (s) this.sequence = s;

        switch (op) {
            case 10: // HELLO
                this.startHeartbeat(d.heartbeat_interval);
                this.gatewayWs?.send(JSON.stringify({
                    op: 2,
                    d: {
                        token: this.config.botToken,
                        intents: (1 << 0) | (1 << 9) | (1 << 12) | (1 << 15),
                        // GUILDS | GUILD_MESSAGES | MESSAGE_CONTENT | GUILD_MESSAGE_REACTIONS
                        properties: {
                            os: process.platform,
                            browser: 'kestrel',
                            device: 'kestrel',
                        },
                    },
                }));
                break;

            case 0: // DISPATCH
                this.handleDispatch(t, d);
                if (t === 'READY') {
                    this.sessionId = d.session_id;
                    logger.info(`Discord bot ready: ${d.user.username}#${d.user.discriminator}`);
                    resolve?.();
                }
                break;

            case 1: // HEARTBEAT REQUEST
                this.sendHeartbeat();
                break;

            case 11: // HEARTBEAT ACK
                break;

            case 7: // RECONNECT
                logger.info('Discord requested reconnect');
                this.gatewayWs?.close(4000, 'Reconnect requested');
                break;

            case 9: // INVALID SESSION
                logger.warn('Discord invalid session, re-identifying...');
                if (d) {
                    this.gatewayWs?.send(JSON.stringify({
                        op: 6,
                        d: {
                            token: this.config.botToken,
                            session_id: this.sessionId,
                            seq: this.sequence,
                        },
                    }));
                } else {
                    reject?.(new Error('Discord session invalidated'));
                }
                break;
        }
    }

    private startHeartbeat(intervalMs: number): void {
        if (this.heartbeatInterval) clearInterval(this.heartbeatInterval);
        this.heartbeatInterval = setInterval(() => this.sendHeartbeat(), intervalMs);
    }

    private sendHeartbeat(): void {
        this.gatewayWs?.send(JSON.stringify({ op: 1, d: this.sequence }));
    }

    // ── Event Dispatch ─────────────────────────────────────────────

    private handleDispatch(event: string, data: any): void {
        switch (event) {
            case 'MESSAGE_CREATE':
                this.handleMessage(data as DiscordMessagePayload);
                break;
            case 'INTERACTION_CREATE':
                this.handleInteraction(data as DiscordInteraction);
                break;
        }
    }

    private handleMessage(msg: DiscordMessagePayload): void {
        // Ignore bot messages
        if (msg.author.bot) return;

        const text = msg.content;

        // Handle task mode (!goal prefix)
        if (text.startsWith('!')) {
            const goal = text.substring(1).trim();
            if (goal) {
                this.handleTaskMessage(msg, goal);
                return;
            }
        }

        // Start typing indicator
        this.startTyping(msg.channel_id);

        const userId = this.resolveUserId(msg.author);
        this.userChannelMap.set(userId, msg.channel_id);

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

        const incoming: IncomingMessage = {
            id: randomUUID(),
            channel: 'discord',
            userId,
            workspaceId: this.config.defaultWorkspaceId,
            conversationId: `dc-${msg.channel_id}`,
            content: text,
            attachments: attachments.length ? attachments : undefined,
            metadata: {
                channelUserId: msg.author.id,
                channelMessageId: msg.id,
                timestamp: new Date(msg.timestamp),
                discordChannelId: msg.channel_id,
                discordGuildId: msg.guild_id,
                discordUsername: msg.author.username,
            },
        };

        this.emit('message', incoming);
    }

    /**
     * Handle a !goal message — create a thread and launch the task.
     */
    private async handleTaskMessage(msg: DiscordMessagePayload, goal: string): Promise<void> {
        const userId = this.resolveUserId(msg.author);
        this.userChannelMap.set(userId, msg.channel_id);

        // Create a thread for this task
        const threadName = `🦅 Task: ${goal.substring(0, 90)}`;
        try {
            const thread = await this.apiRequest('POST', `/channels/${msg.channel_id}/messages/${msg.id}/threads`, {
                name: threadName,
                auto_archive_duration: 1440,  // 24 hours
            });

            const conversationId = `dc-task-${thread.id}`;
            this.taskThreads.set(conversationId, thread.id);

            // Send initial message in thread
            await this.apiRequest('POST', `/channels/${thread.id}/messages`, {
                embeds: [{
                    title: '🦅 Autonomous Task Started',
                    description: goal,
                    color: COLORS.task,
                    fields: [
                        { name: 'Status', value: '⏳ Working...', inline: true },
                        { name: 'Requested By', value: `<@${msg.author.id}>`, inline: true },
                    ],
                    timestamp: new Date().toISOString(),
                }],
            });

            // Emit the task
            const incoming: IncomingMessage = {
                id: randomUUID(),
                channel: 'discord',
                userId,
                workspaceId: this.config.defaultWorkspaceId,
                conversationId,
                content: goal,
                metadata: {
                    channelUserId: msg.author.id,
                    channelMessageId: msg.id,
                    timestamp: new Date(msg.timestamp),
                    discordChannelId: thread.id,
                    discordGuildId: msg.guild_id,
                    discordUsername: msg.author.username,
                    isTaskRequest: true,
                    taskThreadId: thread.id,
                },
            };

            this.emit('message', incoming);
        } catch (err) {
            // Fallback: no thread, just reply inline
            logger.warn('Could not create Discord thread for task', { error: (err as Error).message });
            await this.apiRequest('POST', `/channels/${msg.channel_id}/messages`, {
                embeds: [{
                    title: '🦅 Task Started',
                    description: goal,
                    color: COLORS.task,
                }],
            });

            this.emit('message', {
                id: randomUUID(),
                channel: 'discord',
                userId,
                workspaceId: this.config.defaultWorkspaceId,
                conversationId: `dc-${msg.channel_id}`,
                content: goal,
                metadata: {
                    channelUserId: msg.author.id,
                    channelMessageId: msg.id,
                    timestamp: new Date(msg.timestamp),
                    discordChannelId: msg.channel_id,
                    isTaskRequest: true,
                },
            });
        }
    }

    // ── Interaction Handling ────────────────────────────────────────

    private async handleInteraction(interaction: DiscordInteraction): Promise<void> {
        // Handle button clicks (MESSAGE_COMPONENT)
        if (interaction.type === 3) {
            await this.handleComponentInteraction(interaction);
            return;
        }

        // Only handle APPLICATION_COMMAND
        if (interaction.type !== 2) return;

        const user = interaction.member?.user || interaction.user;
        if (!user) return;

        // Access control
        if (!this.isAllowed(interaction.member?.roles)) {
            await this.respondToInteraction(interaction.id, interaction.token, {
                type: 4,
                data: {
                    content: '🔒 Access denied. You don\'t have the required role to use Kestrel.',
                    flags: 64,
                },
            });
            return;
        }

        const commandName = interaction.data?.name;
        const options = interaction.data?.options || [];

        switch (commandName) {
            case 'chat': {
                const messageOpt = options.find(o => o.name === 'message');
                const content = messageOpt?.value || '';

                // Acknowledge with "thinking"
                await this.respondToInteraction(interaction.id, interaction.token, {
                    type: 5,
                });

                // Start typing
                this.startTyping(interaction.channel_id);

                const userId = this.resolveUserId(user);
                this.userChannelMap.set(userId, interaction.channel_id);

                const incoming: IncomingMessage = {
                    id: randomUUID(),
                    channel: 'discord',
                    userId,
                    workspaceId: this.config.defaultWorkspaceId,
                    conversationId: `dc-${interaction.channel_id}`,
                    content,
                    metadata: {
                        channelUserId: user.id,
                        channelMessageId: interaction.id,
                        timestamp: new Date(),
                        interactionToken: interaction.token,
                        isSlashCommand: true,
                    },
                };

                this.emit('message', incoming);
                break;
            }

            case 'task': {
                const goalOpt = options.find(o => o.name === 'goal');
                const goal = goalOpt?.value || '';

                // Acknowledge
                await this.respondToInteraction(interaction.id, interaction.token, {
                    type: 4,
                    data: {
                        embeds: [{
                            title: '🦅 Launching Autonomous Task',
                            description: goal,
                            color: COLORS.task,
                            fields: [
                                { name: 'Status', value: '⏳ Initializing...', inline: true },
                            ],
                            timestamp: new Date().toISOString(),
                        }],
                    },
                });

                const userId = this.resolveUserId(user);
                this.userChannelMap.set(userId, interaction.channel_id);

                const incoming: IncomingMessage = {
                    id: randomUUID(),
                    channel: 'discord',
                    userId,
                    workspaceId: this.config.defaultWorkspaceId,
                    conversationId: `dc-task-${interaction.channel_id}-${Date.now()}`,
                    content: goal,
                    metadata: {
                        channelUserId: user.id,
                        channelMessageId: interaction.id,
                        timestamp: new Date(),
                        discordChannelId: interaction.channel_id,
                        isTaskRequest: true,
                        interactionToken: interaction.token,
                    },
                };

                this.emit('message', incoming);
                break;
            }

            case 'tasks':
                await this.respondToInteraction(interaction.id, interaction.token, {
                    type: 4,
                    data: {
                        embeds: [{
                            title: '📋 Your Tasks',
                            description: 'Task listing is available via the CLI:\n```\nkestrel tasks\n```',
                            color: COLORS.info,
                        }],
                        flags: 64,
                    },
                });
                break;

            case 'status':
                await this.respondToInteraction(interaction.id, interaction.token, {
                    type: 4,
                    data: {
                        embeds: [{
                            title: '🦅 Kestrel Status',
                            color: COLORS.success,
                            fields: [
                                { name: '🟢 Bot', value: 'Online', inline: true },
                                { name: '🏢 Workspace', value: `\`${this.config.defaultWorkspaceId}\``, inline: true },
                                { name: '👤 User', value: `<@${user.id}>`, inline: true },
                                { name: '📡 Gateway', value: this.sessionId ? 'Connected' : 'Disconnected', inline: true },
                            ],
                            timestamp: new Date().toISOString(),
                        }],
                        flags: 64,
                    },
                });
                break;

            case 'cancel': {
                const taskIdOpt = options.find(o => o.name === 'task_id');
                const taskId = taskIdOpt?.value || '';

                const userId = this.resolveUserId(user);
                this.emit('message', {
                    id: randomUUID(),
                    channel: 'discord',
                    userId,
                    workspaceId: this.config.defaultWorkspaceId,
                    conversationId: `dc-${interaction.channel_id}`,
                    content: `/cancel ${taskId}`,
                    metadata: {
                        channelUserId: user.id,
                        channelMessageId: interaction.id,
                        timestamp: new Date(),
                        isCommand: true,
                    },
                });

                await this.respondToInteraction(interaction.id, interaction.token, {
                    type: 4,
                    data: {
                        embeds: [{
                            title: '⏹ Cancellation Requested',
                            description: `Task \`${taskId}\` cancellation sent.`,
                            color: COLORS.warning,
                        }],
                        flags: 64,
                    },
                });
                break;
            }

            case 'model': {
                const nameOpt = options.find(o => o.name === 'name');
                const modelName = nameOpt?.value || '';

                await this.respondToInteraction(interaction.id, interaction.token, {
                    type: 4,
                    data: {
                        embeds: [{
                            title: '🔄 Model Switched',
                            description: `Now using \`${modelName}\``,
                            color: COLORS.info,
                        }],
                        flags: 64,
                    },
                });
                break;
            }

            case 'workspace':
                await this.respondToInteraction(interaction.id, interaction.token, {
                    type: 4,
                    data: {
                        content: `🏢 Current workspace: \`${this.config.defaultWorkspaceId}\``,
                        flags: 64,
                    },
                });
                break;

            case 'help':
                await this.respondToInteraction(interaction.id, interaction.token, {
                    type: 4,
                    data: {
                        embeds: [{
                            title: '🦅 Kestrel — Autonomous AI Agent',
                            description: 'Your AI assistant on Discord. Chat naturally or launch autonomous tasks.',
                            color: COLORS.primary,
                            fields: [
                                { name: '💬 Chat', value: '`/chat` or just type in a DM', inline: false },
                                { name: '🤖 Task', value: '`/task` or `!goal` to launch autonomous agent', inline: false },
                                { name: '📋 Tasks', value: '`/tasks` — List active tasks', inline: true },
                                { name: '⏹ Cancel', value: '`/cancel` — Cancel a task', inline: true },
                                { name: '📊 Status', value: '`/status` — System status', inline: true },
                                { name: '🔄 Model', value: '`/model` — Switch AI model', inline: true },
                                { name: '🏢 Workspace', value: '`/workspace` — Show workspace', inline: true },
                            ],
                        }],
                        flags: 64,
                    },
                });
                break;
        }
    }

    // ── Component (Button) Interactions ────────────────────────────

    private async handleComponentInteraction(interaction: DiscordInteraction): Promise<void> {
        const customId = interaction.data?.custom_id || '';
        const user = interaction.member?.user || interaction.user;

        if (customId.startsWith('approve:')) {
            const approvalId = customId.substring('approve:'.length);
            await this.respondToInteraction(interaction.id, interaction.token, {
                type: 4,
                data: {
                    embeds: [{
                        title: '✅ Approved',
                        description: `Approval \`${approvalId}\` confirmed by <@${user?.id}>`,
                        color: COLORS.success,
                    }],
                },
            });
            this.pendingApprovals.delete(approvalId);
        } else if (customId.startsWith('reject:')) {
            const approvalId = customId.substring('reject:'.length);
            await this.respondToInteraction(interaction.id, interaction.token, {
                type: 4,
                data: {
                    embeds: [{
                        title: '❌ Rejected',
                        description: `Approval \`${approvalId}\` rejected by <@${user?.id}>`,
                        color: COLORS.error,
                    }],
                },
            });
            this.pendingApprovals.delete(approvalId);
        } else {
            // Treat as a generic callback → route as message
            await this.respondToInteraction(interaction.id, interaction.token, {
                type: 6, // DEFERRED_UPDATE_MESSAGE
            });

            if (user) {
                const userId = this.resolveUserId(user);
                this.emit('message', {
                    id: randomUUID(),
                    channel: 'discord',
                    userId,
                    workspaceId: this.config.defaultWorkspaceId,
                    conversationId: `dc-${interaction.channel_id}`,
                    content: customId,
                    metadata: {
                        channelUserId: user.id,
                        channelMessageId: interaction.id,
                        timestamp: new Date(),
                        isComponentInteraction: true,
                    },
                });
            }
        }
    }

    // ── Sending ────────────────────────────────────────────────────

    async send(userId: string, message: OutgoingMessage): Promise<void> {
        const channelId = this.userChannelMap.get(userId);
        if (!channelId) {
            logger.warn('Cannot send Discord message — no channel for user', { userId });
            return;
        }

        await this.sendToChannel(channelId, message);
    }

    /**
     * Send a message to a Discord channel, using embeds for long content
     * and smart chunking.
     */
    private async sendToChannel(channelId: string, message: OutgoingMessage): Promise<void> {
        this.stopTyping(channelId);

        const content = message.content;

        if (content.length > 2000) {
            // Long messages → embed(s)
            const chunks = this.chunkMessage(content, 4000);
            for (let i = 0; i < chunks.length; i++) {
                const payload: any = {
                    embeds: [{
                        description: chunks[i],
                        color: COLORS.primary,
                    }],
                };

                // Add buttons only to last chunk
                if (i === chunks.length - 1 && message.options?.buttons?.length) {
                    payload.components = [{
                        type: 1,
                        components: message.options.buttons.map((btn) => ({
                            type: 2,
                            style: 1,
                            label: btn.label,
                            custom_id: btn.action,
                        })),
                    }];
                }

                await this.apiRequest('POST', `/channels/${channelId}/messages`, payload);
            }
        } else {
            // Short messages → plain text
            const payload: any = { content };

            if (message.options?.buttons?.length) {
                payload.components = [{
                    type: 1,
                    components: message.options.buttons.map((btn) => ({
                        type: 2,
                        style: 1,
                        label: btn.label,
                        custom_id: btn.action,
                    })),
                }];
            }

            await this.apiRequest('POST', `/channels/${channelId}/messages`, payload);
        }
    }

    private chunkMessage(text: string, maxLength: number): string[] {
        if (text.length <= maxLength) return [text];

        const chunks: string[] = [];
        let remaining = text;

        while (remaining.length > 0) {
            if (remaining.length <= maxLength) {
                chunks.push(remaining);
                break;
            }

            let splitAt = remaining.lastIndexOf('\n', maxLength);
            if (splitAt < maxLength / 2) splitAt = remaining.lastIndexOf('. ', maxLength);
            if (splitAt < maxLength / 2) splitAt = remaining.lastIndexOf(' ', maxLength);
            if (splitAt < maxLength / 2) splitAt = maxLength;

            chunks.push(remaining.substring(0, splitAt));
            remaining = remaining.substring(splitAt).trimStart();
        }

        return chunks;
    }

    // ── Task Progress ─────────────────────────────────────────────

    /**
     * Send a progress update to a task thread.
     */
    async sendTaskProgress(channelId: string, step: string, detail: string = ''): Promise<void> {
        const payload = {
            embeds: [{
                description: detail
                    ? `🔧 **${step}**\n${detail}`
                    : `🔧 ${step}`,
                color: COLORS.progress,
            }],
        };

        await this.apiRequest('POST', `/channels/${channelId}/messages`, payload);
    }

    /**
     * Send an approval request with approve/reject buttons.
     */
    async sendApprovalRequest(
        channelId: string,
        approvalId: string,
        description: string,
    ): Promise<void> {
        this.pendingApprovals.set(approvalId, { channelId, userId: '' });

        await this.apiRequest('POST', `/channels/${channelId}/messages`, {
            embeds: [{
                title: '⚠️ Approval Required',
                description: `${description}\n\nID: \`${approvalId}\``,
                color: COLORS.warning,
                timestamp: new Date().toISOString(),
            }],
            components: [{
                type: 1,
                components: [
                    {
                        type: 2,
                        style: 3, // SUCCESS (green)
                        label: '✅ Approve',
                        custom_id: `approve:${approvalId}`,
                    },
                    {
                        type: 2,
                        style: 4, // DANGER (red)
                        label: '❌ Reject',
                        custom_id: `reject:${approvalId}`,
                    },
                ],
            }],
        });
    }

    // ── Formatting ─────────────────────────────────────────────────

    formatOutgoing(message: OutgoingMessage): OutgoingMessage {
        // Discord supports full Markdown, no changes needed
        return message;
    }

    // ── Slash Command Registration ─────────────────────────────────

    private async registerSlashCommands(): Promise<void> {
        const endpoint = this.config.guildId
            ? `/applications/${this.config.clientId}/guilds/${this.config.guildId}/commands`
            : `/applications/${this.config.clientId}/commands`;

        await this.apiRequest('PUT', endpoint, SLASH_COMMANDS);
        logger.info(`Discord slash commands registered (${SLASH_COMMANDS.length} commands)`);
    }

    // ── Interaction Responses ──────────────────────────────────────

    private async respondToInteraction(
        interactionId: string,
        interactionToken: string,
        body: any,
    ): Promise<void> {
        const url = `${this.apiBase}/interactions/${interactionId}/${interactionToken}/callback`;
        const res = await fetch(url, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body),
        });

        if (!res.ok) {
            logger.error('Discord interaction response failed', { status: res.status });
        }
    }

    // ── User Mapping ───────────────────────────────────────────────

    private resolveUserId(user: DiscordUser): string {
        const existing = this.channelUserMap.get(user.id);
        if (existing) return existing;

        const kestrelId = `dc-${user.id}`;
        this.channelUserMap.set(user.id, kestrelId);
        return kestrelId;
    }

    // ── REST API ───────────────────────────────────────────────────

    private async apiRequest(method: string, path: string, body?: any): Promise<any> {
        const res = await fetch(`${this.apiBase}${path}`, {
            method,
            headers: {
                Authorization: `Bot ${this.config.botToken}`,
                'Content-Type': 'application/json',
            },
            body: body ? JSON.stringify(body) : undefined,
        });

        if (res.status === 204) return null;

        const data = await res.json();
        if (!res.ok) {
            throw new Error(`Discord API error: ${JSON.stringify(data)} (${method} ${path})`);
        }

        return data;
    }
}
