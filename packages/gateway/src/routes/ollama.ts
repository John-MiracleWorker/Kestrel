import { FastifyInstance } from 'fastify';
import { z } from 'zod';
import { ZodTypeProvider } from 'fastify-type-provider-zod';
import { requireAuth } from '../auth/middleware';
import { logger } from '../utils/logger';

const OLLAMA_PROBE_TIMEOUT_MS = 3000; // 3s per host (Docker adds latency)
const OLLAMA_PORT = 11434;
const CACHE_TTL_MS = 60_000; // 60s — return cached results instantly

// ── Result Cache ─────────────────────────────────────────────────────────────

let cachedServers: any[] = [];
let cacheTimestamp = 0;
let scanInProgress = false;

// ── Helpers ──────────────────────────────────────────────────────────────────

async function probeOllamaHost(
    host: string,
    timeoutMs = OLLAMA_PROBE_TIMEOUT_MS,
): Promise<{ url: string; host: string; models: any[]; score: number } | null> {
    // If host already looks like a full URL, use it directly
    const url = host.startsWith('http') ? host : `http://${host}:${OLLAMA_PORT}`;
    try {
        const controller = new AbortController();
        const timer = setTimeout(() => controller.abort(), timeoutMs);
        const resp = await fetch(`${url}/api/tags`, { signal: controller.signal });
        clearTimeout(timer);
        if (!resp.ok) return null;
        const data = (await resp.json()) as { models?: any[] };
        const models = (data.models || []).map((m: any) => ({
            name: m.name,
            size: m.size,
            parameterSize: m.details?.parameter_size || '',
            quantization: m.details?.quantization_level || '',
            family: m.details?.family || '',
        }));
        const score = scoreHost(models);
        return { url, host, models, score };
    } catch {
        return null;
    }
}

function scoreHost(models: any[]): number {
    if (!models.length) return 0;
    return Math.max(...models.map((m) => scoreModel(m.name)));
}

function scoreModel(name: string): number {
    if (!name) return 0;
    const n = name.toLowerCase();
    if (n.includes(':cloud')) return 1;
    const m = n.match(/:(\d+)b/);
    if (m) return parseInt(m[1]);
    for (const [tag, v] of [
        ['70b', 70],
        ['72b', 72],
        ['34b', 34],
        ['32b', 32],
        ['13b', 13],
        ['14b', 14],
        ['8b', 8],
        ['7b', 7],
        ['4b', 4],
        ['3b', 3],
    ] as [string, number][]) {
        if (n.includes(tag)) return v;
    }
    return 5;
}

