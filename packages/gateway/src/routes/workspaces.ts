import { FastifyInstance, FastifyRequest, FastifyReply } from 'fastify';
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

    // ── GET /api/workspaces ──────────────────────────────────────────
    app.get('/api/workspaces', { preHandler: [requireAuth] }, async (req: FastifyRequest) => {
        const user = (req as any).user;
        return { workspaces: await brainClient.listWorkspaces(user.id) };
    });

    // ── POST /api/workspaces ─────────────────────────────────────────
    app.post('/api/workspaces', { preHandler: [requireAuth] }, async (req: FastifyRequest, reply: FastifyReply) => {
        const user = (req as any).user;
        const { name } = req.body as any;

        if (!name || name.trim().length < 2) {
            return reply.status(400).send({ error: 'Workspace name must be at least 2 characters' });
        }

        try {
            const workspace = await brainClient.createWorkspace(user.id, name.trim());
            return { workspace };
        } catch (err: any) {
            logger.error('Create workspace failed', { error: err.message });
            return reply.status(400).send({ error: err.message });
        }
    });

    // ── GET /api/workspaces/:workspaceId ─────────────────────────────
    app.get('/api/workspaces/:workspaceId', {
        preHandler: [requireAuth, requireWorkspace],
    }, async (req: FastifyRequest) => {
        const { workspaceId } = req.params as any;
        const workspace = (req as any).workspace;
        return { workspace: { id: workspaceId, role: workspace.role } };
    });

    // ── PUT /api/workspaces/:workspaceId ─────────────────────────────
    app.put('/api/workspaces/:workspaceId', {
        preHandler: [requireAuth, requireWorkspace, requireRole('admin')],
    }, async (req: FastifyRequest, reply: FastifyReply) => {
        const { workspaceId } = req.params as any;
        const { name, description, settings } = req.body as any;

        try {
            const updated = await brainClient.updateWorkspace(workspaceId, { name, description, settings });
            return { workspace: updated };
        } catch (err: any) {
            logger.error('Update workspace failed', { error: err.message });
            return reply.status(400).send({ error: err.message });
        }
    });

    // ── DELETE /api/workspaces/:workspaceId ──────────────────────────
    app.delete('/api/workspaces/:workspaceId', {
        preHandler: [requireAuth, requireWorkspace, requireRole('owner')],
    }, async (req: FastifyRequest, reply: FastifyReply) => {
        const { workspaceId } = req.params as any;

        try {
            await brainClient.deleteWorkspace(workspaceId);
            return { success: true };
        } catch (err: any) {
            logger.error('Delete workspace failed', { error: err.message });
            return reply.status(400).send({ error: err.message });
        }
    });

    // ── Conversation Routes ──────────────────────────────────────────

    app.get('/api/workspaces/:workspaceId/conversations', {
        preHandler: [requireAuth, requireWorkspace],
    }, async (req: FastifyRequest) => {
        const user = (req as any).user;
        const { workspaceId } = req.params as any;
        return { conversations: await brainClient.listConversations(user.id, workspaceId) };
    });

    app.post('/api/workspaces/:workspaceId/conversations', {
        preHandler: [requireAuth, requireWorkspace],
    }, async (req: FastifyRequest) => {
        const user = (req as any).user;
        const { workspaceId } = req.params as any;
        const conversation = await brainClient.createConversation(user.id, workspaceId);
        return { conversation };
    });

    app.get('/api/workspaces/:workspaceId/conversations/:conversationId/messages', {
        preHandler: [requireAuth, requireWorkspace],
    }, async (req: FastifyRequest) => {
        const user = (req as any).user;
        const { workspaceId, conversationId } = req.params as any;
        return { messages: await brainClient.getMessages(user.id, workspaceId, conversationId) };
    });

    // ── Delete Conversation ──────────────────────────────────────────
    app.delete('/api/workspaces/:workspaceId/conversations/:conversationId', {
        preHandler: [requireAuth, requireWorkspace],
    }, async (req: FastifyRequest, reply: FastifyReply) => {
        const user = (req as any).user;
        const { workspaceId, conversationId } = req.params as any;
        try {
            const success = await brainClient.deleteConversation(user.id, workspaceId, conversationId);
            return { success };
        } catch (err: any) {
            return reply.status(500).send({ error: err.message });
        }
    });

    // ── Update Conversation (Rename) ─────────────────────────────────
    app.patch('/api/workspaces/:workspaceId/conversations/:conversationId', {
        preHandler: [requireAuth, requireWorkspace],
    }, async (req: FastifyRequest, reply: FastifyReply) => {
        const user = (req as any).user;
        const { workspaceId, conversationId } = req.params as any;
        const { title } = req.body as any;
        try {
            const conversation = await brainClient.updateConversation(user.id, workspaceId, conversationId, title);
            return { conversation };
        } catch (err: any) {
            return reply.status(500).send({ error: err.message });
        }
    });

    // ── Generate Title ───────────────────────────────────────────────
    app.post('/api/workspaces/:workspaceId/conversations/:conversationId/generate-title', {
        preHandler: [requireAuth, requireWorkspace],
    }, async (req: FastifyRequest, reply: FastifyReply) => {
        const user = (req as any).user;
        const { workspaceId, conversationId } = req.params as any;
        try {
            const title = await brainClient.generateTitle(user.id, workspaceId, conversationId);
            return { title };
        } catch (err: any) {
            return reply.status(500).send({ error: err.message });
        }
    });

    // ── Invitation Routes ────────────────────────────────────────────

    // Send an invite
    app.post('/api/workspaces/:workspaceId/invite', {
        preHandler: [requireAuth, requireWorkspace, requireRole('admin')],
    }, async (req: FastifyRequest, reply: FastifyReply) => {
        const { workspaceId } = req.params as any;
        const { email, role } = req.body as any;

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

    // Accept an invite
    app.post('/api/invitations/:token/accept', {
        preHandler: [requireAuth],
    }, async (req: FastifyRequest, reply: FastifyReply) => {
        const { token } = req.params as any;
        const user = (req as any).user;

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
}
