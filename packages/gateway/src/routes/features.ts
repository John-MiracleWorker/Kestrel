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
            const userId = (request as unknown as { userId: string }).userId;

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
            const userId = (request as unknown as { userId: string }).userId;
            const { limit } = request.query as { limit: number };

            try {
                const result = await brainClient.call('GetNotifications', { userId, limit });
                return reply.send({ notifications: result?.notifications || [] });
            } catch (error) {
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
                await brainClient.call('MarkNotificationRead', { notificationId });
                return reply.send({ success: true });
            } catch {
                return reply.status(500).send({ error: 'Failed to mark read' });
            }
        },
    );

    // ── P1: Mark All Read ────────────────────────────────────────────
    app.withTypeProvider<ZodTypeProvider>().post(
        '/api/notifications/read-all',
        { preHandler: [requireAuth] },
        async (request, reply) => {
            const userId = (request as unknown as { userId: string }).userId;
            try {
                await brainClient.call('MarkAllNotificationsRead', { userId });
                return reply.send({ success: true });
            } catch {
                return reply.status(500).send({ error: 'Failed to mark all read' });
            }
        },
    );

    // ── P2: List Installed Tools ─────────────────────────────────────
    app.withTypeProvider<ZodTypeProvider>().get(
        '/api/workspaces/:workspaceId/tools',
        {
            preHandler: [requireAuth, requireWorkspace],
            schema: { params: z.object({ workspaceId: z.string().uuid() }) },
        },
        async (request, reply) => {
            const { workspaceId } = request.params as { workspaceId: string };
            try {
                const result = await brainClient.call('ListInstalledTools', { workspaceId });
                return reply.send({ tools: result?.tools || [] });
            } catch {
                return reply.send({ tools: [] });
            }
        },
    );

    // ── P2: Install Tool ─────────────────────────────────────────────
    app.withTypeProvider<ZodTypeProvider>().post(
        '/api/workspaces/:workspaceId/tools',
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
                await brainClient.call('InstallTool', { workspaceId, ...body });
                return reply.send({ success: true, name: body.name });
            } catch (error) {
                logger.error('Tool install error:', error);
                return reply.status(500).send({ error: 'Failed to install tool' });
            }
        },
    );

    // ── P2: Uninstall Tool ───────────────────────────────────────────
    app.withTypeProvider<ZodTypeProvider>().delete(
        '/api/workspaces/:workspaceId/tools/:toolName',
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
                await brainClient.call('UninstallTool', { workspaceId, name: toolName });
                return reply.send({ success: true });
            } catch {
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
            const userId = (request as unknown as { userId: string }).userId;

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
}
