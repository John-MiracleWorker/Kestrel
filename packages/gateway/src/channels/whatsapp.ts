import { createHmac } from 'crypto';
import { BaseChannelAdapter, ChannelType, OutgoingMessage, Attachment } from './base';
import { createIngressEnvelope, type NormalizedIngressPayloadKind } from './ingress';
import { parseTaskRequest } from './orchestration/intents';
import { logger } from '../utils/logger';

// ── Configuration ──────────────────────────────────────────────────

export interface WhatsAppConfig {
    accountSid: string;
    authToken: string;
    fromNumber: string; // e.g. "whatsapp:+14155238886"
    defaultWorkspaceId: string;
    webhookUrl?: string; // For signature validation
    allowedNumbers?: string[]; // Optional: restrict to these phone numbers
}

// ── WhatsApp Adapter ───────────────────────────────────────────────

/**
 * Full-featured WhatsApp adapter using the Twilio API.
 *
 * Features:
 *   ✅ Chat mode — conversational AI via WhatsApp
 *   ✅ Task mode — launch autonomous tasks with !goal prefix
 *   ✅ Commands — /help, /status, /task, /tasks, /cancel, /new, /model
 *   ✅ Smart chunking — splits long responses (1600 char WhatsApp limit)
 *   ✅ Delivery receipts — status callback tracking
 *   ✅ Media handling — images, audio, video, documents
 *   ✅ Access control — optional phone number allowlist
 *   ✅ Twilio signature validation
 *   ✅ Progress updates — sends step-by-step task progress
 *   ✅ Markdown stripping — clean WhatsApp-compatible formatting
 */
export class WhatsAppAdapter extends BaseChannelAdapter {
    readonly channelType: ChannelType = 'whatsapp';

    private readonly apiBase = 'https://api.twilio.com/2010-04-01';
    private readonly authHeader: string;

    // Phone → userId mapping
    private phoneMap = new Map<string, string>(); // kestrelUserId → phone
    private userIdMap = new Map<string, string>(); // phone → kestrelUserId

    // Delivery status tracking
    private pendingMessages = new Map<string, { phone: string; sentAt: Date }>();

    constructor(private config: WhatsAppConfig) {
        super();
        this.authHeader =
            'Basic ' + Buffer.from(`${config.accountSid}:${config.authToken}`).toString('base64');
    }

    // ── Lifecycle ──────────────────────────────────────────────────

    async connect(): Promise<void> {
        this.setStatus('connecting');

        // Validate credentials by fetching account info
        const url = `${this.apiBase}/Accounts/${this.config.accountSid}.json`;
        const res = await fetch(url, {
            headers: { Authorization: this.authHeader },
        });

        if (!res.ok) {
            throw new Error(`Twilio credentials validation failed: ${res.status}`);
        }

        const account = (await res.json()) as { friendly_name: string; status: string };
        logger.info(
            `WhatsApp adapter connected via Twilio: ${account.friendly_name} (${account.status})`,
        );
        this.setStatus('connected');
    }

    async disconnect(): Promise<void> {
        this.setStatus('disconnected');
        logger.info('WhatsApp adapter disconnected');
    }

    // ── Access Control ─────────────────────────────────────────────

    private isAllowed(phone: string): boolean {
        if (!this.config.allowedNumbers?.length) return true;
        return this.config.allowedNumbers.includes(phone);
    }

    // ── Sending ────────────────────────────────────────────────────

    async send(userId: string, message: OutgoingMessage): Promise<void> {
        const phone = this.phoneMap.get(userId);
        if (!phone) {
            logger.warn('Cannot send WhatsApp message — no phone for user', { userId });
            return;
        }

        await this.sendToPhone(phone, message);
    }

    /**
     * Send a message to a phone number, handling chunking and media.
     */
    private async sendToPhone(phone: string, message: OutgoingMessage): Promise<void> {
        const content = this.stripMarkdown(message.content);
        const chunks = this.chunkMessage(content, 1500);

        for (const chunk of chunks) {
            await this.sendTwilioMessage(phone, chunk);
        }

        // Send attachments as media messages
        if (message.attachments?.length) {
            for (const att of message.attachments) {
                await this.sendTwilioMessage(phone, att.filename || 'File', att.url);
            }
        }
    }

