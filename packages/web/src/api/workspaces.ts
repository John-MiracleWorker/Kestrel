import { request } from './http';
import type { Workspace } from './types';

export const workspaces = {
    list: () => request<{ workspaces: Workspace[] }>('/workspaces'),
    create: (name: string) => request<Workspace>('/workspaces', { method: 'POST', body: { name } }),
    get: (id: string) => request<Workspace>(`/workspaces/${id}`),
    update: (id: string, data: Partial<Workspace>) =>
        request<Workspace>(`/workspaces/${id}`, { method: 'PUT', body: data }),
    delete: (id: string) => request(`/workspaces/${id}`, { method: 'DELETE' }),
};
