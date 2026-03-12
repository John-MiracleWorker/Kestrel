import { FastifyInstance } from 'fastify';
import { z } from 'zod';
import { ZodTypeProvider } from 'fastify-type-provider-zod';
import { hasRole, requireAuth, requireWorkspace } from '../auth/middleware';
import { BrainClient } from '../brain/client';

interface OperationsDeps {
    brainClient: BrainClient;
}

export default async function operationsRoutes(app: FastifyInstance, deps: OperationsDeps) {
    const { brainClient } = deps;
    const typedApp = app.withTypeProvider<ZodTypeProvider>();

    const workspaceParamsSchema = z.object({
        workspaceId: z.string(),
    });
    type WorkspaceParams = z.infer<typeof workspaceParamsSchema>;

    const taskParamsSchema = z.object({
        workspaceId: z.string(),
        taskId: z.string(),
    });
    type TaskParams = z.infer<typeof taskParamsSchema>;

    const statusQuerySchema = z.object({
        status: z.string().optional(),
    });

    const approvalAuditQuerySchema = z.object({
        taskId: z.string().optional(),
        status: z.string().optional(),
    });

    typedApp.get(
        '/api/workspaces/:workspaceId/operations/tasks',
        {
            preHandler: [requireAuth, requireWorkspace],
            schema: { params: workspaceParamsSchema, querystring: statusQuerySchema },
        },
        async (req) => {
            const user = req.user!;
            const { workspaceId } = req.params as WorkspaceParams;
            const { status } = req.query as z.infer<typeof statusQuerySchema>;
            const result = await brainClient.listOperatorTasks(workspaceId, user.id, status);
            return { tasks: result.tasks || [] };
        },
    );

    typedApp.get(
        '/api/workspaces/:workspaceId/operations/tasks/:taskId',
        {
            preHandler: [requireAuth, requireWorkspace],
            schema: { params: taskParamsSchema },
        },
        async (req) => {
            const user = req.user!;
            const { workspaceId, taskId } = req.params as TaskParams;
            const result = await brainClient.getTaskDetail(workspaceId, user.id, taskId);
            return { task: result.task || null };
        },
    );

    typedApp.get(
        '/api/workspaces/:workspaceId/operations/tasks/:taskId/timeline',
        {
            preHandler: [requireAuth, requireWorkspace],
            schema: { params: taskParamsSchema },
        },
        async (req) => {
            const user = req.user!;
            const { workspaceId, taskId } = req.params as TaskParams;
            const result = await brainClient.listTaskTimeline(workspaceId, user.id, taskId);
            return { events: result.events || [] };
        },
    );

    typedApp.get(
        '/api/workspaces/:workspaceId/operations/tasks/:taskId/checkpoints',
        {
            preHandler: [requireAuth, requireWorkspace],
            schema: { params: taskParamsSchema },
        },
        async (req) => {
            const user = req.user!;
            const { workspaceId, taskId } = req.params as TaskParams;
            const result = await brainClient.listTaskCheckpoints(workspaceId, user.id, taskId);
            return { checkpoints: result.checkpoints || [] };
        },
    );

    typedApp.get(
        '/api/workspaces/:workspaceId/operations/tasks/:taskId/artifacts',
        {
            preHandler: [requireAuth, requireWorkspace],
            schema: { params: taskParamsSchema },
        },
        async (req) => {
            const user = req.user!;
            const { workspaceId, taskId } = req.params as TaskParams;
            const result = await brainClient.listTaskArtifacts(workspaceId, user.id, taskId);
            return { artifacts: result.artifacts || [] };
        },
    );

    typedApp.get(
        '/api/workspaces/:workspaceId/operations/artifacts',
        {
            preHandler: [requireAuth, requireWorkspace],
            schema: { params: workspaceParamsSchema },
        },
        async (req) => {
            const user = req.user!;
            const { workspaceId } = req.params as WorkspaceParams;
            const result = await brainClient.listTaskArtifacts(workspaceId, user.id);
            return { artifacts: result.artifacts || [] };
        },
    );

    typedApp.get(
        '/api/workspaces/:workspaceId/operations/approvals',
        {
            preHandler: [requireAuth, requireWorkspace],
            schema: { params: workspaceParamsSchema, querystring: approvalAuditQuerySchema },
        },
        async (req) => {
            const user = req.user!;
            const { workspaceId } = req.params as WorkspaceParams;
            const { taskId, status } = req.query as z.infer<typeof approvalAuditQuerySchema>;
            const result = await brainClient.getApprovalAudit(workspaceId, user.id, {
                taskId,
                status,
            });
            return { approvals: result.approvals || [] };
        },
    );

    typedApp.get(
        '/api/workspaces/:workspaceId/operations/runtime-profile',
        {
            preHandler: [requireAuth, requireWorkspace],
            schema: { params: workspaceParamsSchema },
        },
        async (req) => {
            const user = req.user!;
            const { workspaceId } = req.params as WorkspaceParams;
            const includeSensitive = hasRole(req.workspace?.role, 'admin');
            const result = await brainClient.getRuntimeProfile(
                workspaceId,
                user.id,
                includeSensitive,
            );
            return { profile: result.profile || null };
        },
    );
}
