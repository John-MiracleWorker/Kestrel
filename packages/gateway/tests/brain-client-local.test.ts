import fs from 'fs';
import net from 'net';
import os from 'os';
import path from 'path';
import { afterEach, describe, expect, it, vi } from 'vitest';

const tempRoots: string[] = [];
const activeServers: net.Server[] = [];

function makeTempRoot(): string {
    const root = fs.mkdtempSync(path.join(os.tmpdir(), 'kestrel-brain-local-'));
    tempRoots.push(root);
    return root;
}

async function startControlServer(): Promise<number> {
    const server = net.createServer((socket) => {
        let buffer = '';
        socket.on('data', (chunk) => {
            buffer += chunk.toString();
            while (buffer.includes('\n')) {
                const newlineIndex = buffer.indexOf('\n');
                const line = buffer.slice(0, newlineIndex).trim();
                buffer = buffer.slice(newlineIndex + 1);
                if (!line) continue;
                const request = JSON.parse(line) as {
                    request_id: string;
                    method: string;
                    params?: Record<string, any>;
                };
                const write = (payload: Record<string, any>) => {
                    socket.write(
                        `${JSON.stringify({ request_id: request.request_id, ok: true, ...payload })}\n`,
                    );
                };

                switch (request.method) {
                    case 'status':
                        write({ done: true, result: { status: 'running' } });
                        break;
                    case 'chat':
                        write({
                            done: true,
                            result: {
                                message: `local:${request.params?.prompt || ''}`,
                                provider: 'fake',
                                model: 'local-test',
                            },
                        });
                        break;
                    case 'task.list':
                        write({
                            done: true,
                            result: {
                                tasks: [
                                    { id: 'task-1', goal: 'local task', status: 'running' },
                                    { id: 'task-2', goal: 'done task', status: 'completed' },
                                ],
                            },
                        });
                        break;
                    case 'runtime.profile':
                        write({
                            done: true,
                            result: {
                                local_models: {
                                    default_provider: 'ollama',
                                    default_model: 'qwen3:8b',
                                    providers: {
                                        ollama: { model: 'qwen3:8b' },
                                    },
                                },
                            },
                        });
                        break;
                    default:
                        write({ done: true, result: {} });
                        break;
                }
            }
        });
    });

    await new Promise<void>((resolve) => server.listen(0, '127.0.0.1', resolve));
    activeServers.push(server);
    const address = server.address();
    if (!address || typeof address === 'string') {
        throw new Error('Failed to bind control server');
    }
    return address.port;
}

afterEach(async () => {
    for (const server of activeServers.splice(0)) {
        await new Promise<void>((resolve, reject) =>
            server.close((error) => (error ? reject(error) : resolve())),
        );
    }
    for (const root of tempRoots.splice(0)) {
        fs.rmSync(root, { recursive: true, force: true });
    }
    delete process.env.KESTREL_HOME;
    delete process.env.KESTREL_RUNTIME_MODE;
    delete process.env.KESTREL_CONTROL_PORT;
    delete process.env.KESTREL_CONTROL_HOST;
    delete process.env.BRAIN_TRANSPORT;
    vi.resetModules();
});

describe('BrainClient local mode', () => {
    it('uses daemon transport for chat and task reads while persisting local operator state', async () => {
        const root = makeTempRoot();
        const port = await startControlServer();
        process.env.KESTREL_HOME = root;
        process.env.KESTREL_RUNTIME_MODE = 'native';
        process.env.KESTREL_CONTROL_HOST = '127.0.0.1';
        process.env.KESTREL_CONTROL_PORT = String(port);

        const { BrainClient } = await import('../src/brain/client');
        const client = new BrainClient('unused');
        await client.connect();

        const user = await client.createUser('local@example.com', 'supersecret', 'Local Operator');
        expect(user.email).toBe('local@example.com');

        const authed = await client.authenticateUser('local@example.com', 'supersecret');
        expect(authed.id).toBe(user.id);

        const workspaces = await client.listWorkspaces(user.id);
        expect(workspaces.length).toBeGreaterThan(0);

        const conversation = await client.createConversation(user.id, workspaces[0].id);
        const chunks: any[] = [];
        for await (const chunk of client.streamChat({
            userId: user.id,
            workspaceId: workspaces[0].id,
            conversationId: conversation.id,
            messages: [{ role: 0, content: 'hello from telegram' }],
            provider: '',
            model: '',
        })) {
            chunks.push(chunk);
        }

        expect(chunks[0].content_delta).toContain('local:hello from telegram');
        expect(chunks[chunks.length - 1].type).toBe(2);

        const messages = await client.getMessages(user.id, workspaces[0].id, conversation.id);
        expect(messages.map((message: any) => message.role)).toEqual(['user', 'assistant']);

        const tasks = await client.listTasks(user.id, workspaces[0].id);
        expect(tasks.tasks).toHaveLength(2);

        const models = await client.listModels('ollama', undefined, workspaces[0].id);
        expect(models).toEqual([{ id: 'qwen3:8b', name: 'qwen3:8b' }]);
    });
});
