import { FastifyInstance } from 'fastify';
import { z } from 'zod';
import { ZodTypeProvider } from 'fastify-type-provider-zod';
import { requireAuth } from '../auth/middleware';
import { logger } from '../utils/logger';

const LMSTUDIO_PROBE_TIMEOUT_MS = 3000;
const LMSTUDIO_PORT = parseInt(process.env.LMSTUDIO_PORT || '1234', 10);
const CACHE_TTL_MS = 60_000;

// ── Result Cache ─────────────────────────────────────────────────────────────
let cachedServers: any[] = [];
let cacheTimestamp = 0;
let scanInProgress = false;

// ── Helpers ──────────────────────────────────────────────────────────────────

async function probeLMStudioHost(
    host: string,
    timeoutMs = LMSTUDIO_PROBE_TIMEOUT_MS,
): Promise<{ url: string; host: string; models: any[]; score: number } | null> {
    const url = host.startsWith('http') ? host : `http://${host}:${LMSTUDIO_PORT}`;
    try {
        const controller = new AbortController();
        const timer = setTimeout(() => controller.abort(), timeoutMs);
        const resp = await fetch(`${url}/v1/models`, { signal: controller.signal });
        clearTimeout(timer);
        if (!resp.ok) return null;
        const data = (await resp.json()) as { data?: any[] };
        const models = (data.data || []).map((m: any) => ({
            name: m.id || '',
            ownedBy: m.owned_by || 'lmstudio',
            // Extract parameter size from model ID (e.g. "8B" from "Meta-Llama-3.1-8B-Instruct")
            parameterSize: extractParamSize(m.id || ''),
        }));
        const score = scoreHost(models);
        return { url, host, models, score };
    } catch {
        return null;
    }
}

function extractParamSize(name: string): string {
    const m = name.match(/(\d+)[Bb]/);
    return m ? `${m[1]}B` : '';
}

function scoreHost(models: any[]): number {
    if (!models.length) return 0;
    return Math.max(...models.map((m) => scoreModel(m.name)));
}

function scoreModel(name: string): number {
    if (!name) return 0;
    const n = name.toLowerCase();
    const m = n.match(/(\d+)b/);
    if (m) return parseInt(m[1]);
    return 5;
}

function buildHostList(): string[] {
    const priority = ['host.docker.internal', '172.17.0.1'];

    const explicitHost = process.env.LMSTUDIO_HOST;
    if (explicitHost) {
        priority.unshift(explicitHost.replace(/^https?:\/\//, '').replace(/:.*$/, ''));
    }

    const subnet = process.env.LMSTUDIO_SCAN_SUBNET;
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
    logger.info(`LM Studio scan: probing ${hosts.length} hosts...`);

    // Phase 1: Priority hosts
    const priorityBatch = hosts.slice(0, 4);
    const priorityResults: any[] = [];
    const settled1 = await Promise.allSettled(priorityBatch.map((h) => probeLMStudioHost(h, 5000)));
    for (const r of settled1) {
        if (r.status === 'fulfilled' && r.value) priorityResults.push(r.value);
    }

    // Phase 2: Scan rest in batches
    const rest = hosts.slice(4);
    const batchSize = 80;
    const allResults = [...priorityResults];
    for (let i = 0; i < rest.length; i += batchSize) {
        const batch = rest.slice(i, i + batchSize);
        const settled = await Promise.allSettled(batch.map((h) => probeLMStudioHost(h)));
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
    logger.info(`LM Studio scan: found ${unique.length} server(s)`);
    return unique;
}

// ── Route Plugin ─────────────────────────────────────────────────────────────

export default async function lmstudioRoutes(app: FastifyInstance) {
    const typedApp = app.withTypeProvider<ZodTypeProvider>();

    // ── GET /api/lmstudio/servers ─────────────────────────────────────────────
    app.get('/api/lmstudio/servers', { preHandler: [requireAuth] }, async (_req, _reply) => {
        if (process.env.LMSTUDIO_DISABLE_SCAN === '1') {
            return { servers: [] };
        }

        const now = Date.now();
        if (cachedServers.length > 0 && now - cacheTimestamp < CACHE_TTL_MS) {
            return { servers: cachedServers, cached: true };
        }

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
    });

    // ── GET /api/lmstudio/servers/rescan ──────────────────────────────────────
    app.get('/api/lmstudio/servers/rescan', { preHandler: [requireAuth] }, async (_req, _reply) => {
        if (process.env.LMSTUDIO_DISABLE_SCAN === '1') {
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
    });

    // ── GET /api/lmstudio/servers/:host/models ───────────────────────────────
    typedApp.get(
        '/api/lmstudio/servers/:host/models',
        {
            preHandler: [requireAuth],
            schema: { params: z.object({ host: z.string() }) },
        },
        async (req, reply) => {
            const { host } = req.params as { host: string };
            const decodedHost = decodeURIComponent(host);
            const result = await probeLMStudioHost(decodedHost, 5000);
            if (!result) {
                return reply
                    .status(404)
                    .send({ error: `No LM Studio server found at ${decodedHost}` });
            }
            return { models: result.models, score: result.score, url: result.url };
        },
    );
}
