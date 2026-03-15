import * as grpc from '@grpc/grpc-js';
import * as protoLoader from '@grpc/proto-loader';
import path from 'path';

import {
    getLocalControlTransport,
    getLocalGatewayStateStore,
    isLocalBrainMode,
    type ControlTransport,
    type LocalGatewayStateStore,
} from './local';
import { logger } from '../utils/logger';

const PROTO_PATH = path.resolve(__dirname, '../../../shared/proto/brain.proto');

export type BrainClientRuntime = {
    address: string;
    client: any;
    connected: boolean;
    localMode: boolean;
    localTransport: ControlTransport;
    localStore: LocalGatewayStateStore;
};

export function createBrainClientRuntime(address: string): BrainClientRuntime {
    return {
        address,
        client: null,
        connected: false,
        localMode: isLocalBrainMode(),
        localTransport: getLocalControlTransport(),
        localStore: getLocalGatewayStateStore(),
    };
}

export function streamToAsyncIterable(stream: any): AsyncIterable<any> {
    const queue: any[] = [];
    const waiters: Array<{
        resolve: (value: IteratorResult<any>) => void;
        reject: (reason?: any) => void;
    }> = [];
    let ended = false;
    let streamError: any = null;

    const settleWaiters = () => {
        while (waiters.length > 0) {
            const waiter = waiters.shift();
            if (!waiter) {
                continue;
            }

            if (queue.length > 0) {
                waiter.resolve({ value: queue.shift(), done: false });
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

    stream.on('data', (data: any) => {
        queue.push(data);
        settleWaiters();
    });

    stream.on('end', () => {
        ended = true;
        settleWaiters();
    });

    stream.on('error', (err: any) => {
        streamError = err;
        settleWaiters();
    });

    return {
        [Symbol.asyncIterator]() {
            return {
                next(): Promise<IteratorResult<any>> {
                    if (queue.length > 0) {
                        return Promise.resolve({ value: queue.shift(), done: false });
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

export async function connectBrainClient(
    runtime: BrainClientRuntime,
    maxRetries = 10,
): Promise<void> {
    if (runtime.localMode) {
        await runtime.localTransport.request('status', {});
        runtime.connected = true;
        logger.info('Brain client attached to local daemon control plane');
        return;
    }

    const packageDef = protoLoader.loadSync(PROTO_PATH, {
        keepCase: true,
        longs: String,
        enums: String,
        defaults: true,
        oneofs: true,
    });
    const proto = grpc.loadPackageDefinition(packageDef) as any;
    const BrainService = proto.kestrel.brain.BrainService;

    runtime.client = new BrainService(runtime.address, grpc.credentials.createInsecure());

    for (let attempt = 1; attempt <= maxRetries; attempt += 1) {
        try {
            await new Promise<void>((resolve, reject) => {
                const deadline = new Date(Date.now() + 5000);
                runtime.client.waitForReady(deadline, (err: Error | null) => {
                    if (err) {
                        reject(err);
                    } else {
                        resolve();
                    }
                });
            });
            runtime.connected = true;
            logger.info('Brain gRPC connected', { address: runtime.address, attempt });
            return;
        } catch (err: any) {
            const delay = Math.min(1000 * Math.pow(2, attempt - 1), 10000);
            logger.warn(
                `Brain gRPC not ready (attempt ${attempt}/${maxRetries}), retrying in ${delay}ms...`,
                {
                    address: runtime.address,
                    error: err.message,
                },
            );
            if (attempt === maxRetries) {
                runtime.connected = false;
                throw new Error(
                    `Failed to connect to Brain gRPC at ${runtime.address} after ${maxRetries} attempts`,
                );
            }
            await new Promise((resolve) => setTimeout(resolve, delay));
        }
    }
}

export function closeBrainClient(runtime: BrainClientRuntime): void {
    if (runtime.localMode) {
        runtime.connected = false;
        return;
    }
    if (runtime.client) {
        grpc.closeClient(runtime.client);
        runtime.connected = false;
    }
}

export async function callBrainMethod(
    runtime: BrainClientRuntime,
    method: string,
    request: any,
): Promise<any> {
    if (!runtime.connected) {
        throw new Error('Brain service not connected');
    }
    if (runtime.localMode) {
        return localCall(runtime, method, request);
    }

    return new Promise((resolve, reject) => {
        if (typeof runtime.client[method] !== 'function') {
            logger.warn(`Brain RPC method ${method} not available — returning empty`);
            resolve({});
            return;
        }
        runtime.client[method](request, (err: Error | null, response: any) => {
            if (err) {
                reject(err);
            } else {
                resolve(response);
            }
        });
    });
}

export async function localCall(
    runtime: BrainClientRuntime,
    method: string,
    request: any,
): Promise<any> {
    switch (method) {
        case 'ListProviderConfigs':
            return runtime.localStore.listProviderConfigs(String(request.workspace_id || 'local'));
        case 'SetProviderConfig':
            return runtime.localStore.setProviderConfig(request || {});
        case 'DeleteProviderConfig':
            runtime.localStore.deleteProviderConfig(
                String(request.workspace_id || 'local'),
                String(request.provider || ''),
            );
            return { success: true };
        case 'ListTools':
            return { tools: runtime.localStore.listLocalTools() };
        case 'SubmitFeedback':
            return { id: `feedback-${Date.now()}` };
        case 'ListWorkspaceMembers':
            return {
                members: runtime.localStore.listWorkspaceMembers(
                    String(request.workspaceId || request.workspace_id || 'local'),
                ),
            };
        case 'InviteWorkspaceMember':
        case 'RemoveWorkspaceMember':
            throw new Error('Unsupported in local mode');
        case 'GetCapabilities':
            return {
                capabilities: [
                    {
                        name: 'Telegram First Sessions',
                        description:
                            'Unified Telegram, desktop, CLI, and web sessions over the local gateway.',
                        status: 'active',
                        category: 'channels',
                        icon: '✈',
                    },
                    {
                        name: 'Native Runtime',
                        description: 'Local-native execution and daemon-backed task orchestration.',
                        status: 'active',
                        category: 'runtime',
                        icon: '⚙',
                    },
                    {
                        name: 'Media Artifacts',
                        description: 'Shared local media artifacts and delivery receipts.',
                        status: 'active',
                        category: 'media',
                        icon: '▣',
                    },
                ],
            };
        case 'GetMemoryGraph':
            return { nodes: [], links: [] };
        case 'ListProcesses': {
            const tasks =
                (await runtime.localTransport.request('task.list', { limit: 100 })).tasks || [];
            return {
                processes: tasks,
                running: tasks.filter((task: any) => task.status === 'running').length,
            };
        }
        default:
            logger.warn('Unsupported local Brain RPC method', { method });
            return {};
    }
}
