/**
 * Feature routes — P1 through P3 capabilities.
 *
 * These routes proxy through Brain gRPC using the generic `call()` method.
 * Brain-side handlers will be matched to these RPC names.
 *
 * P1: Notifications
 * P2: Feedback, Tools (MCP)
 * P3: Members
 */
import { FastifyInstance } from 'fastify';
import { z } from 'zod';
import { ZodTypeProvider } from 'fastify-type-provider-zod';
import { requireAuth, requireWorkspace } from '../auth/middleware';
import { BrainClient } from '../brain/client';
import { logger } from '../utils/logger';
import { getPool } from '../db/pool';

interface FeatureDeps {
    brainClient: BrainClient;
}

export async function featureRoutes(app: FastifyInstance, deps: FeatureDeps) {
    const { brainClient } = deps;

    // ── P2: Message Feedback ─────────────────────────────────────────
    app.withTypeProvider<ZodTypeProvider>().post(
        '/api/workspaces/:workspaceId/feedback',
        {
            preHandler: [requireAuth, requireWorkspace],
            schema: {
                params: z.object({ workspaceId: z.string().uuid() }),
                body: z.object({
                    conversationId: z.string().uuid(),
                    messageId: z.string(),
                    rating: z.number().int().min(-1).max(1),
                    comment: z.string().optional().default(''),
                }),
            },
        },
        async (request, reply) => {
            const { workspaceId } = request.params as { workspaceId: string };
            const body = request.body as {
                conversationId: string; messageId: string; rating: number; comment: string;
            };
            const userId = request.user!.id;

            try {
                const result = await brainClient.call('SubmitFeedback', {
                    userId, workspaceId, ...body,
                });
                return reply.send({ success: true, id: result?.id || '' });
            } catch (error) {
                logger.error('Feedback error:', error);
                return reply.status(500).send({ error: 'Failed to save feedback' });
            }
        },
    );

    // ── P1: Get Notifications ────────────────────────────────────────
    app.withTypeProvider<ZodTypeProvider>().get(
        '/api/notifications',
        {
            preHandler: [requireAuth],
            schema: {
                querystring: z.object({
                    limit: z.coerce.number().int().min(1).max(50).optional().default(20),
                }),
            },
        },
        async (request, reply) => {
            const userId = request.user!.id;
            const { limit } = request.query as { limit: number };

            try {
                const pool = getPool();
                const result = await pool.query(
                    `SELECT id, type, title, body, source, data, read, created_at
                     FROM notifications
                     WHERE user_id = $1 AND read = false
                     ORDER BY created_at DESC
                     LIMIT $2`,
                    [userId, limit]
                );
                return reply.send({ notifications: result.rows });
            } catch (error) {
                logger.error('Failed to fetch notifications', { error });
                return reply.send({ notifications: [] });
            }
        },
    );

    // ── P1: Mark Notification Read ───────────────────────────────────
    app.withTypeProvider<ZodTypeProvider>().post(
        '/api/notifications/:notificationId/read',
        {
            preHandler: [requireAuth],
            schema: {
                params: z.object({ notificationId: z.string().uuid() }),
            },
        },
        async (request, reply) => {
            const { notificationId } = request.params as { notificationId: string };
            try {
                const pool = getPool();
                await pool.query(
                    `UPDATE notifications SET read = true WHERE id = $1`,
                    [notificationId]
                );
                return reply.send({ success: true });
            } catch (error) {
                logger.error('Failed to mark read', { error });
                return reply.status(500).send({ error: 'Failed to mark read' });
            }
        },
    );

    // ── P1: Mark All Read ────────────────────────────────────────────
    app.withTypeProvider<ZodTypeProvider>().post(
        '/api/notifications/read-all',
        { preHandler: [requireAuth] },
        async (request, reply) => {
            const userId = request.user!.id;
            try {
                const pool = getPool();
                await pool.query(
                    `UPDATE notifications SET read = true WHERE user_id = $1 AND read = false`,
                    [userId]
                );
                return reply.send({ success: true });
            } catch (error) {
                logger.error('Failed to mark all read', { error });
                return reply.status(500).send({ error: 'Failed to mark all read' });
            }
        },
    );

    // ── P2: List Installed Tools ─────────────────────────────────────
    app.withTypeProvider<ZodTypeProvider>().get(
        '/api/workspaces/:workspaceId/mcp-tools',
        {
            preHandler: [requireAuth, requireWorkspace],
            schema: { params: z.object({ workspaceId: z.string().uuid() }) },
        },
        async (request, reply) => {
            const { workspaceId } = request.params as { workspaceId: string };
            try {
                const pool = getPool();
                const result = await pool.query(
                    `SELECT id, name, description, server_url, transport, config, enabled, installed_at, updated_at
                     FROM installed_tools
                     WHERE workspace_id = $1
                     ORDER BY installed_at DESC`,
                    [workspaceId]
                );
                return reply.send({ tools: result.rows });
            } catch (err) {
                logger.error('List installed tools error:', err);
                return reply.send({ tools: [] });
            }
        },
    );

    // ── P2: Install Tool ─────────────────────────────────────────────
    app.withTypeProvider<ZodTypeProvider>().post(
        '/api/workspaces/:workspaceId/mcp-tools',
        {
            preHandler: [requireAuth, requireWorkspace],
            schema: {
                params: z.object({ workspaceId: z.string().uuid() }),
                body: z.object({
                    name: z.string().min(1).max(100),
                    description: z.string().optional().default(''),
                    serverUrl: z.string().min(1),
                    transport: z.enum(['stdio', 'http', 'sse']).optional().default('stdio'),
                    config: z.record(z.string(), z.unknown()).optional().default({}),
                }),
            },
        },
        async (request, reply) => {
            const { workspaceId } = request.params as { workspaceId: string };
            const body = request.body as {
                name: string; description: string; serverUrl: string;
                transport: string; config: Record<string, unknown>;
            };

            try {
                const pool = getPool();
                await pool.query(
                    `INSERT INTO installed_tools (workspace_id, name, description, server_url, transport, config)
                     VALUES ($1, $2, $3, $4, $5, $6::jsonb)
                     ON CONFLICT (workspace_id, name)
                     DO UPDATE SET server_url = $4, transport = $5, config = $6::jsonb,
                                   description = $3, updated_at = NOW(), enabled = true`,
                    [workspaceId, body.name, body.description, body.serverUrl, body.transport, JSON.stringify(body.config)]
                );
                logger.info(`MCP tool installed: ${body.name} in workspace ${workspaceId}`);
                return reply.send({ success: true, name: body.name });
            } catch (error) {
                logger.error('Tool install error:', error);
                return reply.status(500).send({ error: 'Failed to install tool' });
            }
        },
    );

    // ── P2: Uninstall Tool ───────────────────────────────────────────
    app.withTypeProvider<ZodTypeProvider>().delete(
        '/api/workspaces/:workspaceId/mcp-tools/:toolName',
        {
            preHandler: [requireAuth, requireWorkspace],
            schema: {
                params: z.object({
                    workspaceId: z.string().uuid(),
                    toolName: z.string(),
                }),
            },
        },
        async (request, reply) => {
            const { workspaceId, toolName } = request.params as {
                workspaceId: string; toolName: string;
            };
            try {
                const pool = getPool();
                await pool.query(
                    'DELETE FROM installed_tools WHERE workspace_id = $1 AND name = $2',
                    [workspaceId, toolName]
                );
                logger.info(`MCP tool uninstalled: ${toolName} from workspace ${workspaceId}`);
                return reply.send({ success: true });
            } catch (err) {
                logger.error('Tool uninstall error:', err);
                return reply.status(500).send({ error: 'Failed to uninstall' });
            }
        },
    );

    // ── P3: List Workspace Members ───────────────────────────────────
    app.withTypeProvider<ZodTypeProvider>().get(
        '/api/workspaces/:workspaceId/members',
        {
            preHandler: [requireAuth, requireWorkspace],
            schema: { params: z.object({ workspaceId: z.string().uuid() }) },
        },
        async (request, reply) => {
            const { workspaceId } = request.params as { workspaceId: string };
            try {
                const result = await brainClient.call('ListWorkspaceMembers', { workspaceId });
                return reply.send({ members: result?.members || [] });
            } catch {
                return reply.send({ members: [] });
            }
        },
    );

    // ── P3: Invite Member ────────────────────────────────────────────
    app.withTypeProvider<ZodTypeProvider>().post(
        '/api/workspaces/:workspaceId/members/invite',
        {
            preHandler: [requireAuth, requireWorkspace],
            schema: {
                params: z.object({ workspaceId: z.string().uuid() }),
                body: z.object({
                    email: z.string().email(),
                    role: z.enum(['admin', 'member', 'viewer']).optional().default('member'),
                }),
            },
        },
        async (request, reply) => {
            const { workspaceId } = request.params as { workspaceId: string };
            const { email, role } = request.body as { email: string; role: string };
            const userId = request.user!.id;

            try {
                const result = await brainClient.call('InviteWorkspaceMember', {
                    workspaceId, invitedBy: userId, email, role,
                });
                return reply.send({
                    success: true,
                    inviteLink: result?.inviteLink || '',
                    expiresAt: result?.expiresAt || '',
                });
            } catch (error) {
                logger.error('Invite error:', error);
                return reply.status(500).send({ error: 'Failed to create invite' });
            }
        },
    );

    // ── P3: Remove Member ────────────────────────────────────────────
    app.withTypeProvider<ZodTypeProvider>().delete(
        '/api/workspaces/:workspaceId/members/:memberId',
        {
            preHandler: [requireAuth, requireWorkspace],
            schema: {
                params: z.object({
                    workspaceId: z.string().uuid(),
                    memberId: z.string().uuid(),
                }),
            },
        },
        async (request, reply) => {
            const { workspaceId, memberId } = request.params as {
                workspaceId: string; memberId: string;
            };
            try {
                await brainClient.call('RemoveWorkspaceMember', { workspaceId, memberId });
                return reply.send({ success: true });
            } catch {
                return reply.status(500).send({ error: 'Failed to remove member' });
            }
        },
    );

    // ── P2: Search MCP Servers (Smithery registry) ───────────────────
    const BUILTIN_MCP_CATALOG: Array<{ name: string; description: string; transport: string; category: string; requires_env?: string[] }> = [
        { name: '@modelcontextprotocol/server-filesystem', description: 'Read/write local files and directories', transport: 'stdio', category: 'files' },
        { name: '@modelcontextprotocol/server-github', description: 'GitHub repos, issues, PRs, and code search', transport: 'stdio', category: 'dev', requires_env: ['GITHUB_PERSONAL_ACCESS_TOKEN'] },
        { name: '@modelcontextprotocol/server-postgres', description: 'Query PostgreSQL databases', transport: 'stdio', category: 'data', requires_env: ['POSTGRES_URL'] },
        { name: '@modelcontextprotocol/server-sqlite', description: 'Query SQLite databases', transport: 'stdio', category: 'data' },
        { name: '@modelcontextprotocol/server-slack', description: 'Read/send Slack messages and channels', transport: 'stdio', category: 'comms', requires_env: ['SLACK_BOT_TOKEN'] },
        { name: '@modelcontextprotocol/server-puppeteer', description: 'Browser automation and web scraping', transport: 'stdio', category: 'web' },
        { name: '@modelcontextprotocol/server-brave-search', description: 'Web search via Brave Search API', transport: 'stdio', category: 'web', requires_env: ['BRAVE_API_KEY'] },
        { name: '@modelcontextprotocol/server-memory', description: 'Persistent key-value memory storage', transport: 'stdio', category: 'memory' },
        { name: '@modelcontextprotocol/server-google-maps', description: 'Google Maps geocoding, directions, places', transport: 'stdio', category: 'geo', requires_env: ['GOOGLE_MAPS_API_KEY'] },
        { name: '@modelcontextprotocol/server-fetch', description: 'HTTP requests to external APIs', transport: 'stdio', category: 'web' },
        { name: '@modelcontextprotocol/server-sequential-thinking', description: 'Step-by-step reasoning and problem solving', transport: 'stdio', category: 'reasoning' },
        { name: '@modelcontextprotocol/server-everything', description: 'Kitchen-sink demo of all MCP features', transport: 'stdio', category: 'demo' },
    ];

    app.withTypeProvider<ZodTypeProvider>().get(
        '/api/mcp/search',
        {
            preHandler: [requireAuth],
            schema: {
                querystring: z.object({
                    q: z.string().min(1).max(100),
                }),
            },
        },
        async (request, reply) => {
            const { q } = request.query as { q: string };
            const query = q.toLowerCase();

            // Search built-in catalog first
            const builtinResults = BUILTIN_MCP_CATALOG
                .filter(s => s.name.toLowerCase().includes(query) || s.description.toLowerCase().includes(query) || s.category.includes(query))
                .map(s => ({ ...s, source: 'official' }));

            // Search Smithery registry
            let smitheryResults: Array<{ name: string; description: string; transport: string; source: string }> = [];
            try {
                const res = await fetch(`https://registry.smithery.ai/servers?q=${encodeURIComponent(q)}&pageSize=10`, {
                    headers: { 'Accept': 'application/json' },
                    signal: AbortSignal.timeout(5000),
                });
                if (res.ok) {
                    const data = await res.json() as { servers?: Array<{ qualifiedName: string; displayName: string; description: string }> };
                    smitheryResults = (data.servers || []).map(s => ({
                        name: s.qualifiedName || s.displayName,
                        description: (s.description || '').slice(0, 200),
                        transport: 'stdio',
                        source: 'smithery',
                    }));
                }
            } catch {
                // Smithery timeout or unavailable — return builtin only
            }

            // Deduplicate and combine
            const seen = new Set<string>();
            const results = [...builtinResults, ...smitheryResults].filter(r => {
                if (seen.has(r.name)) return false;
                seen.add(r.name);
                return true;
            });

            return reply.send({ results: results.slice(0, 20) });
        },
    );

    // ── Background Processes / Scheduled Jobs ───────────────────────
    app.withTypeProvider<ZodTypeProvider>().get(
        '/api/workspaces/:workspaceId/processes',
        {
            preHandler: [requireAuth, requireWorkspace],
            schema: { params: z.object({ workspaceId: z.string().uuid() }) },
        },
        async (request, reply) => {
            const { workspaceId } = (request as any).params;
            try {
                const result = await brainClient.call('ListProcesses', { workspaceId });
                return reply.send({
                    processes: result?.processes || [],
                    running: result?.running || 0,
                });
            } catch {
                // Fallback: return mock structure if Brain method not ready
                return reply.send({
                    processes: [],
                    running: 0,
                    hint: 'Brain ListProcesses not implemented yet — showing empty',
                });
            }
        },
    );
}

