/**
 * Supported channel types.
 */
export type ChannelType = 'web' | 'whatsapp' | 'telegram' | 'discord' | 'mobile';

/**
 * Adapter connection state.
 */
export type AdapterStatus = 'disconnected' | 'connecting' | 'connected' | 'error';

/**
 * Attachment (files, images, audio).
 */
export interface Attachment {
    type: 'image' | 'audio' | 'video' | 'file';
    url: string;
    mimeType?: string;
    size?: number;
    filename?: string;
}

/**
 * Button for interactive messages (Telegram/Discord).
 */
export interface Button {
    label: string;
    action: string;
    value?: string;
}

/**
 * Incoming message from any channel — normalized format.
 */
export interface IncomingMessage {
    id: string;
    channel: ChannelType;
    userId: string;
    workspaceId: string;
    conversationId?: string;
    content: string;
    attachments?: Attachment[];
    metadata: {
        channelUserId: string;   // Original platform user ID
        channelMessageId: string;
        timestamp: Date;
        [key: string]: any;
    };
}

/**
 * Outgoing message to any channel.
 */
export interface OutgoingMessage {
    conversationId: string;
    content: string;
    attachments?: Attachment[];
    options?: {
        buttons?: Button[];
        markdown?: boolean;
        mentions?: string[];
    };
}

/**
 * Base class for all channel adapters.
 * Subclasses implement connect(), disconnect(), send(), and
 * optionally override handleAttachment() and formatOutgoing().
 */
export abstract class BaseChannelAdapter {
    abstract readonly channelType: ChannelType;

    private _status: AdapterStatus = 'disconnected';
    private messageHandlers: Array<(msg: IncomingMessage) => void> = [];
    private errorHandlers: Array<(err: Error) => void> = [];
    private statusHandlers: Array<(status: AdapterStatus) => void> = [];

    /** Current connection status */
    get status(): AdapterStatus {
        return this._status;
    }

    protected setStatus(status: AdapterStatus) {
        this._status = status;
        this.statusHandlers.forEach(h => h(status));
    }

    abstract connect(): Promise<void>;
    abstract disconnect(): Promise<void>;
    abstract send(userId: string, message: OutgoingMessage): Promise<void>;

    /**
     * Process an attachment for this channel (download, transcode, etc.).
     * Default implementation returns the attachment unchanged.
     */
    async handleAttachment(attachment: Attachment): Promise<Attachment> {
        return attachment;
    }

    /**
     * Format an outgoing message for this channel's native format.
     * E.g., convert Markdown → Telegram HTML, or build Discord embeds.
     * Default implementation returns content unchanged.
     */
    formatOutgoing(message: OutgoingMessage): OutgoingMessage {
        return message;
    }

    // ── Event system ───────────────────────────────────────────────

    on(event: 'message', handler: (msg: IncomingMessage) => void): void;
    on(event: 'error', handler: (err: Error) => void): void;
    on(event: 'status', handler: (status: AdapterStatus) => void): void;
    on(event: string, handler: any): void {
        if (event === 'message') this.messageHandlers.push(handler);
        if (event === 'error') this.errorHandlers.push(handler);
        if (event === 'status') this.statusHandlers.push(handler);
    }

    protected emit(event: 'message', msg: IncomingMessage): void;
    protected emit(event: 'error', err: Error): void;
    protected emit(event: 'status', status: AdapterStatus): void;
    protected emit(event: string, data: any): void {
        if (event === 'message') this.messageHandlers.forEach(h => h(data));
        if (event === 'error') this.errorHandlers.forEach(h => h(data));
        if (event === 'status') this.statusHandlers.forEach(h => h(data));
    }
}
