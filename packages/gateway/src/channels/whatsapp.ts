import { createHmac } from 'crypto';
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

export interface WhatsAppConfig {
    accountSid: string;
    authToken: string;
    fromNumber: string;            // e.g. "whatsapp:+14155238886"
    defaultWorkspaceId: string;
    webhookUrl?: string;           // For signature validation
}

// ── WhatsApp Adapter ───────────────────────────────────────────────

/**
 * WhatsApp adapter using the Twilio API.
 * Receives messages via Twilio webhook, sends responses via REST API.
 */
export class WhatsAppAdapter extends BaseChannelAdapter {
    readonly channelType: ChannelType = 'whatsapp';

    private readonly apiBase = 'https://api.twilio.com/2010-04-01';
    private readonly authHeader: string;

    // Phone → userId mapping
    private phoneMap = new Map<string, string>();    // kestrelUserId → phone
    private userIdMap = new Map<string, string>();    // phone → kestrelUserId

    constructor(private config: WhatsAppConfig) {
        super();
        this.authHeader = 'Basic ' + Buffer.from(
            `${config.accountSid}:${config.authToken}`
        ).toString('base64');
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

        const account = await res.json() as { friendly_name: string; status: string };
        logger.info(`WhatsApp adapter connected via Twilio: ${account.friendly_name} (${account.status})`);
        this.setStatus('connected');
    }

    async disconnect(): Promise<void> {
        this.setStatus('disconnected');
        logger.info('WhatsApp adapter disconnected');
    }

    // ── Sending ────────────────────────────────────────────────────

    async send(userId: string, message: OutgoingMessage): Promise<void> {
        const phone = this.phoneMap.get(userId);
        if (!phone) {
            logger.warn('Cannot send WhatsApp message — no phone for user', { userId });
            return;
        }

        // Send text message
        await this.sendTwilioMessage(phone, message.content);

        // Send attachments as media messages
        if (message.attachments?.length) {
            for (const att of message.attachments) {
                await this.sendTwilioMessage(phone, att.filename || '', att.url);
            }
        }
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
    }

    // ── Formatting ─────────────────────────────────────────────────

    formatOutgoing(message: OutgoingMessage): OutgoingMessage {
        // WhatsApp has a 1600 char limit per message
        let content = message.content;
        if (content.length > 1590) {
            content = content.substring(0, 1587) + '...';
        }

        // Strip Markdown that WhatsApp doesn't support well
        content = content
            .replace(/#{1,6}\s+/g, '')       // Headers → plain text
            .replace(/\[([^\]]+)\]\([^)]+\)/g, '$1');  // Links → text only

        return { ...message, content };
    }

    // ── Webhook Processing ─────────────────────────────────────────

    /**
     * Validate Twilio signature on incoming webhook.
     */
    validateSignature(url: string, body: Record<string, string>, signature: string): boolean {
        // Build validation string: URL + sorted params
        const keys = Object.keys(body).sort();
        let data = url;
        for (const key of keys) {
            data += key + body[key];
        }

        const computed = createHmac('sha1', this.config.authToken)
            .update(data)
            .digest('base64');

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

        const incoming: IncomingMessage = {
            id: randomUUID(),
            channel: 'whatsapp',
            userId,
            workspaceId: this.config.defaultWorkspaceId,
            conversationId: `wa-${from}`,   // 1 phone = 1 conversation
            content: messageBody,
            attachments: attachments.length ? attachments : undefined,
            metadata: {
                channelUserId: from,
                channelMessageId: messageSid,
                timestamp: new Date(),
                phoneNumber: from,
            },
        };

        this.emit('message', incoming);
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
