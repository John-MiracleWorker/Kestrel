const APPROVE_KEYWORDS = [
    'approve',
    'approved',
    'yes',
    'go ahead',
    'do it',
    'proceed',
    'confirm',
    'i approve',
];

const DENY_KEYWORDS = ['deny', 'denied', 'reject', 'no', 'cancel', 'stop', 'abort'];

function matchesKeyword(text: string, keyword: string): boolean {
    const escaped = keyword.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
    return new RegExp(`\\b${escaped}\\b`).test(text);
}

export function parseTaskRequest(text: string): string | null {
    if (!text.startsWith('!')) {
        return null;
    }
    const goal = text.slice(1).trim();
    return goal || null;
}

export function parseApprovalDecision(text: string): boolean | null {
    const normalized = text.toLowerCase().trim();
    if (!normalized) {
        return null;
    }
    if (APPROVE_KEYWORDS.some((keyword) => matchesKeyword(normalized, keyword))) {
        return true;
    }
    if (DENY_KEYWORDS.some((keyword) => matchesKeyword(normalized, keyword))) {
        return false;
    }
    return null;
}
