/**
 * Auto-documentation generation routes.
 */
import { FastifyInstance } from 'fastify';
import { requireAuth, requireWorkspace } from '../auth/middleware';
import { logger } from '../utils/logger';
import { getPool } from '../db/pool';

export async function docsRoutes(app: FastifyInstance) {
    // ── Auto-Documentation: Generate ────────────────────────────────
    app.post<{ Params: { workspaceId: string }; Body: { category?: string } }>(
        '/api/workspaces/:workspaceId/docs/generate',
        { preHandler: [requireAuth, requireWorkspace] },
        async (request, reply) => {
            const { workspaceId } = request.params;
            const { category } = request.body || {};

            try {
                const pool = getPool();
                const convContext = await pool.query(
                    `SELECT title, created_at FROM conversations WHERE workspace_id = $1 ORDER BY created_at DESC LIMIT 20`,
                    [workspaceId]
                );

                const evidenceContext = await pool.query(
                    `SELECT description, decision_type, reasoning FROM evidence_chain
                     WHERE task_id IN (SELECT id FROM conversations WHERE workspace_id = $1)
                     ORDER BY created_at DESC LIMIT 30`,
                    [workspaceId]
                ).catch(() => ({ rows: [] }));

                const contextSummary = [
                    'Recent conversations:',
                    ...convContext.rows.map((c: any) => `- ${c.title} (${new Date(c.created_at).toLocaleDateString()})`),
                    '',
                    'Key decisions:',
                    ...evidenceContext.rows.map((e: any) => `- [${e.decision_type}] ${e.description}: ${e.reasoning || ''}`),
                ].join('\n');

                const geminiKey = process.env.GEMINI_API_KEY;
                const openaiKey = process.env.OPENAI_API_KEY;

                if (!geminiKey && !openaiKey) {
                    return reply.status(400).send({ error: 'No API key configured for doc generation' });
                }

                const systemPrompt = `You are a technical documentation generator. Based on the project context provided, generate comprehensive documentation.
Generate the documentation in this JSON format (no markdown fences):
{
  "docs": [
    {
      "id": "unique-id",
      "title": "Document Title",
      "category": "${category || 'General'}",
      "lastUpdated": "${new Date().toISOString()}",
      "content": "Full markdown content..."
    }
  ]
}

Generate 3-5 documents covering: architecture overview, API reference, key decisions, and setup guide.
Use real details from the context, not generic placeholders.`;

                let text = '';
                if (geminiKey) {
                    const url = `https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent?key=${geminiKey}`;
                    const res = await fetch(url, {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({
                            systemInstruction: { parts: [{ text: systemPrompt }] },
                            contents: [{ role: 'user', parts: [{ text: `Generate documentation based on this project context:\n\n${contextSummary}` }] }],
                            generationConfig: { temperature: 0.4, maxOutputTokens: 8192 },
                        }),
                    });
                    const data = await res.json() as any;
                    text = data?.candidates?.[0]?.content?.parts?.[0]?.text || '';
                } else if (openaiKey) {
                    const res = await fetch('https://api.openai.com/v1/chat/completions', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${openaiKey}` },
                        body: JSON.stringify({
                            model: 'gpt-4o',
                            messages: [
                                { role: 'system', content: systemPrompt },
                                { role: 'user', content: `Generate documentation based on this project context:\n\n${contextSummary}` },
                            ],
                            temperature: 0.4, max_tokens: 8192,
                        }),
                    });
                    const data = await res.json() as any;
                    text = data?.choices?.[0]?.message?.content || '';
                }

                try {
                    const cleaned = text.replace(/```json\s*/gi, '').replace(/```\s*/g, '').trim();
                    const parsed = JSON.parse(cleaned);
                    return reply.send({ docs: parsed.docs || [] });
                } catch {
                    return reply.send({ docs: [{ id: 'generated-overview', title: 'Generated Overview', category: 'General', lastUpdated: new Date().toISOString(), content: text }] });
                }
            } catch (error: any) {
                logger.error('Doc generation error', { error: error.message });
                return reply.status(500).send({ error: 'Documentation generation failed' });
            }
        },
    );
}
