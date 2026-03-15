import { isTauri } from '@tauri-apps/api/core';

import type { UploadedFile } from './types';

const GATEWAY_ORIGIN = 'http://localhost:8741';
export const BASE_URL = isTauri() ? `${GATEWAY_ORIGIN}/api` : '/api';
const REFRESH_STORAGE_KEY = 'kestrel_refresh';

type RequestOptions = {
    method?: string;
    body?: unknown;
    headers?: Record<string, string>;
};

let accessToken: string | null = null;
let refreshToken: string | null = null;
let onAuthExpired: (() => void) | null = null;
let refreshPromise: Promise<boolean> | null = null;

function getTokenStorage(): Storage | null {
    if (typeof window === 'undefined') {
        return null;
    }
    return window.sessionStorage;
}

export function setTokens(access: string, refresh: string) {
    accessToken = access;
    refreshToken = refresh;
    localStorage.removeItem('kestrel_access');
    localStorage.removeItem(REFRESH_STORAGE_KEY);
    getTokenStorage()?.setItem(REFRESH_STORAGE_KEY, refresh);
}

export function loadTokens(): boolean {
    refreshToken = getTokenStorage()?.getItem(REFRESH_STORAGE_KEY) ?? null;
    if (!refreshToken) {
        const legacyRefresh = localStorage.getItem(REFRESH_STORAGE_KEY);
        if (legacyRefresh) {
            refreshToken = legacyRefresh;
            getTokenStorage()?.setItem(REFRESH_STORAGE_KEY, legacyRefresh);
            localStorage.removeItem(REFRESH_STORAGE_KEY);
        }
    }
    return !!refreshToken;
}

export function clearTokens() {
    accessToken = null;
    refreshToken = null;
    localStorage.removeItem('kestrel_access');
    localStorage.removeItem(REFRESH_STORAGE_KEY);
    getTokenStorage()?.removeItem(REFRESH_STORAGE_KEY);
}

export function getAccessToken(): string | null {
    return accessToken;
}

export function getRefreshToken(): string | null {
    return refreshToken;
}

export function hasRefreshToken(): boolean {
    return !!refreshToken || !!getTokenStorage()?.getItem(REFRESH_STORAGE_KEY);
}

export function setOnAuthExpired(callback: () => void) {
    onAuthExpired = callback;
}

async function tryRefresh(): Promise<boolean> {
    if (!refreshToken) return false;
    if (refreshPromise) return refreshPromise;

    refreshPromise = (async () => {
        try {
            const response = await fetch(`${BASE_URL}/auth/refresh`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ refreshToken }),
            });

            if (!response.ok) {
                return false;
            }

            const data = (await response.json()) as {
                accessToken: string;
                refreshToken: string;
            };
            setTokens(data.accessToken, data.refreshToken);
            return true;
        } catch {
            return false;
        } finally {
            refreshPromise = null;
        }
    })();

    return refreshPromise;
}

export async function forceRefresh(): Promise<boolean> {
    return tryRefresh();
}

export async function request<T = unknown>(url: string, options: RequestOptions = {}): Promise<T> {
    const headers: Record<string, string> = {
        'Content-Type': 'application/json',
        ...(accessToken ? { Authorization: `Bearer ${accessToken}` } : {}),
        ...options.headers,
    };

    let response = await fetch(`${BASE_URL}${url}`, {
        method: options.method || 'GET',
        headers,
        body: options.body ? JSON.stringify(options.body) : undefined,
    });

    if (response.status === 401) {
        if (refreshToken) {
            const refreshed = await tryRefresh();
            if (refreshed) {
                headers.Authorization = `Bearer ${accessToken}`;
                response = await fetch(`${BASE_URL}${url}`, {
                    method: options.method || 'GET',
                    headers,
                    body: options.body ? JSON.stringify(options.body) : undefined,
                });
            } else {
                onAuthExpired?.();
                throw new Error('Session expired');
            }
        } else {
            onAuthExpired?.();
            throw new Error('Session expired');
        }
    }

    if (!response.ok) {
        const error = (await response.json().catch(() => ({ error: response.statusText }))) as {
            error?: string;
        };
        throw new Error(error.error || response.statusText);
    }

    return response.json() as Promise<T>;
}

export async function uploadFiles(files: File[]): Promise<UploadedFile[]> {
    const formData = new FormData();
    files.forEach((file) => formData.append('file', file));

    const headers: Record<string, string> = {};
    if (accessToken) {
        headers.Authorization = `Bearer ${accessToken}`;
    }

    let response = await fetch(`${BASE_URL}/upload`, {
        method: 'POST',
        headers,
        body: formData,
    });

    if (response.status === 401 && refreshToken) {
        const refreshed = await tryRefresh();
        if (refreshed) {
            headers.Authorization = `Bearer ${accessToken}`;
            response = await fetch(`${BASE_URL}/upload`, {
                method: 'POST',
                headers,
                body: formData,
            });
        }
    }

    if (!response.ok) {
        const error = (await response.json().catch(() => ({ error: response.statusText }))) as {
            error?: string;
        };
        throw new Error(error.error || 'Upload failed');
    }

    const data = (await response.json()) as { files: UploadedFile[] };
    return data.files;
}

export function createChatSocket(): WebSocket {
    const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    const wsUrl = isTauri() ? 'ws://localhost:8741/ws' : `${protocol}//${window.location.host}/ws`;
    const socket = new WebSocket(wsUrl);
    socket.addEventListener('open', () => {
        socket.send(JSON.stringify({ type: 'auth', token: accessToken }));
    });
    return socket;
}
