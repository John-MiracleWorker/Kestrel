import crypto from 'crypto';
import fs from 'fs';
import net from 'net';
import path from 'path';
import { getGatewayStateFile, getKestrelHome } from '../utils/paths';
import { logger } from '../utils/logger';

const DEFAULT_CONTROL_PORT = parseInt(process.env.KESTREL_CONTROL_PORT || '8749', 10);
const DEFAULT_CONTROL_HOST = process.env.KESTREL_CONTROL_HOST || '127.0.0.1';

export interface ControlEnvelope {
    request_id: string;
    ok: boolean;
    done?: boolean;
    result?: any;
    event?: any;
    error?: {
        message?: string;
        code?: string;
    };
}

export interface ControlTransport {
    request(method: string, params?: Record<string, any>): Promise<any>;
    stream(method: string, params?: Record<string, any>): AsyncIterable<ControlEnvelope>;
}

export function isLocalBrainMode(): boolean {
    const runtimeMode = (process.env.KESTREL_RUNTIME_MODE || '').toLowerCase();
    const brainTransport = (process.env.BRAIN_TRANSPORT || '').toLowerCase();
    return runtimeMode === 'native' || runtimeMode === 'local' || brainTransport === 'local';
}

function parseJsonLines(socket: net.Socket, requestId: string): AsyncIterable<ControlEnvelope> {
    const queue: ControlEnvelope[] = [];
    const waiters: Array<{
        resolve: (value: IteratorResult<ControlEnvelope>) => void;
        reject: (reason?: unknown) => void;
    }> = [];
    let buffer = '';
    let ended = false;
    let streamError: Error | null = null;

    const settle = () => {
        while (waiters.length > 0) {
            const waiter = waiters.shift();
            if (!waiter) continue;

            if (queue.length > 0) {
                waiter.resolve({ value: queue.shift()!, done: false });
                continue;
            }

            if (streamError) {
                waiter.reject(streamError);
                continue;
            }

            if (ended) {
                waiter.resolve({ value: undefined, done: true });
            }
        }
    };

    socket.on('data', (chunk: Buffer | string) => {
        buffer += chunk.toString();
        while (buffer.includes('\n')) {
            const newlineIndex = buffer.indexOf('\n');
            const line = buffer.slice(0, newlineIndex).trim();
            buffer = buffer.slice(newlineIndex + 1);
            if (!line) continue;

            try {
                const payload = JSON.parse(line) as ControlEnvelope;
                if (payload.request_id !== requestId) {
                    continue;
                }
                queue.push(payload);
                settle();
            } catch (error: any) {
                streamError = new Error(error?.message || 'Invalid daemon control response');
                settle();
                socket.destroy(streamError);
                return;
            }
        }
    });

    socket.on('error', (error) => {
        streamError = error;
        settle();
    });

    socket.on('close', () => {
        ended = true;
        settle();
    });

    return {
        [Symbol.asyncIterator]() {
            return {
                next(): Promise<IteratorResult<ControlEnvelope>> {
                    if (queue.length > 0) {
                        return Promise.resolve({ value: queue.shift()!, done: false });
                    }
                    if (streamError) {
                        return Promise.reject(streamError);
                    }
                    if (ended) {
                        return Promise.resolve({ value: undefined, done: true });
                    }
                    return new Promise((resolve, reject) => {
                        waiters.push({ resolve, reject });
                    });
                },
            };
        },
    };
}

export class DaemonControlTransport implements ControlTransport {
    constructor(
        private readonly options: {
            host?: string;
            port?: number;
            socketPath?: string;
            platform?: NodeJS.Platform;
        } = {},
    ) {}

    private platform(): NodeJS.Platform {
        return this.options.platform || process.platform;
    }

    private socketPath(): string {
        if (this.options.socketPath) {
            return this.options.socketPath;
        }
        return path.join(getKestrelHome(), 'run', 'control.sock');
    }

    private host(): string {
        return this.options.host || DEFAULT_CONTROL_HOST;
    }

    private port(): number {
        return this.options.port || DEFAULT_CONTROL_PORT;
    }

    private async openConnection(): Promise<net.Socket> {
        return new Promise((resolve, reject) => {
            const socket =
                this.platform() === 'win32'
                    ? net.createConnection({ host: this.host(), port: this.port() })
                    : net.createConnection(this.socketPath());

            socket.once('connect', () => resolve(socket));
            socket.once('error', (error) => reject(error));
        });
    }

    async *stream(
        method: string,
        params: Record<string, any> = {},
    ): AsyncIterable<ControlEnvelope> {
        const socket = await this.openConnection();
        const requestId = crypto.randomUUID();
        const payload = JSON.stringify({
            request_id: requestId,
            method,
            params,
        });
        socket.write(`${payload}\n`);

        try {
            for await (const response of parseJsonLines(socket, requestId)) {
                if (!response.ok) {
                    throw new Error(response.error?.message || 'Daemon control request failed');
                }
                yield response;
                if (response.done) {
                    break;
                }
            }
        } finally {
            socket.end();
            socket.destroy();
        }
    }

    async request(method: string, params: Record<string, any> = {}): Promise<any> {
        for await (const response of this.stream(method, params)) {
            if (response.result !== undefined) {
                return response.result;
            }
            if (response.done) {
                return {};
            }
        }
        throw new Error(`No result received for daemon method ${method}`);
    }
}
