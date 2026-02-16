/**
 * Supported channel types.
 */
export type ChannelType = 'web' | 'whatsapp' | 'telegram' | 'discord' | 'mobile';

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
 * Subclasses implement connect(), disconnect(), send().
 */
export abstract class BaseChannelAdapter {
    abstract readonly channelType: ChannelType;

    private messageHandlers: Array<(msg: IncomingMessage) => void> = [];
    private errorHandlers: Array<(err: Error) => void> = [];

    abstract connect(): Promise<void>;
    abstract disconnect(): Promise<void>;
    abstract send(userId: string, message: OutgoingMessage): Promise<void>;

    on(event: 'message', handler: (msg: IncomingMessage) => void): void;
    on(event: 'error', handler: (err: Error) => void): void;
    on(event: string, handler: any): void {
        if (event === 'message') this.messageHandlers.push(handler);
        if (event === 'error') this.errorHandlers.push(handler);
    }

    protected emit(event: 'message', msg: IncomingMessage): void;
    protected emit(event: 'error', err: Error): void;
    protected emit(event: string, data: any): void {
        if (event === 'message') this.messageHandlers.forEach(h => h(data));
        if (event === 'error') this.errorHandlers.forEach(h => h(data));
    }
}
