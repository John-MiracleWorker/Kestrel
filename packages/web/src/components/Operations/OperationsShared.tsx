import type { CSSProperties } from 'react';

import type { OperatorTaskItem, TaskTimelineItem } from '../../api/client';

export function panelStyle(extra: CSSProperties = {}): CSSProperties {
    return {
        background: 'rgba(7, 12, 18, 0.92)',
        border: '1px solid rgba(0, 243, 255, 0.14)',
        borderRadius: 'var(--radius-lg)',
        padding: '16px',
        boxShadow: '0 18px 40px rgba(0, 0, 0, 0.28)',
        ...extra,
    };
}

export function statValue(tasks: OperatorTaskItem[], status: string) {
    if (status === 'orphaned') {
        return tasks.filter((task) => task.orphaned).length;
    }
    return tasks.filter((task) => task.summary.status === status).length;
}

export function eventLabel(event: TaskTimelineItem): string {
    return String(event.type || '')
        .replace(/_/g, ' ')
        .toLowerCase();
}

export function parseJsonArray(value: string): any[] {
    if (!value) return [];
    try {
        const parsed = JSON.parse(value);
        return Array.isArray(parsed) ? parsed : [];
    } catch {
        return [];
    }
}

export function compactId(value: string, edge = 8): string {
    if (!value || value.length <= edge * 2 + 1) return value;
    return `${value.slice(0, edge)}...${value.slice(-edge)}`;
}

export function receiptTone(failureClass: string): string {
    if (!failureClass || failureClass === 'none') return 'var(--accent-green)';
    if (failureClass === 'partial_output') return '#ffb36a';
    if (failureClass === 'escalation_required') return '#f59e0b';
    return '#ff8ca5';
}

export function verdictTone(verdict: string): string {
    return verdict.toLowerCase() === 'pass' ? 'var(--accent-green)' : '#ff8ca5';
}
