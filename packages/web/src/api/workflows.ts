import { request } from './http';
import type { WorkflowItem } from './types';

export const workflows = {
    list: (category?: string) =>
        request<{ workflows: WorkflowItem[] }>(
            `/workflows${category ? `?category=${category}` : ''}`,
        ).then((res) => res.workflows),
};
