import { getRefreshToken, request } from './http';

export const auth = {
    register: (email: string, password: string, displayName?: string) =>
        request<{ accessToken: string; refreshToken: string; user: unknown }>('/auth/register', {
            method: 'POST',
            body: { email, password, displayName },
        }),

    login: (email: string, password: string) =>
        request<{
            accessToken: string;
            refreshToken: string;
            user: unknown;
            workspaces: unknown[];
        }>('/auth/login', {
            method: 'POST',
            body: { email, password },
        }),

    logout: () =>
        request('/auth/logout', { method: 'POST', body: { refreshToken: getRefreshToken() } }),

    me: () => request<{ id: string; email: string; displayName: string }>('/auth/me'),

    oauthProviders: () => request<{ providers: string[] }>('/auth/oauth/providers'),
};