function buildHostList(): string[] {
    // Priority hosts probed first (these resolve quickly from inside Docker)
    const localRuntime = ['native', 'local'].includes(
        (process.env.KESTREL_RUNTIME_MODE || '').toLowerCase(),
    );
    const priority = localRuntime
        ? ['127.0.0.1', 'localhost', 'host.docker.internal', '172.17.0.1']
        : ['host.docker.internal', '172.17.0.1'];

    // If a specific host is set, always include it
    const explicitHost = process.env.OLLAMA_HOST;
    if (explicitHost) {
        priority.unshift(explicitHost.replace(/^https?:\/\//, '').replace(/:.*$/, ''));
    }

    const subnet = process.env.OLLAMA_SCAN_SUBNET;
    const ranges: string[] = [];

    if (subnet) {
        const [base] = subnet.split('/');
        const parts = base.split('.');
        if (parts.length === 4) {
            for (let i = 1; i <= 254; i++) {
                ranges.push(`${parts[0]}.${parts[1]}.${parts[2]}.${i}`);
            }
        }
    } else {
        for (const prefix of ['192.168.1', '192.168.0', '10.0.0', '10.0.1']) {
            for (let i = 1; i <= 254; i++) {
                ranges.push(`${prefix}.${i}`);
            }
        }
    }

    // Deduplicate, keeping priority hosts at front
    const seen = new Set(priority);
    for (const h of ranges) {
        if (!seen.has(h)) {
            seen.add(h);
            priority.push(h);
        }
    }
    return priority;
}

async function doScan(): Promise<any[]> {
    const hosts = buildHostList();
    logger.info(`Ollama scan: probing ${hosts.length} hosts...`);

    // Phase 1: Probe priority hosts (first 4) with generous timeout
    const priorityBatch = hosts.slice(0, 4);
    const priorityResults: any[] = [];
    const settled1 = await Promise.allSettled(priorityBatch.map((h) => probeOllamaHost(h, 5000)));
    for (const r of settled1) {
        if (r.status === 'fulfilled' && r.value) priorityResults.push(r.value);
    }

    // Phase 2: Scan the rest in batches of 80
    const rest = hosts.slice(4);
    const batchSize = 80;
    const allResults = [...priorityResults];
    for (let i = 0; i < rest.length; i += batchSize) {
        const batch = rest.slice(i, i + batchSize);
        const settled = await Promise.allSettled(batch.map((h) => probeOllamaHost(h)));
        for (const r of settled) {
            if (r.status === 'fulfilled' && r.value) allResults.push(r.value);
        }
    }

    // Deduplicate by URL
    const seen = new Set<string>();
    const unique = allResults.filter((r) => {
        if (seen.has(r.url)) return false;
        seen.add(r.url);
        return true;
    });

    unique.sort((a, b) => b.score - a.score);
    logger.info(`Ollama scan: found ${unique.length} server(s)`);
    return unique;
}

// ── Route Plugin ─────────────────────────────────────────────────────────────

export default async function ollamaRoutes(app: FastifyInstance) {
    const typedApp = app.withTypeProvider<ZodTypeProvider>();

    // ── GET /api/ollama/servers ───────────────────────────────────────────────
    app.get(
        '/api/ollama/servers',
        {
            preHandler: [requireAuth],
        },
        async (_req, _reply) => {
            if (process.env.OLLAMA_DISABLE_SCAN === '1') {
                return { servers: [] };
            }

            const now = Date.now();

            // Return cached results if fresh
            if (cachedServers.length > 0 && now - cacheTimestamp < CACHE_TTL_MS) {
                return { servers: cachedServers, cached: true };
            }

            // If a scan is already running, return whatever we have
            if (scanInProgress) {
                return { servers: cachedServers, scanning: true };
            }

            scanInProgress = true;
            try {
                const servers = await doScan();
                cachedServers = servers;
                cacheTimestamp = Date.now();
                return { servers };
            } finally {
                scanInProgress = false;
            }
        },
    );

    // ── GET /api/ollama/servers/rescan ────────────────────────────────────────
    // Force a fresh scan, ignoring cache
    app.get(
        '/api/ollama/servers/rescan',
        {
            preHandler: [requireAuth],
        },
        async (_req, _reply) => {
            if (process.env.OLLAMA_DISABLE_SCAN === '1') {
                return { servers: [] };
            }

            scanInProgress = true;
            try {
                const servers = await doScan();
                cachedServers = servers;
                cacheTimestamp = Date.now();
                return { servers };
            } finally {
                scanInProgress = false;
            }
        },
    );

    // ── GET /api/ollama/servers/:host/models ──────────────────────────────────
    typedApp.get(
        '/api/ollama/servers/:host/models',
        {
            preHandler: [requireAuth],
            schema: { params: z.object({ host: z.string() }) },
        },
        async (req, reply) => {
            const { host } = req.params as { host: string };
            const decodedHost = decodeURIComponent(host);
            const result = await probeOllamaHost(decodedHost, 5000);
            if (!result) {
                return reply
                    .status(404)
                    .send({ error: `No Ollama server found at ${decodedHost}` });
            }
            return { models: result.models, score: result.score, url: result.url };
        },
    );

    // ── POST /api/ollama/pull ─────────────────────────────────────────────────
    typedApp.post(
        '/api/ollama/pull',
        {
            preHandler: [requireAuth],
            schema: {
                body: z.object({
                    host: z.string(),
                    model: z.string(),
                }),
            },
        },
        async (req, reply) => {
            const { host, model } = req.body as { host: string; model: string };
            const ollamaUrl = host.startsWith('http') ? host : `http://${host}:${OLLAMA_PORT}`;

            logger.info(`Ollama pull: ${model} on ${ollamaUrl}`);

            reply.raw.writeHead(200, {
                'Content-Type': 'text/event-stream',
                'Cache-Control': 'no-cache',
                Connection: 'keep-alive',
                'X-Accel-Buffering': 'no',
            });

            const sendEvent = (data: object) => {
                reply.raw.write(`data: ${JSON.stringify(data)}\n\n`);
            };

            try {
                const resp = await fetch(`${ollamaUrl}/api/pull`, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ name: model, stream: true }),
                });

                if (!resp.ok) {
                    sendEvent({ status: 'error', error: `Ollama returned ${resp.status}` });
                    reply.raw.end();
                    return reply;
                }

                const reader = resp.body!.getReader();
                const decoder = new TextDecoder();
                let buffer = '';

                while (true) {
                    const { done, value } = await reader.read();
                    if (done) break;
                    buffer += decoder.decode(value, { stream: true });
                    const lines = buffer.split('\n');
                    buffer = lines.pop() ?? '';
                    for (const line of lines) {
                        if (!line.trim()) continue;
                        try {
                            const obj = JSON.parse(line);
                            sendEvent({
                                status: obj.status || 'pulling',
                                completed: obj.completed,
                                total: obj.total,
                                digest: obj.digest,
                            });
                        } catch {
                            /* skip malformed */
                        }
                    }
                }

                sendEvent({ status: 'done', model });
            } catch (err: any) {
                logger.error(`Ollama pull failed: ${err.message}`);
                sendEvent({ status: 'error', error: err.message });
            }

            reply.raw.end();
            return reply;
        },
    );

    // ── DELETE /api/ollama/models ─────────────────────────────────────────────
    typedApp.delete(
        '/api/ollama/models',
        {
            preHandler: [requireAuth],
            schema: {
                body: z.object({
                    host: z.string(),
                    model: z.string(),
                }),
            },
        },
        async (req, reply) => {
            const { host, model } = req.body as { host: string; model: string };
            const ollamaUrl = host.startsWith('http') ? host : `http://${host}:${OLLAMA_PORT}`;
            try {
                const resp = await fetch(`${ollamaUrl}/api/delete`, {
                    method: 'DELETE',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ name: model }),
                });
                if (!resp.ok) {
                    return reply.status(400).send({ error: `Ollama returned ${resp.status}` });
                }
                // Invalidate cache
                cacheTimestamp = 0;
                return { success: true };
            } catch (err: any) {
                return reply.status(503).send({ error: err.message });
            }
        },
    );
}
