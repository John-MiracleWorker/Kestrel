import { FastifyInstance, FastifyRequest, FastifyReply } from 'fastify';
import { z } from 'zod';
import { ZodTypeProvider } from 'fastify-type-provider-zod';
import { requireAuth, requireWorkspace, requireRole, generateSecureToken } from '../auth/middleware';
import { BrainClient } from '../brain/client';
import { logger } from '../utils/logger';
import Redis from 'ioredis';

interface WorkspaceDeps {
    brainClient: BrainClient;
    redis: Redis;
}

/**
 * Workspace + invitation route plugin.
 */
export default async function workspaceRoutes(app: FastifyInstance, deps: WorkspaceDeps) {
    const { brainClient, redis } = deps;
    const typedApp = app.withTypeProvider<ZodTypeProvider>();

    // ── GET /api/workspaces ──────────────────────────────────────────
    typedApp.get('/api/workspaces', { preHandler: [requireAuth] }, async (req) => {
        const user = req.user!;
        return { workspaces: await brainClient.listWorkspaces(user.id) };
    });

    const createWorkspaceSchema = z.object({
        name: z.string().min(2, 'Workspace name must be at least 2 characters'),
    });
    type CreateWorkspaceBody = z.infer<typeof createWorkspaceSchema>;

    // ── POST /api/workspaces ─────────────────────────────────────────
    typedApp.post('/api/workspaces', {
        preHandler: [requireAuth],
        schema: { body: createWorkspaceSchema }
    }, async (req, reply) => {
        const user = req.user!;
        const { name } = req.body as CreateWorkspaceBody;

        try {
            const workspace = await brainClient.createWorkspace(user.id, name.trim());
            return { workspace };
        } catch (err: any) {
            logger.error('Create workspace failed', { error: err.message });
            return reply.status(400).send({ error: err.message });
        }
    });

    const workspaceParamsSchema = z.object({
        workspaceId: z.string(),
    });
    type WorkspaceParams = z.infer<typeof workspaceParamsSchema>;

    // ── GET /api/workspaces/:workspaceId ─────────────────────────────
    typedApp.get('/api/workspaces/:workspaceId', {
        preHandler: [requireAuth, requireWorkspace],
        schema: { params: workspaceParamsSchema }
    }, async (req) => {
        const { workspaceId } = req.params as WorkspaceParams;
        const workspace = req.workspace!;
        return { workspace: { id: workspaceId, role: workspace.role } };
    });

    const updateWorkspaceSchema = z.object({
        name: z.string().optional(),
        description: z.string().optional(),
        settings: z.any().optional(),
    });
    type UpdateWorkspaceBody = z.infer<typeof updateWorkspaceSchema>;

    // ── PUT /api/workspaces/:workspaceId ─────────────────────────────
    typedApp.put('/api/workspaces/:workspaceId', {
        preHandler: [requireAuth, requireWorkspace, requireRole('admin')],
        schema: { params: workspaceParamsSchema, body: updateWorkspaceSchema }
    }, async (req, reply) => {
        const { workspaceId } = req.params as WorkspaceParams;
        const { name, description, settings } = req.body as UpdateWorkspaceBody;

        try {
            const updated = await brainClient.updateWorkspace(workspaceId, { name, description, settings });
            return { workspace: updated };
        } catch (err: any) {
            logger.error('Update workspace failed', { error: err.message });
            return reply.status(400).send({ error: err.message });
        }
    });

    // ── DELETE /api/workspaces/:workspaceId ──────────────────────────
    typedApp.delete('/api/workspaces/:workspaceId', {
        preHandler: [requireAuth, requireWorkspace, requireRole('owner')],
        schema: { params: workspaceParamsSchema }
    }, async (req, reply) => {
        const { workspaceId } = req.params as WorkspaceParams;

        try {
            await brainClient.deleteWorkspace(workspaceId);
            return { success: true };
        } catch (err: any) {
            logger.error('Delete workspace failed', { error: err.message });
            return reply.status(400).send({ error: err.message });
        }
    });

    // ── Conversation Routes ──────────────────────────────────────────

    typedApp.get('/api/workspaces/:workspaceId/conversations', {
        preHandler: [requireAuth, requireWorkspace],
        schema: { params: workspaceParamsSchema }
    }, async (req) => {
        const user = req.user!;
        const { workspaceId } = req.params as WorkspaceParams;
        return { conversations: await brainClient.listConversations(user.id, workspaceId) };
    });

    typedApp.post('/api/workspaces/:workspaceId/conversations', {
        preHandler: [requireAuth, requireWorkspace],
        schema: { params: workspaceParamsSchema }
    }, async (req) => {
        const user = req.user!;
        const { workspaceId } = req.params as WorkspaceParams;
        const conversation = await brainClient.createConversation(user.id, workspaceId);
        return { conversation };
    });

    const conversationParamsSchema = z.object({
        workspaceId: z.string(),
        conversationId: z.string(),
    });
    type ConversationParams = z.infer<typeof conversationParamsSchema>;

    typedApp.get('/api/workspaces/:workspaceId/conversations/:conversationId/messages', {
        preHandler: [requireAuth, requireWorkspace],
        schema: { params: conversationParamsSchema }
    }, async (req) => {
        const user = req.user!;
        const { workspaceId, conversationId } = req.params as ConversationParams;
        return { messages: await brainClient.getMessages(user.id, workspaceId, conversationId) };
    });

    // ── Delete Conversation ──────────────────────────────────────────
    typedApp.delete('/api/workspaces/:workspaceId/conversations/:conversationId', {
        preHandler: [requireAuth, requireWorkspace],
        schema: { params: conversationParamsSchema }
    }, async (req, reply) => {
        const user = req.user!;
        const { workspaceId, conversationId } = req.params as ConversationParams;
        try {
            const success = await brainClient.deleteConversation(user.id, workspaceId, conversationId);
            return { success };
        } catch (err: any) {
            return reply.status(500).send({ error: err.message });
        }
    });

    const updateConversationSchema = z.object({
        title: z.string().min(1, 'Title cannot be empty')
    });
    type UpdateConversationBody = z.infer<typeof updateConversationSchema>;

    // ── Update Conversation (Rename) ─────────────────────────────────
    typedApp.patch('/api/workspaces/:workspaceId/conversations/:conversationId', {
        preHandler: [requireAuth, requireWorkspace],
        schema: { params: conversationParamsSchema, body: updateConversationSchema }
    }, async (req, reply) => {
        const user = req.user!;
        const { workspaceId, conversationId } = req.params as ConversationParams;
        const { title } = req.body as UpdateConversationBody;
        try {
            const conversation = await brainClient.updateConversation(user.id, workspaceId, conversationId, title);
            return { conversation };
        } catch (err: any) {
            return reply.status(500).send({ error: err.message });
        }
    });

    // ── Generate Title ───────────────────────────────────────────────
    typedApp.post('/api/workspaces/:workspaceId/conversations/:conversationId/generate-title', {
        preHandler: [requireAuth, requireWorkspace],
        schema: {
            params: conversationParamsSchema,
            body: z.any().optional()
        }
    }, async (req, reply) => {
        const user = req.user!;
        const { workspaceId, conversationId } = req.params as ConversationParams;
        try {
            const title = await brainClient.generateTitle(user.id, workspaceId, conversationId);
            return { title };
        } catch (err: any) {
            return reply.status(500).send({ error: err.message });
        }
    });




    // ── Invitation Routes ────────────────────────────────────────────

    const inviteSchema = z.object({
        email: z.string().email(),
        role: z.enum(['owner', 'admin', 'member', 'guest']).optional(),
    });
    type InviteBody = z.infer<typeof inviteSchema>;

    // Send an invite
    typedApp.post('/api/workspaces/:workspaceId/invite', {
        preHandler: [requireAuth, requireWorkspace, requireRole('admin')],
        schema: { params: workspaceParamsSchema, body: inviteSchema }
    }, async (req, reply) => {
        const { workspaceId } = req.params as WorkspaceParams;
        const { email, role } = req.body as InviteBody;

        if (!email) {
            return reply.status(400).send({ error: 'Email required' });
        }

        const inviteRole = role || 'member';
        const token = generateSecureToken(32);
        const expiresAt = Date.now() + 7 * 24 * 60 * 60 * 1000; // 7 days

        // Store invitation in Redis
        await redis.set(
            `invite:${token}`,
            JSON.stringify({ workspaceId, email, role: inviteRole, expiresAt }),
            'EX',
            7 * 24 * 60 * 60
        );

        logger.info('Workspace invitation created', { workspaceId, email, role: inviteRole });

        return {
            inviteToken: token,
            expiresAt: new Date(expiresAt).toISOString(),
            // In production, send email with invite link
            inviteUrl: `${process.env.WEB_BASE_URL || 'http://localhost:5173'}/invite/${token}`,
        };
    });

    const acceptInviteParamsSchema = z.object({
        token: z.string()
    });
    type AcceptInviteParams = z.infer<typeof acceptInviteParamsSchema>;

    // Accept an invite
    typedApp.post('/api/invitations/:token/accept', {
        preHandler: [requireAuth],
        schema: { params: acceptInviteParamsSchema }
    }, async (req, reply) => {
        const { token } = req.params as AcceptInviteParams;
        const user = req.user!;

        const raw = await redis.get(`invite:${token}`);
        if (!raw) {
            return reply.status(404).send({ error: 'Invitation not found or expired' });
        }

        const invite = JSON.parse(raw);

        if (invite.expiresAt < Date.now()) {
            await redis.del(`invite:${token}`);
            return reply.status(410).send({ error: 'Invitation expired' });
        }

        if (invite.email !== user.email) {
            return reply.status(403).send({ error: 'Invitation is for a different email' });
        }

        try {
            await brainClient.addWorkspaceMember(invite.workspaceId, user.id, invite.role);
            await redis.del(`invite:${token}`);

            return {
                success: true,
                workspaceId: invite.workspaceId,
                role: invite.role,
            };
        } catch (err: any) {
            logger.error('Accept invite failed', { error: err.message });
            return reply.status(400).send({ error: err.message });
        }
    });

    // ── GET /api/workspaces/:workspaceId/moltbook/activity ──────────
    typedApp.get('/api/workspaces/:workspaceId/moltbook/activity', {
        preHandler: [requireAuth, requireWorkspace],
    }, async (req, reply) => {
        const { workspaceId } = req.params as { workspaceId: string };
        const { limit } = req.query as { limit?: string };
        try {
            const result = await brainClient.call('GetMoltbookActivity', {
                workspace_id: workspaceId,
                limit: parseInt(limit || '20', 10),
            });
            return { activity: result.activity || [] };
        } catch (err: any) {
            logger.error('Moltbook activity fetch failed', { error: err.message });
            return { activity: [] };
        }
    });
}
