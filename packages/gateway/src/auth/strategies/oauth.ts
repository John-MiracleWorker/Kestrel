import { FastifyInstance, FastifyRequest, FastifyReply } from 'fastify';
import { generateTokenPair, generateSecureToken } from '../../auth/middleware';
import { BrainClient } from '../../brain/client';
import { logger } from '../../utils/logger';
import Redis from 'ioredis';
import crypto from 'crypto';

/**
 * OAuth2 configuration for each provider.
 */
const OAUTH_PROVIDERS: Record<string, {
    clientIdEnv: string;
    clientSecretEnv: string;
    authUrl: string;
    tokenUrl: string;
    userInfoUrl: string;
    scopes: string[];
}> = {
    google: {
        clientIdEnv: 'GOOGLE_OAUTH_CLIENT_ID',
        clientSecretEnv: 'GOOGLE_OAUTH_CLIENT_SECRET',
        authUrl: 'https://accounts.google.com/o/oauth2/v2/auth',
        tokenUrl: 'https://oauth2.googleapis.com/token',
        userInfoUrl: 'https://www.googleapis.com/oauth2/v2/userinfo',
        scopes: ['email', 'profile'],
    },
    github: {
        clientIdEnv: 'GITHUB_OAUTH_CLIENT_ID',
        clientSecretEnv: 'GITHUB_OAUTH_CLIENT_SECRET',
        authUrl: 'https://github.com/login/oauth/authorize',
        tokenUrl: 'https://github.com/login/oauth/access_token',
        userInfoUrl: 'https://api.github.com/user',
        scopes: ['user:email'],
    },
};

interface OAuthDeps {
    brainClient: BrainClient;
    redis: Redis;
}

/**
 * OAuth2 route plugin — Google and GitHub login flows.
 */
export default async function oauthRoutes(app: FastifyInstance, deps: OAuthDeps) {
    const { brainClient, redis } = deps;
    const baseUrl = process.env.GATEWAY_BASE_URL || 'http://localhost:8741';
    const webBaseUrl = process.env.WEB_BASE_URL || 'http://localhost:5173';

    // ── GET /api/auth/oauth/:provider ────────────────────────────────
    // Redirect user to OAuth provider's consent screen
    app.get('/api/auth/oauth/:provider', async (req: FastifyRequest, reply: FastifyReply) => {
        const { provider } = req.params as any;
        const config = OAUTH_PROVIDERS[provider];

        if (!config) {
            return reply.status(400).send({ error: `Unknown OAuth provider: ${provider}` });
        }

        const clientId = process.env[config.clientIdEnv];
        if (!clientId) {
            return reply.status(503).send({ error: `${provider} OAuth not configured` });
        }

        // CSRF state token
        const state = generateSecureToken(24);
        await redis.set(`oauth_state:${state}`, provider, 'EX', 600); // 10 min

        const redirectUri = `${baseUrl}/api/auth/oauth/${provider}/callback`;
        const params = new URLSearchParams({
            client_id: clientId,
            redirect_uri: redirectUri,
            response_type: 'code',
            scope: config.scopes.join(' '),
            state,
        });

        return reply.redirect(`${config.authUrl}?${params.toString()}`);
    });

    // ── GET /api/auth/oauth/:provider/callback ───────────────────────
    // Handle OAuth provider callback
    app.get('/api/auth/oauth/:provider/callback', async (req: FastifyRequest, reply: FastifyReply) => {
        const { provider } = req.params as any;
        const { code, state } = req.query as any;
        const config = OAUTH_PROVIDERS[provider];

        if (!config || !code || !state) {
            return reply.redirect(`${webBaseUrl}/auth/error?reason=invalid_callback`);
        }

        // Validate CSRF state
        const storedProvider = await redis.get(`oauth_state:${state}`);
        if (storedProvider !== provider) {
            return reply.redirect(`${webBaseUrl}/auth/error?reason=invalid_state`);
        }
        await redis.del(`oauth_state:${state}`);

        const clientId = process.env[config.clientIdEnv] || '';
        const clientSecret = process.env[config.clientSecretEnv] || '';
        const redirectUri = `${baseUrl}/api/auth/oauth/${provider}/callback`;

        try {
            // Exchange code for access token
            const tokenResponse = await fetch(config.tokenUrl, {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/x-www-form-urlencoded',
                    Accept: 'application/json',
                },
                body: new URLSearchParams({
                    client_id: clientId,
                    client_secret: clientSecret,
                    code,
                    redirect_uri: redirectUri,
                    grant_type: 'authorization_code',
                }),
            });

            const tokenData = await tokenResponse.json() as any;
            const accessToken = tokenData.access_token;

            if (!accessToken) {
                logger.error('OAuth token exchange failed', { provider, error: tokenData });
                return reply.redirect(`${webBaseUrl}/auth/error?reason=token_exchange_failed`);
            }

            // Fetch user info
            const userResponse = await fetch(config.userInfoUrl, {
                headers: { Authorization: `Bearer ${accessToken}` },
            });
            const userData = await userResponse.json() as any;

            // Extract email (GitHub may need separate email API call)
            let email = userData.email;
            if (!email && provider === 'github') {
                const emailsResponse = await fetch('https://api.github.com/user/emails', {
                    headers: { Authorization: `Bearer ${accessToken}` },
                });
                const emails = await emailsResponse.json() as any[];
                const primary = emails.find((e: any) => e.primary && e.verified);
                email = primary?.email;
            }

            if (!email) {
                return reply.redirect(`${webBaseUrl}/auth/error?reason=no_email`);
            }

            // Create or find user in Brain service
            let user: any;
            try {
                // Try to authenticate (existing user)
                user = await brainClient.authenticateUser(email, `oauth:${provider}`);
            } catch {
                // New user — create with OAuth marker
                const displayName = userData.name || userData.login || email.split('@')[0];
                const oauthPassword = `oauth:${provider}:${crypto.randomBytes(32).toString('hex')}`;
                user = await brainClient.createUser(email, oauthPassword, displayName);
            }

            // Generate Kestrel JWT pair
            const workspaces = await brainClient.listWorkspaces(user.id);
            const tokens = generateTokenPair({
                sub: user.id,
                email,
                workspaces: (workspaces || []).map((w: any) => ({
                    id: w.id,
                    role: w.role || 'member',
                })),
            });

            // Store refresh token
            await redis.set(
                `refresh:${user.id}:${tokens.refreshToken.slice(-16)}`,
                tokens.refreshToken,
                'EX',
                7 * 24 * 60 * 60
            );

            // Redirect to web app with tokens
            const params = new URLSearchParams({
                accessToken: tokens.accessToken,
                refreshToken: tokens.refreshToken,
            });
            return reply.redirect(`${webBaseUrl}/auth/callback?${params.toString()}`);

        } catch (err: any) {
            logger.error('OAuth callback failed', { provider, error: err.message });
            return reply.redirect(`${webBaseUrl}/auth/error?reason=server_error`);
        }
    });

    // ── GET /api/auth/oauth/providers ─────────────────────────────────
    // List available OAuth providers (for frontend)
    app.get('/api/auth/oauth/providers', async () => {
        const available = Object.entries(OAUTH_PROVIDERS)
            .filter(([, config]) => !!process.env[config.clientIdEnv])
            .map(([name]) => name);

        return { providers: available };
    });
}