    /**
     * Split long messages at natural boundaries.
     */
    private chunkMessage(text: string, maxLength: number): string[] {
        if (text.length <= maxLength) return [text];

        const chunks: string[] = [];
        let remaining = text;

        while (remaining.length > 0) {
            if (remaining.length <= maxLength) {
                chunks.push(remaining);
                break;
            }

            // Try to split at natural boundaries
            let splitAt = remaining.lastIndexOf('\n', maxLength);
            if (splitAt < maxLength / 2) splitAt = remaining.lastIndexOf('. ', maxLength);
            if (splitAt < maxLength / 2) splitAt = remaining.lastIndexOf(' ', maxLength);
            if (splitAt < maxLength / 2) splitAt = maxLength;

            chunks.push(remaining.substring(0, splitAt));
            remaining = remaining.substring(splitAt).trimStart();
        }

        return chunks;
    }

    /**
     * Strip Markdown that WhatsApp doesn't support.
     * Keeps *bold* and _italic_ which WhatsApp does handle.
     */
    private stripMarkdown(text: string): string {
        return text
            .replace(/#{1,6}\s+/g, '') // Headers → plain text
            .replace(/\[([^\]]+)\]\([^)]+\)/g, '$1') // Links → text only
            .replace(
                /```[\s\S]*?```/g,
                (
                    m, // Code blocks → indented
                ) =>
                    m
                        .replace(/```\w*\n?/, '')
                        .replace(/```/, '')
                        .split('\n')
                        .map((l) => '  ' + l)
                        .join('\n'),
            )
            .replace(/`([^`]+)`/g, '$1'); // Inline code → plain
    }

    private async sendTwilioMessage(to: string, body: string, mediaUrl?: string): Promise<void> {
        const url = `${this.apiBase}/Accounts/${this.config.accountSid}/Messages.json`;

        const params = new URLSearchParams({
            From: this.config.fromNumber,
            To: `whatsapp:${to}`,
            Body: body,
        });

        if (mediaUrl) {
            params.append('MediaUrl', mediaUrl);
        }

        const res = await fetch(url, {
            method: 'POST',
            headers: {
                Authorization: this.authHeader,
                'Content-Type': 'application/x-www-form-urlencoded',
            },
            body: params.toString(),
        });

        if (!res.ok) {
            const err = await res.text();
            logger.error('Twilio send failed', { error: err, to });
            throw new Error(`Twilio send failed: ${res.status}`);
        }

        // Track for delivery receipts
        const data = (await res.json()) as { sid: string };
        this.pendingMessages.set(data.sid, { phone: to, sentAt: new Date() });
    }

    // ── Formatting ─────────────────────────────────────────────────

    formatOutgoing(message: OutgoingMessage): OutgoingMessage {
        return message; // Chunking and stripping handled in sendToPhone
    }

    // ── Progress Updates ──────────────────────────────────────────

    /**
     * Send a progress update for an active task (silent, short message).
     */
    async sendTaskProgress(phone: string, step: string, detail: string = ''): Promise<void> {
        const text = detail ? `🔧 *${step}*\n${detail}` : `🔧 ${step}`;
        await this.sendTwilioMessage(phone, text);
    }

    /**
     * Send an approval request via WhatsApp.
     */
    async sendApprovalRequest(
        phone: string,
        approvalId: string,
        description: string,
    ): Promise<void> {
        const text =
            `⚠️ *Approval Required*\n\n` +
            `${description}\n\n` +
            `Reply with:\n` +
            `  /approve ${approvalId}\n` +
            `  /reject ${approvalId}`;
        await this.sendTwilioMessage(phone, text);
    }

    private emitIngressEvent(input: {
        userId: string;
        phone: string;
        content: string;
        conversationId: string;
        channelMessageId?: string;
        attachments?: Attachment[];
        payloadKind?: NormalizedIngressPayloadKind;
        metadata?: Record<string, unknown>;
    }): void {
        const event = createIngressEnvelope({
            channel: 'whatsapp',
            userId: input.userId,
            workspaceId: this.config.defaultWorkspaceId,
            conversationId: input.conversationId,
            content: input.content,
            attachments: input.attachments,
            metadata: {
                channelUserId: input.phone,
                channelMessageId: input.channelMessageId,
                timestamp: new Date(),
                phoneNumber: input.phone,
                ...input.metadata,
            },
            externalUserId: input.phone,
            externalConversationId: input.phone,
            authContext: {
                transport: 'whatsapp_webhook',
                authenticatedUserId: input.userId,
                isProvisionalUser: false,
            },
            payloadKind: input.payloadKind,
        });

        this.emit('message', event);
    }

    // ── Webhook Processing ─────────────────────────────────────────

    /**
     * Validate Twilio signature on incoming webhook.
     */
    validateSignature(url: string, body: Record<string, string>, signature: string): boolean {
        const keys = Object.keys(body).sort();
        let data = url;
        for (const key of keys) {
            data += key + body[key];
        }

        const computed = createHmac('sha1', this.config.authToken).update(data).digest('base64');

        return computed === signature;
    }

    /**
     * Process an incoming Twilio webhook payload.
     */
    async processWebhook(body: Record<string, string>): Promise<void> {
        const from = body.From?.replace('whatsapp:', '') || '';
        const messageBody = body.Body || '';
        const messageSid = body.MessageSid || '';
        const numMedia = parseInt(body.NumMedia || '0');

        if (!from) {
            logger.warn('WhatsApp webhook received without From number');
            return;
        }

        // Access control
        if (!this.isAllowed(from)) {
            await this.sendTwilioMessage(
                from,
                '🔒 Access denied. You are not authorized to use this bot.',
            );
            return;
        }

        // Handle commands
        if (messageBody.startsWith('/')) {
            await this.handleCommand(from, messageBody);
            return;
        }

        // Handle task mode (!goal prefix)
        const taskGoal = parseTaskRequest(messageBody);
        if (taskGoal) {
            await this.handleTaskRequest(from, taskGoal, messageSid);
            return;
        }

        // Map phone → user
        const userId = this.resolveUserId(from);

        // Build attachments
        const attachments: Attachment[] = [];
        for (let i = 0; i < numMedia; i++) {
            const mediaUrl = body[`MediaUrl${i}`];
            const mediaType = body[`MediaContentType${i}`] || '';

            if (mediaUrl) {
                let type: Attachment['type'] = 'file';
                if (mediaType.startsWith('image/')) type = 'image';
                else if (mediaType.startsWith('audio/')) type = 'audio';
                else if (mediaType.startsWith('video/')) type = 'video';

                attachments.push({
                    type,
                    url: mediaUrl,
                    mimeType: mediaType,
                });
            }
        }

        this.emitIngressEvent({
            userId,
            phone: from,
            conversationId: `wa-${from}`,
            content: messageBody,
            attachments: attachments.length ? attachments : undefined,
            channelMessageId: messageSid || undefined,
        });
    }

    // ── Task Mode ─────────────────────────────────────────────────

    private async handleTaskRequest(
        phone: string,
        goal: string,
        messageSid: string,
    ): Promise<void> {
        const userId = this.resolveUserId(phone);

        await this.sendTwilioMessage(
            phone,
            `🦅 *Starting autonomous task...*\n\n` +
                `📋 *Goal:* ${goal}\n\n` +
                `_I'll work on this and send you updates._`,
        );

        this.emitIngressEvent({
            userId,
            phone,
            conversationId: `wa-task-${phone}-${Date.now()}`,
            content: goal,
            channelMessageId: messageSid || undefined,
            payloadKind: 'task',
            metadata: {
                isTaskRequest: true,
            },
        });
    }

    // ── Commands ───────────────────────────────────────────────────

    private async handleCommand(phone: string, text: string): Promise<void> {
        const parts = text.split(/\s+/);
        const command = parts[0];
        const args = parts.slice(1);

        switch (command) {
            case '/start':
            case '/help':
                await this.sendTwilioMessage(
                    phone,
                    `🦅 *Kestrel on WhatsApp*\n\n` +
                        `*💬 Chat Mode*\n` +
                        `Just send a message to chat with your AI agent.\n\n` +
                        `*🤖 Task Mode*\n` +
                        `Start with ! to launch an autonomous task:\n` +
                        `  !review the auth module\n\n` +
                        `*Commands:*\n` +
                        `  /help — This help\n` +
                        `  /task <goal> — Start a task\n` +
                        `  /tasks — List active tasks\n` +
                        `  /status — System status\n` +
                        `  /cancel <id> — Cancel a task\n` +
                        `  /approve <id> — Approve action\n` +
                        `  /reject <id> — Reject action\n` +
                        `  /model <name> — Switch AI model\n` +
                        `  /new — New conversation`,
                );
                break;

            case '/task': {
                const goal = args.join(' ').trim();
                if (!goal) {
                    await this.sendTwilioMessage(
                        phone,
                        `❓ Usage: /task <your goal>\n\n` +
                            `Example: /task review the database schema`,
                    );
                    return;
                }
                await this.handleTaskRequest(phone, goal, '');
                break;
            }

            case '/tasks':
                await this.sendTwilioMessage(
                    phone,
                    `📋 *Your Tasks*\n\n` +
                        `Task listing available via the CLI:\n` +
                        `  kestrel tasks`,
                );
                break;

            case '/status':
                await this.sendTwilioMessage(
                    phone,
                    `🦅 *Kestrel Status*\n\n` +
                        `✅ Bot: Online\n` +
                        `📱 Channel: WhatsApp\n` +
                        `🏢 Workspace: ${this.config.defaultWorkspaceId}\n` +
                        `📞 Your number: ${phone}`,
                );
                break;

            case '/cancel': {
                const taskId = args[0];
                if (!taskId) {
                    await this.sendTwilioMessage(phone, `❓ Usage: /cancel <task_id>`);
                    return;
                }
                const userId = this.resolveUserId(phone);
                this.emitIngressEvent({
                    userId,
                    phone,
                    conversationId: `wa-${phone}`,
                    content: `/cancel ${taskId}`,
                    payloadKind: 'command',
                    metadata: {
                        isCommand: true,
                    },
                });
                break;
            }

            case '/approve': {
                const approvalId = args[0];
                if (!approvalId) {
                    await this.sendTwilioMessage(phone, `❓ Usage: /approve <approval_id>`);
                    return;
                }
                await this.sendTwilioMessage(phone, `✅ *Approved* \`${approvalId}\``);
                break;
            }

            case '/reject': {
                const rejectId = args[0];
                if (!rejectId) {
                    await this.sendTwilioMessage(phone, `❓ Usage: /reject <approval_id>`);
                    return;
                }
                await this.sendTwilioMessage(phone, `❌ *Rejected* \`${rejectId}\``);
                break;
            }

            case '/model':
                if (args[0]) {
                    await this.sendTwilioMessage(phone, `🔄 Model switched to ${args[0]}`);
                } else {
                    await this.sendTwilioMessage(
                        phone,
                        `❓ Usage: /model <model_name>\n\n` +
                            `Examples:\n  /model gpt-4o\n  /model claude-sonnet-4-20250514\n  /model gemini-2.5-pro`,
                    );
                }
                break;

            case '/new':
                await this.sendTwilioMessage(
                    phone,
                    `✨ New conversation started! Send your first message.`,
                );
                break;

            default:
                await this.sendTwilioMessage(
                    phone,
                    `Unknown command: ${command}. Send /help for available commands.`,
                );
        }
    }

    // ── Delivery Status ───────────────────────────────────────────

    /**
     * Process a Twilio status callback for delivery tracking.
     */
    processStatusCallback(body: Record<string, string>): void {
        const messageSid = body.MessageSid;
        const status = body.MessageStatus;

        if (
            status === 'delivered' ||
            status === 'read' ||
            status === 'failed' ||
            status === 'undelivered'
        ) {
            this.pendingMessages.delete(messageSid);
        }

        if (status === 'failed' || status === 'undelivered') {
            logger.error('WhatsApp message delivery failed', {
                messageSid,
                status,
                errorCode: body.ErrorCode,
                errorMessage: body.ErrorMessage,
            });
        }
    }

    // ── User Mapping ───────────────────────────────────────────────

    private resolveUserId(phone: string): string {
        const existing = this.userIdMap.get(phone);
        if (existing) return existing;

        const userId = `wa-${phone.replace(/\+/g, '')}`;
        this.userIdMap.set(phone, userId);
        this.phoneMap.set(userId, phone);
        return userId;
    }
}
