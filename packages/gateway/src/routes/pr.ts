/**
 * PR review routes — GitHub integration with LLM analysis.
 */
import { FastifyInstance } from 'fastify';
import { requireAuth, requireWorkspace } from '../auth/middleware';
import { logger } from '../utils/logger';

export async function prRoutes(app: FastifyInstance) {
    // ── PR Review: Analyze GitHub PR ─────────────────────────────────
    app.post<{
        Params: { workspaceId: string };
        Body: { owner: string; repo: string; prNumber: number; githubToken?: string };
    }>(
        '/api/workspaces/:workspaceId/pr/review',
        { preHandler: [requireAuth, requireWorkspace] },
        async (request, reply) => {
            const { owner, repo, prNumber, githubToken } = request.body;
            const token = githubToken || process.env.GITHUB_TOKEN;

            if (!token) {
                return reply.status(400).send({
                    error: 'GitHub token required',
                    hint: 'Set GITHUB_TOKEN env var or pass githubToken in request body',
                });
            }

            try {
                const prRes = await fetch(`https://api.github.com/repos/${owner}/${repo}/pulls/${prNumber}`, {
                    headers: { Authorization: `token ${token}`, Accept: 'application/vnd.github.v3+json' },
                });
                if (!prRes.ok) throw new Error(`GitHub API error: ${prRes.status}`);
                const prData = await prRes.json() as any;

                const diffRes = await fetch(`https://api.github.com/repos/${owner}/${repo}/pulls/${prNumber}`, {
                    headers: { Authorization: `token ${token}`, Accept: 'application/vnd.github.v3.diff' },
                });
                if (!diffRes.ok) throw new Error(`GitHub diff error: ${diffRes.status}`);
                const diff = await diffRes.text();
                const truncatedDiff = diff.length > 15000 ? diff.slice(0, 15000) + '\n... (diff truncated)' : diff;

                const geminiKey = process.env.GEMINI_API_KEY;
                const openaiKey = process.env.OPENAI_API_KEY;

                if (!geminiKey && !openaiKey) {
                    return reply.status(400).send({ error: 'No LLM API key configured for PR review' });
                }

                const prReviewPrompt = `You are a senior code reviewer. Analyze this pull request and provide a thorough review.

PR Title: ${prData.title}
PR Description: ${prData.body || 'No description'}
Author: ${prData.user?.login}
Files Changed: ${prData.changed_files}
Additions: ${prData.additions}, Deletions: ${prData.deletions}

Diff:
${truncatedDiff}

Respond in this JSON format (no markdown fences):
{
  "summary": "1-2 sentence summary of the PR",
  "verdict": "approve|request_changes|comment",
  "score": 1-10,
  "issues": [
    {
      "severity": "critical|warning|suggestion|nit",
      "file": "filename",
      "line": "line number or range (string)",
      "title": "Issue title",
      "description": "Detailed description",
      "suggestion": "Suggested fix or improvement"
    }
  ],
  "positives": ["Things done well"],
  "risks": ["Potential risks or concerns"]
}`;

                let text = '';
                if (geminiKey) {
                    const url = `https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent?key=${geminiKey}`;
                    const res = await fetch(url, {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({
                            contents: [{ role: 'user', parts: [{ text: prReviewPrompt }] }],
                            generationConfig: { temperature: 0.3, maxOutputTokens: 4096 },
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
                            messages: [{ role: 'user', content: prReviewPrompt }],
                            temperature: 0.3, max_tokens: 4096,
                        }),
                    });
                    const data = await res.json() as any;
                    text = data?.choices?.[0]?.message?.content || '';
                }

                try {
                    const cleaned = text.replace(/```json\s*/gi, '').replace(/```\s*/g, '').trim();
                    const review = JSON.parse(cleaned);
                    return reply.send({
                        review,
                        pr: {
                            title: prData.title, number: prData.number, author: prData.user?.login,
                            url: prData.html_url, state: prData.state,
                            additions: prData.additions, deletions: prData.deletions,
                            changedFiles: prData.changed_files,
                        },
                    });
                } catch {
                    return reply.send({
                        review: { summary: text.slice(0, 500), verdict: 'comment', score: 5, issues: [], positives: [], risks: [] },
                        pr: { title: prData.title, number: prData.number, author: prData.user?.login, url: prData.html_url },
                    });
                }
            } catch (error: any) {
                logger.error('PR review error', { error: error.message });
                return reply.status(500).send({ error: `PR review failed: ${error.message}` });
            }
        },
    );

    // ── PR Review: List PRs from repo ───────────────────────────────
    app.get<{
        Params: { workspaceId: string };
        Querystring: { owner: string; repo: string; state?: string };
    }>(
        '/api/workspaces/:workspaceId/pr/list',
        { preHandler: [requireAuth, requireWorkspace] },
        async (request, reply) => {
            const { owner, repo, state = 'open' } = request.query as any;
            const token = process.env.GITHUB_TOKEN;

            if (!token) {
                return reply.status(400).send({ error: 'GITHUB_TOKEN not configured' });
            }

            try {
                const res = await fetch(
                    `https://api.github.com/repos/${owner}/${repo}/pulls?state=${state}&per_page=20`,
                    { headers: { Authorization: `token ${token}`, Accept: 'application/vnd.github.v3+json' } },
                );
                if (!res.ok) throw new Error(`GitHub API error: ${res.status}`);

                const prs = await res.json() as any[];
                return reply.send({
                    prs: prs.map((pr: any) => ({
                        number: pr.number, title: pr.title, author: pr.user?.login,
                        state: pr.state, url: pr.html_url, createdAt: pr.created_at,
                        additions: pr.additions, deletions: pr.deletions,
                        labels: pr.labels?.map((l: any) => l.name) || [],
                    })),
                });
            } catch (error: any) {
                logger.error('List PRs error', { error: error.message });
                return reply.status(500).send({ error: `Failed to list PRs: ${error.message}` });
            }
        },
    );
}
