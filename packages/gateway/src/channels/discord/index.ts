import { randomUUID } from 'crypto';
import {
    BaseChannelAdapter,
    ChannelType,
    IncomingMessage,
    OutgoingMessage,
    Attachment,
} from '../base';
import { logger } from '../../utils/logger';
import { DiscordConfig, DiscordInteraction, DiscordMessagePayload, DiscordUser } from './types';
import { SLASH_COMMANDS, COLORS } from './constants';
import { handleMessage, handleTaskMessage, handleInteraction, handleComponentInteraction } from './handlers';


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
    public sessionId?: string;
    private sequence: number | null = null;

    // Channel → userId mapping
    public channelUserMap = new Map<string, string>();    // discordUserId → kestrelUserId
    public userChannelMap = new Map<string, string>();    // kestrelUserId → discordChannelId

    // Typing indicators
    private typingIntervals = new Map<string, NodeJS.Timeout>();

    // Active task threads
    public taskThreads = new Map<string, string>();  // taskConversationId → threadId

    // Pending approvals
    public pendingApprovals = new Map<string, { channelId: string; userId: string }>();

    constructor(public config: DiscordConfig) {
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

    public isAllowed(roles?: string[]): boolean {
        if (!this.config.allowedRoleIds?.length) return true;
        if (!roles?.length) return false;
        return roles.some(r => this.config.allowedRoleIds!.includes(r));
    }

    // ── Typing Indicators ──────────────────────────────────────────

    public startTyping(channelId: string): void {
        this.apiRequest('POST', `/channels/${channelId}/typing`).catch(() => { });

        if (!this.typingIntervals.has(channelId)) {
            const interval = setInterval(() => {
                this.apiRequest('POST', `/channels/${channelId}/typing`).catch(() => { });
            }, 8000);  // Discord typing indicator lasts ~10s
            this.typingIntervals.set(channelId, interval);
        }
    }

    public stopTyping(channelId: string): void {
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
                handleMessage(this, data as DiscordMessagePayload);
                break;
            case 'INTERACTION_CREATE':
                handleInteraction(this, data as DiscordInteraction);
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

    public async respondToInteraction(
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

    public resolveUserId(user: DiscordUser): string {
        const existing = this.channelUserMap.get(user.id);
        if (existing) return existing;

        const kestrelId = `dc-${user.id}`;
        this.channelUserMap.set(user.id, kestrelId);
        return kestrelId;
    }

    // ── REST API ───────────────────────────────────────────────────

    public async apiRequest(method: string, path: string, body?: any): Promise<any> {
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
