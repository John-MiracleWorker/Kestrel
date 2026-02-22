/**
 * Vision Analysis Routes — Kestrel Vision (Screen Share Agent backend)
 *
 * Accepts a base64-encoded screenshot, sends it to a vision-capable LLM
 * (Gemini/OpenAI), and returns structured code analysis suggestions.
 */

import { FastifyInstance } from 'fastify';
import { requireAuth } from '../auth/middleware';
import { BrainClient } from '../brain/client';
import { logger } from '../utils/logger';

const VISION_SYSTEM_PROMPT = `You are Kestrel Vision, an AI code analyst that reviews screenshots of code editors, terminals, and development tools in real-time.

Analyze the screenshot and provide ONLY relevant, actionable observations. Focus on:
- Security issues (hardcoded secrets, weak auth, SQL injection, XSS)
- Bugs and logic errors visible in the code
- Performance concerns (N+1 queries, missing indexes, memory leaks)
- Missing error handling or edge cases
- Code quality issues (unused variables, dead code, naming)
- Architecture concerns visible from imports/structure

Rules:
1. ONLY report issues you can actually SEE in the screenshot. Never fabricate line numbers or file names.
2. If the screenshot shows a terminal/browser/non-code content, comment on what you observe.
3. If the image is too blurry or unclear, say so.
4. Be concise — each suggestion should be 1-2 sentences.
5. Include the approximate location (line number or section) when visible.

Respond in this exact JSON format (no markdown, no code fences):
{
  "suggestions": [
    {
      "type": "warning|info|suggestion",
      "title": "Short title (max 60 chars)",
      "description": "Detailed explanation (max 200 chars)",
      "lineRef": "filename:line or null if not visible"
    }
  ],
  "context": "Brief 1-line description of what you see on screen"
}

If there's nothing noteworthy, return: {"suggestions": [], "context": "description of screen"}`;

interface AnalyzeBody {
    image: string;
    mimeType?: string;
    context?: string;
}

export default async function visionRoutes(
    app: FastifyInstance,
    _deps: { brainClient: BrainClient },
) {
    app.post<{ Params: { workspaceId: string }; Body: AnalyzeBody }>(
        '/api/workspaces/:workspaceId/vision/analyze',
        { preHandler: [requireAuth] },
        async (request, reply) => {
            const { image, mimeType = 'image/png', context } = request.body;
            const user = (request as any).user;

            if (!image) {
                return reply.status(400).send({ error: 'Missing image data' });
            }

            logger.info(`Vision analyze from user ${user.id}, ~${Math.round(image.length / 1024)}KB`);

            try {
                const geminiKey = process.env.GEMINI_API_KEY;
                const openaiKey = process.env.OPENAI_API_KEY;
                let result: any;

                if (geminiKey) {
                    result = await callGeminiVision(geminiKey, 'gemini-2.0-flash', image, mimeType, context);
                } else if (openaiKey) {
                    result = await callOpenAIVision(openaiKey, 'gpt-4o', image, mimeType, context);
                } else {
                    return reply.status(400).send({
                        error: 'No vision-capable API key configured',
                        hint: 'Set GEMINI_API_KEY or OPENAI_API_KEY in your environment',
                    });
                }

                return reply.send(result);
            } catch (error: any) {
                logger.error('Vision analysis failed', { error: error.message });
                return reply.status(500).send({ error: 'Vision analysis failed', message: error.message });
            }
        },
    );
}

async function callGeminiVision(
    apiKey: string, model: string, imageBase64: string, mimeType: string, context?: string,
): Promise<any> {
    const userPrompt = context
        ? `Analyze this screenshot. Additional context: ${context}`
        : 'Analyze this screenshot of a development environment.';

    const url = `https://generativelanguage.googleapis.com/v1beta/models/${model}:generateContent?key=${apiKey}`;
    const response = await fetch(url, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
            systemInstruction: { parts: [{ text: VISION_SYSTEM_PROMPT }] },
            contents: [{
                role: 'user', parts: [
                    { text: userPrompt },
                    { inlineData: { mimeType, data: imageBase64 } },
                ]
            }],
            generationConfig: { temperature: 0.3, maxOutputTokens: 2048 },
        }),
    });

    if (!response.ok) {
        const errorText = await response.text();
        throw new Error(`Gemini API error ${response.status}: ${errorText.slice(0, 200)}`);
    }

    const data = await response.json() as any;
    const text = data?.candidates?.[0]?.content?.parts?.[0]?.text || '';
    return parseVisionResponse(text);
}

async function callOpenAIVision(
    apiKey: string, model: string, imageBase64: string, mimeType: string, context?: string,
): Promise<any> {
    const userPrompt = context
        ? `Analyze this screenshot. Additional context: ${context}`
        : 'Analyze this screenshot of a development environment.';

    const response = await fetch('https://api.openai.com/v1/chat/completions', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${apiKey}` },
        body: JSON.stringify({
            model,
            messages: [
                { role: 'system', content: VISION_SYSTEM_PROMPT },
                {
                    role: 'user', content: [
                        { type: 'text', text: userPrompt },
                        { type: 'image_url', image_url: { url: `data:${mimeType};base64,${imageBase64}`, detail: 'high' } },
                    ]
                },
            ],
            temperature: 0.3,
            max_tokens: 2048,
        }),
    });

    if (!response.ok) {
        const errorText = await response.text();
        throw new Error(`OpenAI API error ${response.status}: ${errorText.slice(0, 200)}`);
    }

    const data = await response.json() as any;
    const text = data?.choices?.[0]?.message?.content || '';
    return parseVisionResponse(text);
}

function parseVisionResponse(text: string): any {
    try {
        const cleaned = text.replace(/```json\s*/gi, '').replace(/```\s*/g, '').trim();
        const parsed = JSON.parse(cleaned);

        if (parsed.suggestions && Array.isArray(parsed.suggestions)) {
            return {
                suggestions: parsed.suggestions.map((s: any) => ({
                    id: `vision-${Date.now()}-${Math.random().toString(36).slice(2, 6)}`,
                    type: ['warning', 'info', 'suggestion'].includes(s.type) ? s.type : 'info',
                    title: String(s.title || '').slice(0, 80),
                    description: String(s.description || '').slice(0, 300),
                    lineRef: s.lineRef || null,
                    timestamp: Date.now(),
                })),
                context: parsed.context || '',
            };
        }
        return { suggestions: [], context: text.slice(0, 200) };
    } catch {
        logger.warn('Vision response was not valid JSON, wrapping as info');
        return {
            suggestions: [{
                id: `vision-${Date.now()}`,
                type: 'info',
                title: 'Screen Analysis',
                description: text.slice(0, 300),
                lineRef: null,
                timestamp: Date.now(),
            }],
            context: 'raw analysis',
        };
    }
}
