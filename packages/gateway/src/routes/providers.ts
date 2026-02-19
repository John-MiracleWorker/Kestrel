import { FastifyInstance, FastifyRequest, FastifyReply } from 'fastify';
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

    // ── GET /api/workspaces/:workspaceId/providers ────────────────────
    // List all provider configs for a workspace
    app.get(
        '/api/workspaces/:workspaceId/providers',
        { preHandler: [requireAuth, requireWorkspace] },
        async (req: FastifyRequest, reply: FastifyReply) => {
            const user = (req as any).user;
            const { workspaceId } = req.params as any;

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

    // ── PUT /api/workspaces/:workspaceId/providers/:provider ─────────
    // Create or update a provider config (admin+ only)
    app.put(
        '/api/workspaces/:workspaceId/providers/:provider',
        { preHandler: [requireAuth, requireWorkspace, requireRole('admin')] },
        async (req: FastifyRequest, reply: FastifyReply) => {
            const { workspaceId, provider } = req.params as any;
            const body = req.body as any;

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

            // API key handling — store encrypted in Redis, reference in DB
            if (body.apiKey) {
                const keyRef = `provider_key:${workspaceId}:${provider}`;
                await redis.set(keyRef, body.apiKey, 'EX', 365 * 24 * 60 * 60); // 1 year
                config.api_key_encrypted = keyRef;
            }

            const result = await brainClient.call('SetProviderConfig', config);
            return result;
        }
    );

    // ── DELETE /api/workspaces/:workspaceId/providers/:provider ──────
    // Remove a provider config (admin+ only)
    app.delete(
        '/api/workspaces/:workspaceId/providers/:provider',
        { preHandler: [requireAuth, requireWorkspace, requireRole('admin')] },
        async (req: FastifyRequest, _reply: FastifyReply) => {
            const { workspaceId, provider } = req.params as any;

            // Clean up Redis key
            await redis.del(`provider_key:${workspaceId}:${provider}`);

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
    // ── GET /api/workspaces/:workspaceId/providers/:provider/models ──
    app.get(
        '/api/workspaces/:workspaceId/providers/:provider/models',
        { preHandler: [requireAuth, requireWorkspace] },
        async (req: FastifyRequest, reply: FastifyReply) => {
            const { workspaceId, provider } = req.params as any;
            const { apiKey } = req.query as any;

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
}
