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
    author: DiscordUser;
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
    type: number;                    // 2 = APPLICATION_COMMAND
    data?: {
        name: string;
        options?: Array<{ name: string; value: string; type: number }>;
    };
    channel_id: string;
    guild_id?: string;
    member?: { user: DiscordUser };
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
        name: 'workspace',
        description: 'Show current workspace info',
    },
    {
        name: 'help',
        description: 'Show Kestrel bot help',
    },
];

// ── Discord Adapter ────────────────────────────────────────────────

/**
 * Discord Bot adapter using the Discord REST + Gateway API.
 * Supports slash commands, rich embeds, thread-based conversations,
 * and message component interactions.
 */
export class DiscordAdapter extends BaseChannelAdapter {
    readonly channelType: ChannelType = 'discord';

    private readonly apiBase = 'https://discord.com/api/v10';
    private gatewayWs: any = null;   // WebSocket to Discord Gateway
    private heartbeatInterval?: NodeJS.Timeout;
    private sessionId?: string;
    private sequence: number | null = null;

    // Channel → userId mapping
    private channelUserMap = new Map<string, string>();    // discordUserId → kestrelUserId
    private userChannelMap = new Map<string, string>();    // kestrelUserId → discordChannelId (DM)

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
        if (this.gatewayWs) {
            this.gatewayWs.close(1000, 'Bot shutting down');
            this.gatewayWs = null;
        }
        this.setStatus('disconnected');
        logger.info('Discord adapter disconnected');
    }

    // ── Gateway WebSocket ──────────────────────────────────────────

    private async connectGateway(url: string): Promise<void> {
        // Dynamic import since discord gateway uses ws
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

                // Auto-reconnect on non-fatal close codes
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
                // Send IDENTIFY
                this.gatewayWs?.send(JSON.stringify({
                    op: 2,
                    d: {
                        token: this.config.botToken,
                        intents: (1 << 9) | (1 << 12) | (1 << 15), // GUILDS | MESSAGE_CONTENT | GUILD_MESSAGES
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
                    // Resumable
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
        if ((msg.author as any).bot) return;

        const userId = this.resolveUserId(msg.author);

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
            content: msg.content,
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

    private async handleInteraction(interaction: DiscordInteraction): Promise<void> {
        if (interaction.type !== 2) return; // Only APPLICATION_COMMAND

        const user = interaction.member?.user || interaction.user;
        if (!user) return;

        const commandName = interaction.data?.name;
        const options = interaction.data?.options || [];

        switch (commandName) {
            case 'chat': {
                const messageOpt = options.find(o => o.name === 'message');
                const content = messageOpt?.value || '';

                // Acknowledge with "thinking"
                await this.respondToInteraction(interaction.id, interaction.token, {
                    type: 5, // DEFERRED_CHANNEL_MESSAGE_WITH_SOURCE
                });

                // Route as a normal message
                const userId = this.resolveUserId(user);
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

            case 'workspace':
                await this.respondToInteraction(interaction.id, interaction.token, {
                    type: 4,
                    data: {
                        content: `🪶 Current workspace: \`${this.config.defaultWorkspaceId}\``,
                        flags: 64, // Ephemeral
                    },
                });
                break;

            case 'help':
                await this.respondToInteraction(interaction.id, interaction.token, {
                    type: 4,
                    data: {
                        embeds: [{
                            title: '🪶 Kestrel Help',
                            description: 'Your AI assistant on Discord.',
                            color: 0x6366f1,
                            fields: [
                                { name: '/chat', value: 'Chat with Kestrel AI', inline: true },
                                { name: '/workspace', value: 'Show workspace info', inline: true },
                                { name: '/help', value: 'Show this help', inline: true },
                            ],
                        }],
                        flags: 64,
                    },
                });
                break;
        }
    }

    // ── Sending ────────────────────────────────────────────────────

    async send(userId: string, message: OutgoingMessage): Promise<void> {
        const channelId = this.userChannelMap.get(userId);
        if (!channelId) {
            logger.warn('Cannot send Discord message — no channel for user', { userId });
            return;
        }

        // Build embed for rich responses
        const payload: any = {};

        if (message.content.length > 2000) {
            // Long messages → embed
            payload.embeds = [{
                description: message.content.substring(0, 4096),
                color: 0x6366f1,
            }];
        } else {
            payload.content = message.content;
        }

        // Buttons → message components
        if (message.options?.buttons?.length) {
            payload.components = [{
                type: 1, // ACTION_ROW
                components: message.options.buttons.map((btn) => ({
                    type: 2, // BUTTON
                    style: 1, // PRIMARY
                    label: btn.label,
                    custom_id: btn.action,
                })),
            }];
        }

        await this.apiRequest('POST', `/channels/${channelId}/messages`, payload);
    }

    // ── Formatting ─────────────────────────────────────────────────

    formatOutgoing(message: OutgoingMessage): OutgoingMessage {
        // Discord supports most Markdown natively, no changes needed
        return message;
    }

    // ── Slash Command Registration ─────────────────────────────────

    private async registerSlashCommands(): Promise<void> {
        const endpoint = this.config.guildId
            ? `/applications/${this.config.clientId}/guilds/${this.config.guildId}/commands`
            : `/applications/${this.config.clientId}/commands`;

        await this.apiRequest('PUT', endpoint, SLASH_COMMANDS);
        logger.info('Discord slash commands registered');
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
