import { FastifyInstance, FastifyRequest, FastifyReply } from 'fastify';
import { z } from 'zod';
import { ZodTypeProvider } from 'fastify-type-provider-zod';
import { requireAuth, requireWorkspace, requireRole } from '../auth/middleware';
import { BrainClient } from '../brain/client';
import { logger } from '../utils/logger';
import Redis from 'ioredis';

interface ProviderDeps {
    brainClient: BrainClient;
    redis: Redis;
}

/**
 * Provider configuration routes — per-workspace LLM settings.
 *
 * These routes let workspace admins configure which LLM provider,
 * model, temperature, and RAG settings to use for their workspace.
 */
export default async function providerRoutes(app: FastifyInstance, deps: ProviderDeps) {
    const { brainClient, redis } = deps;
    const typedApp = app.withTypeProvider<ZodTypeProvider>();

    const workspaceParamsSchema = z.object({
        workspaceId: z.string()
    });
    type WorkspaceParams = z.infer<typeof workspaceParamsSchema>;

    // ── GET /api/workspaces/:workspaceId/providers ────────────────────
    // List all provider configs for a workspace
    typedApp.get(
        '/api/workspaces/:workspaceId/providers',
        {
            preHandler: [requireAuth, requireWorkspace],
            schema: { params: workspaceParamsSchema }
        },
        async (req, reply) => {
            const user = req.user!;
            const { workspaceId } = req.params as WorkspaceParams;

            // Verify workspace membership
            const isMember = user.workspaces?.some((w: any) => w.id === workspaceId);
            if (!isMember) {
                return reply.status(403).send({ error: 'Not a member of this workspace' });
            }

            // Fetch from Brain service (which reads workspace_provider_config table)
            const configs = await brainClient.call('ListProviderConfigs', {
                workspace_id: workspaceId,
            });

            return configs;
        }
    );

    const providerParamsSchema = z.object({
        workspaceId: z.string(),
        provider: z.string()
    });
    type ProviderParams = z.infer<typeof providerParamsSchema>;

    const putProviderBodySchema = z.object({
        model: z.string().optional(),
        temperature: z.number().min(0).max(2).optional(),
        maxTokens: z.number().int().min(1).max(32768).optional(),
        systemPrompt: z.string().optional(),
        ragEnabled: z.boolean().optional(),
        ragTopK: z.number().int().min(1).max(20).optional(),
        ragMinSimilarity: z.number().min(0).max(1).optional(),
        isDefault: z.boolean().optional(),
        apiKey: z.string().optional(),
    });
    type PutProviderBody = z.infer<typeof putProviderBodySchema>;

    // ── PUT /api/workspaces/:workspaceId/providers/:provider ─────────
    // Create or update a provider config (admin+ only)
    typedApp.put(
        '/api/workspaces/:workspaceId/providers/:provider',
        {
            preHandler: [requireAuth, requireWorkspace, requireRole('admin')],
            schema: { params: providerParamsSchema, body: putProviderBodySchema }
        },
        async (req, reply) => {
            const { workspaceId, provider } = req.params as ProviderParams;
            const body = req.body as PutProviderBody;

            const validProviders = ['local', 'openai', 'anthropic', 'google'];
            if (!validProviders.includes(provider)) {
                return reply.status(400).send({
                    error: `Invalid provider. Must be one of: ${validProviders.join(', ')}`,
                });
            }

            // Validate settings
            const config: Record<string, any> = {
                workspace_id: workspaceId,
                provider,
                model: body.model || '',
                temperature: Math.max(0, Math.min(2, body.temperature ?? 0.7)),
                max_tokens: Math.max(1, Math.min(32768, body.maxTokens ?? 2048)),
                system_prompt: body.systemPrompt || '',
                rag_enabled: body.ragEnabled ?? true,
                rag_top_k: Math.max(1, Math.min(20, body.ragTopK ?? 5)),
                rag_min_similarity: Math.max(0, Math.min(1, body.ragMinSimilarity ?? 0.3)),
                is_default: body.isDefault ?? false,
            };

            // API key handling — Send to Brain service for secure encryption and storage
            if (body.apiKey) {
                config.api_key_encrypted = body.apiKey;
            }

            const result = await brainClient.call('SetProviderConfig', config);
            return result;
        }
    );

    // ── DELETE /api/workspaces/:workspaceId/providers/:provider ──────
    // Remove a provider config (admin+ only)
    typedApp.delete(
        '/api/workspaces/:workspaceId/providers/:provider',
        {
            preHandler: [requireAuth, requireWorkspace, requireRole('admin')],
            schema: { params: providerParamsSchema }
        },
        async (req, _reply) => {
            const { workspaceId, provider } = req.params as ProviderParams;

            // (Redis cleanup removed, legacy keys will expire naturally)

            await brainClient.call('DeleteProviderConfig', {
                workspace_id: workspaceId,
                provider,
            });

            return { success: true };
        }
    );

    // ── GET /api/providers ───────────────────────────────────────────
    // List available LLM providers (public info)
    app.get('/api/providers', async () => {
        return {
            providers: [
                {
                    id: 'local',
                    name: 'Local (llama.cpp)',
                    description: 'On-device inference via llama.cpp',
                    requiresApiKey: false,
                    models: ['auto'],
                },
                {
                    id: 'openai',
                    name: 'OpenAI',
                    description: 'GPT-4o, GPT-4o-mini, o1, o3-mini',
                    requiresApiKey: true,
                    models: ['gpt-4o', 'gpt-4o-mini', 'o1', 'o3-mini'],
                },
                {
                    id: 'anthropic',
                    name: 'Anthropic',
                    description: 'Claude 3.5 Sonnet, Claude 3.5 Haiku',
                    requiresApiKey: true,
                    models: ['claude-3-5-sonnet-20241022', 'claude-3-5-haiku-20241022'],
                },
                {
                    id: 'google',
                    name: 'Google',
                    description: 'Gemini 2.0 Flash, Gemini 1.5 Pro',
                    requiresApiKey: true,
                    models: ['gemini-2.0-flash', 'gemini-1.5-pro'],
                },
            ],
        };
    });


    const providerQuerySchema = z.object({
        apiKey: z.string().optional()
    });
    type ProviderQuery = z.infer<typeof providerQuerySchema>;

    // ── GET /api/workspaces/:workspaceId/providers/:provider/models ──
    typedApp.get(
        '/api/workspaces/:workspaceId/providers/:provider/models',
        {
            preHandler: [requireAuth, requireWorkspace],
            schema: { params: providerParamsSchema, querystring: providerQuerySchema }
        },
        async (req, reply) => {
            const { workspaceId, provider } = req.params as ProviderParams;
            const { apiKey } = req.query as ProviderQuery;

            logger.info(`Fetching models for ${provider} in ${workspaceId}`);
            try {
                const models = await brainClient.listModels(provider, apiKey, workspaceId);
                logger.info(`Found ${models.length} models`);
                return { models };
            } catch (err: any) {
                logger.error('Fetch models failed', { error: err.message });
                return { models: [] };
            }
        }
    );

    // ── GET /api/workspaces/:workspaceId/tools ──────────────────────
    // List available agent tools (matches brain ToolRegistry)
    typedApp.get(
        '/api/workspaces/:workspaceId/tools',
        {
            preHandler: [requireAuth, requireWorkspace],
            schema: { params: workspaceParamsSchema }
        },
        async (_req, _reply) => {
            // Static tool catalog matching brain's build_tool_registry()
            // TODO: Wire to brain gRPC ListTools when proto is updated
            return {
                tools: [
                    {
                        name: 'code_execute',
                        description: 'Execute Python, JavaScript, or shell commands in a sandboxed environment',
                        category: 'code',
                        riskLevel: 'medium',
                        enabled: true,
                    },
                    {
                        name: 'web_search',
                        description: 'Search the web for information using DuckDuckGo',
                        category: 'web',
                        riskLevel: 'low',
                        enabled: true,
                    },
                    {
                        name: 'web_browse',
                        description: 'Fetch and read the content of a web page',
                        category: 'web',
                        riskLevel: 'medium',
                        enabled: true,
                    },
                    {
                        name: 'file_read',
                        description: 'Read the contents of a file from the workspace',
                        category: 'file',
                        riskLevel: 'low',
                        enabled: true,
                    },
                    {
                        name: 'file_write',
                        description: 'Write content to a file in the workspace',
                        category: 'file',
                        riskLevel: 'medium',
                        enabled: true,
                    },
                    {
                        name: 'file_list',
                        description: 'List files and directories in a path',
                        category: 'file',
                        riskLevel: 'low',
                        enabled: true,
                    },
                    {
                        name: 'memory_store',
                        description: 'Store information in long-term memory for later retrieval',
                        category: 'memory',
                        riskLevel: 'low',
                        enabled: true,
                    },
                    {
                        name: 'memory_search',
                        description: 'Search long-term memory for relevant information',
                        category: 'memory',
                        riskLevel: 'low',
                        enabled: true,
                    },
                    {
                        name: 'data_analyze',
                        description: 'Analyze structured data (CSV, JSON) with statistical operations',
                        category: 'data',
                        riskLevel: 'low',
                        enabled: true,
                    },
                    {
                        name: 'ask_human',
                        description: 'Ask the user a question and wait for their response',
                        category: 'control',
                        riskLevel: 'low',
                        enabled: true,
                    },
                    {
                        name: 'task_complete',
                        description: 'Mark the current step or task as complete',
                        category: 'control',
                        riskLevel: 'low',
                        enabled: true,
                    },
                    {
                        name: 'create_skill',
                        description: 'Create a new reusable tool by defining a Python function (requires approval)',
                        category: 'skill',
                        riskLevel: 'high',
                        enabled: true,
                    },
                ],
            };
        }
    );
}
