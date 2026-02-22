import { useState, useEffect } from 'react';
import { createPortal } from 'react-dom';
import { request } from '../../api/client';

/* ── Types ─────────────────────────────────────────────────────── */
interface SavedRepo {
    owner: string;
    repo: string;
}

interface PRInfo {
    title: string;
    number: number;
    author: string;
    url: string;
    state?: string;
    additions?: number;
    deletions?: number;
    changedFiles?: number;
}

interface ReviewIssue {
    severity: 'critical' | 'warning' | 'suggestion' | 'nit';
    file: string;
    line: string;
    title: string;
    description: string;
    suggestion: string;
}

interface ReviewResult {
    summary: string;
    verdict: 'approve' | 'request_changes' | 'comment';
    score: number;
    issues: ReviewIssue[];
    positives: string[];
    risks: string[];
}

interface PRListItem {
    number: number;
    title: string;
    author: string;
    state: string;
    url: string;
    createdAt: string;
    labels: string[];
}

interface PRReviewProps {
    workspaceId: string;
    isVisible: boolean;
    onClose: () => void;
}

const SEVERITY_COLORS: Record<string, string> = {
    critical: '#ef4444', warning: '#f59e0b', suggestion: '#10b981', nit: '#6b7280',
};

const VERDICT_INFO: Record<string, { color: string; icon: string; label: string }> = {
    approve: { color: '#10b981', icon: '✓', label: 'Approved' },
    request_changes: { color: '#ef4444', icon: '✗', label: 'Changes Requested' },
    comment: { color: '#f59e0b', icon: '💬', label: 'Comments' },
};

const STORAGE_KEY = 'kestrel_pr_repos';

/* ── Styles ────────────────────────────────────────────────────── */
const S = {
    overlay: {
        position: 'fixed' as const, top: 0, left: 0, right: 0, bottom: 0,
        background: 'rgba(0,0,0,0.85)', backdropFilter: 'blur(8px)',
        zIndex: 10000, display: 'flex',
        fontFamily: 'JetBrains Mono, monospace',
    },
    sidebar: {
        width: '220px', background: '#080808', borderRight: '1px solid #222',
        display: 'flex', flexDirection: 'column' as const,
    },
    sidebarHeader: {
        padding: '16px', fontSize: '0.65rem', color: '#555',
        textTransform: 'uppercase' as const, letterSpacing: '0.08em',
        borderBottom: '1px solid #222',
    },
    repoList: {
        flex: 1, overflowY: 'auto' as const, padding: '8px 0',
    },
    repoItem: (active: boolean) => ({
        display: 'flex', alignItems: 'center', justifyContent: 'space-between',
        padding: '8px 16px', cursor: 'pointer',
        background: active ? 'rgba(16,185,129,0.06)' : 'transparent',
        borderLeft: active ? '2px solid #10b981' : '2px solid transparent',
        color: active ? '#e0e0e0' : '#666', fontSize: '0.7rem',
        transition: 'all 0.15s',
    }),
    repoRemoveBtn: {
        background: 'none', border: 'none', color: '#444',
        cursor: 'pointer', fontSize: '0.7rem', padding: '0 4px',
    },
    addRepoForm: {
        padding: '12px 16px', borderTop: '1px solid #222', background: '#0a0a0a',
    },
    addInput: {
        width: '100%', background: '#111', border: '1px solid #333',
        borderRadius: '4px', padding: '6px 8px', color: '#e0e0e0',
        fontSize: '0.65rem', outline: 'none', fontFamily: 'inherit',
        marginBottom: '6px', boxSizing: 'border-box' as const,
    },
    addBtn: {
        width: '100%', background: 'rgba(16,185,129,0.1)',
        border: '1px solid rgba(16,185,129,0.3)', borderRadius: '4px',
        color: '#10b981', fontSize: '0.6rem', padding: '5px 0',
        cursor: 'pointer', fontFamily: 'inherit',
    },
    main: {
        flex: 1, display: 'flex', flexDirection: 'column' as const,
    },
    header: {
        display: 'flex', alignItems: 'center', justifyContent: 'space-between',
        padding: '16px 24px', borderBottom: '1px solid #222', background: '#080808',
    },
    title: {
        fontSize: '0.8rem', fontWeight: 700, color: '#10b981',
        letterSpacing: '0.08em', textTransform: 'uppercase' as const,
    },
    closeBtn: {
        background: 'none', border: 'none', color: '#555',
        fontSize: '1.2rem', cursor: 'pointer', padding: '4px 8px',
    },
    prListBar: {
        display: 'flex', gap: '4px', padding: '10px 24px',
        borderBottom: '1px solid #222', background: '#0a0a0a',
        overflowX: 'auto' as const, fontSize: '0.65rem',
    },
    prPill: (active: boolean) => ({
        padding: '4px 10px', borderRadius: '12px', cursor: 'pointer',
        background: active ? 'rgba(16,185,129,0.15)' : '#111',
        border: `1px solid ${active ? 'rgba(16,185,129,0.3)' : '#333'}`,
        color: active ? '#10b981' : '#666', whiteSpace: 'nowrap' as const,
        fontSize: '0.6rem',
    }),
    body: {
        flex: 1, overflowY: 'auto' as const, padding: '24px',
    },
    section: { marginBottom: '24px' },
    sectionTitle: {
        fontSize: '0.65rem', color: '#555', textTransform: 'uppercase' as const,
        letterSpacing: '0.08em', marginBottom: '10px',
    },
    prMeta: {
        display: 'flex', gap: '24px', padding: '12px 16px',
        background: '#111', borderRadius: '6px', marginBottom: '20px', flexWrap: 'wrap' as const,
    },
    metaStat: { display: 'flex', flexDirection: 'column' as const, gap: '2px' },
    metaLabel: { fontSize: '0.55rem', color: '#555', textTransform: 'uppercase' as const },
    metaValue: { fontSize: '0.8rem', color: '#e0e0e0' },
    verdictBadge: (color: string) => ({
        display: 'inline-flex', alignItems: 'center', gap: '6px',
        padding: '6px 16px', borderRadius: '20px', fontSize: '0.7rem',
        fontWeight: 700, color, border: `1px solid ${color}`,
        background: `${color}15`, marginBottom: '12px',
    }),
    scoreBar: (score: number) => ({
        height: '4px', borderRadius: '2px',
        width: `${score * 10}%`,
        background: score >= 7 ? '#10b981' : score >= 4 ? '#f59e0b' : '#ef4444',
    }),
    issueCard: (severity: string) => ({
        padding: '12px 16px', marginBottom: '8px',
        background: '#111', borderRadius: '4px',
        borderLeft: `3px solid ${SEVERITY_COLORS[severity] || '#555'}`,
    }),
    issueTitle: {
        fontSize: '0.75rem', color: '#e0e0e0', marginBottom: '4px',
        display: 'flex', alignItems: 'center', gap: '8px',
    },
    issueSeverity: (severity: string) => ({
        fontSize: '0.55rem', padding: '1px 6px', borderRadius: '3px',
        background: `${SEVERITY_COLORS[severity]}20`,
        color: SEVERITY_COLORS[severity],
        textTransform: 'uppercase' as const, fontWeight: 600,
    }),
    issueDesc: { fontSize: '0.7rem', color: '#888', lineHeight: 1.45, marginBottom: '6px' },
    issueSuggestion: {
        fontSize: '0.65rem', color: '#a855f7', background: 'rgba(168,85,247,0.08)',
        padding: '8px 10px', borderRadius: '4px', marginTop: '4px',
    },
    emptyState: { padding: '60px 20px', textAlign: 'center' as const, color: '#444' },
    loading: { padding: '60px 20px', textAlign: 'center' as const, color: '#00f3ff', fontSize: '0.8rem' },
    error: {
        padding: '16px', background: 'rgba(239,68,68,0.08)',
        border: '1px solid rgba(239,68,68,0.2)', borderRadius: '4px',
        color: '#f87171', fontSize: '0.7rem', marginBottom: '16px',
    },
    listItem: {
        display: 'flex', alignItems: 'center', gap: '8px',
        fontSize: '0.7rem', color: '#aaa', padding: '4px 0',
    },
};

/* ── Component ────────────────────────────────────────────────── */
export function PRReview({ workspaceId, isVisible, onClose }: PRReviewProps) {
    // Saved repos (persisted in localStorage)
    const [repos, setRepos] = useState<SavedRepo[]>(() => {
        try {
            const stored = localStorage.getItem(STORAGE_KEY);
            return stored ? (JSON.parse(stored) as SavedRepo[]) : [];
        }
        catch { return []; }
    });
    const [activeRepo, setActiveRepo] = useState<SavedRepo | null>(repos[0] || null);
    const [newRepoInput, setNewRepoInput] = useState('');

    // PR list for active repo
    const [prList, setPrList] = useState<PRListItem[]>([]);
    const [loadingPRs, setLoadingPRs] = useState(false);

    // Review state
    const [loading, setLoading] = useState(false);
    const [error, setError] = useState<string | null>(null);
    const [review, setReview] = useState<ReviewResult | null>(null);
    const [prInfo, setPrInfo] = useState<PRInfo | null>(null);
    const [selectedPR, setSelectedPR] = useState<number | null>(null);

    // Persist repos
    useEffect(() => {
        localStorage.setItem(STORAGE_KEY, JSON.stringify(repos));
    }, [repos]);

    // Fetch open PRs when active repo changes
    useEffect(() => {
        if (!activeRepo || !isVisible) return;
        setLoadingPRs(true);
        setPrList([]);
        setReview(null);
        setPrInfo(null);
        setSelectedPR(null);

        request<{ prs: PRListItem[] }>(
            `/workspaces/${workspaceId}/pr/list?owner=${activeRepo.owner}&repo=${activeRepo.repo}&state=open`,
        )
            .then(r => setPrList(r.prs || []))
            .catch(() => setPrList([]))
            .finally(() => setLoadingPRs(false));
    }, [activeRepo, isVisible, workspaceId]);

    const addRepo = () => {
        const match = newRepoInput.trim().match(/^([^/\s]+)\/([^/\s]+)$/);
        if (!match) return;
        const r: SavedRepo = { owner: match[1], repo: match[2] };
        if (repos.some(x => x.owner === r.owner && x.repo === r.repo)) return;
        const updated = [...repos, r];
        setRepos(updated);
        setActiveRepo(r);
        setNewRepoInput('');
    };

    const removeRepo = (r: SavedRepo) => {
        const updated = repos.filter(x => !(x.owner === r.owner && x.repo === r.repo));
        setRepos(updated);
        if (activeRepo?.owner === r.owner && activeRepo?.repo === r.repo) {
            setActiveRepo(updated[0] || null);
        }
    };

    const reviewPR = async (prNumber: number) => {
        if (!activeRepo) return;
        setSelectedPR(prNumber);
        setLoading(true);
        setError(null);
        setReview(null);
        setPrInfo(null);

        try {
            const result = await request<{ review: ReviewResult; pr: PRInfo }>(
                `/workspaces/${workspaceId}/pr/review`,
                { method: 'POST', body: { owner: activeRepo.owner, repo: activeRepo.repo, prNumber } },
            );
            setReview(result.review);
            setPrInfo(result.pr);
        } catch (err: unknown) {
            setError(err instanceof Error ? err.message : 'PR review failed');
        } finally {
            setLoading(false);
        }
    };

    if (!isVisible) return null;

    const verdictInfo = review ? VERDICT_INFO[review.verdict] || VERDICT_INFO.comment : null;

    return createPortal(
        <div style={S.overlay}>
            {/* Repo Sidebar */}
            <div style={S.sidebar}>
                <div style={S.sidebarHeader}>Connected Repos</div>
                <div style={S.repoList}>
                    {repos.length === 0 && (
                        <div style={{ padding: '16px', fontSize: '0.65rem', color: '#444', textAlign: 'center' }}>
                            No repos connected yet.
                            Add one below.
                        </div>
                    )}
                    {repos.map(r => (
                        <div key={`${r.owner}/${r.repo}`}
                            style={S.repoItem(activeRepo?.owner === r.owner && activeRepo?.repo === r.repo)}
                            onClick={() => setActiveRepo(r)}>
                            <span>{r.owner}/{r.repo}</span>
                            <button style={S.repoRemoveBtn} onClick={e => { e.stopPropagation(); removeRepo(r); }}>✕</button>
                        </div>
                    ))}
                </div>
                <div style={S.addRepoForm}>
                    <input
                        style={S.addInput}
                        placeholder="owner/repo"
                        value={newRepoInput}
                        onChange={e => setNewRepoInput(e.target.value)}
                        onKeyDown={e => e.key === 'Enter' && addRepo()}
                    />
                    <button style={S.addBtn} onClick={addRepo}>+ Add Repo</button>
                </div>
            </div>

            {/* Main Panel */}
            <div style={S.main}>
                <div style={S.header}>
                    <div style={S.title}>
                        🔍 PR Review
                        {activeRepo && <span style={{ color: '#555', fontWeight: 400 }}> · {activeRepo.owner}/{activeRepo.repo}</span>}
                    </div>
                    <button style={S.closeBtn} onClick={onClose}>✕</button>
                </div>

                {/* PR pills */}
                {activeRepo && (
                    <div style={S.prListBar}>
                        {loadingPRs ? (
                            <span style={{ color: '#555' }}>Loading PRs...</span>
                        ) : prList.length === 0 ? (
                            <span style={{ color: '#444' }}>No open PRs</span>
                        ) : prList.map(pr => (
                            <div key={pr.number} style={S.prPill(selectedPR === pr.number)}
                                onClick={() => void reviewPR(pr.number)}>
                                #{pr.number} {pr.title.slice(0, 30)}{pr.title.length > 30 ? '…' : ''}
                            </div>
                        ))}
                    </div>
                )}

                {/* Body */}
                <div style={S.body}>
                    {loading && (
                        <div style={S.loading}>
                            <div style={{ fontSize: '1.5rem', marginBottom: '12px' }}>🔄</div>
                            Analyzing PR diff with AI...
                        </div>
                    )}

                    {error && <div style={S.error}>✗ {error}</div>}

                    {!activeRepo && (
                        <div style={S.emptyState}>
                            <div style={{ fontSize: '2rem', marginBottom: '12px' }}>🔗</div>
                            <div style={{ fontSize: '0.8rem', color: '#666', marginBottom: '8px' }}>
                                Connect a Repository
                            </div>
                            <div style={{ fontSize: '0.7rem', color: '#444', maxWidth: '400px', margin: '0 auto' }}>
                                Add a GitHub repo in the sidebar (e.g. <span style={{ color: '#10b981' }}>owner/repo</span>)
                                to browse and review pull requests with AI.
                            </div>
                        </div>
                    )}

                    {activeRepo && !loading && !review && !error && (
                        <div style={S.emptyState}>
                            <div style={{ fontSize: '2rem', marginBottom: '12px' }}>🔍</div>
                            <div style={{ fontSize: '0.7rem', color: '#444' }}>
                                Select a PR above to analyze it
                            </div>
                        </div>
                    )}

                    {review && prInfo && (
                        <>
                            <div style={S.prMeta}>
                                <div style={S.metaStat}>
                                    <span style={S.metaLabel}>PR</span>
                                    <span style={S.metaValue}>
                                        <a href={prInfo.url} target="_blank" rel="noopener noreferrer"
                                            style={{ color: '#00f3ff', textDecoration: 'none' }}>
                                            #{prInfo.number}
                                        </a>
                                    </span>
                                </div>
                                <div style={S.metaStat}>
                                    <span style={S.metaLabel}>Title</span>
                                    <span style={{ ...S.metaValue, fontSize: '0.7rem' }}>{prInfo.title}</span>
                                </div>
                                <div style={S.metaStat}>
                                    <span style={S.metaLabel}>Author</span>
                                    <span style={S.metaValue}>{prInfo.author}</span>
                                </div>
                                {prInfo.additions !== undefined && (
                                    <div style={S.metaStat}>
                                        <span style={S.metaLabel}>Changes</span>
                                        <span style={S.metaValue}>
                                            <span style={{ color: '#10b981' }}>+{prInfo.additions}</span>
                                            {' / '}
                                            <span style={{ color: '#ef4444' }}>-{prInfo.deletions}</span>
                                        </span>
                                    </div>
                                )}
                                {prInfo.changedFiles !== undefined && (
                                    <div style={S.metaStat}>
                                        <span style={S.metaLabel}>Files</span>
                                        <span style={S.metaValue}>{prInfo.changedFiles}</span>
                                    </div>
                                )}
                            </div>

                            <div style={S.section}>
                                {verdictInfo && (
                                    <div style={S.verdictBadge(verdictInfo.color)}>
                                        {verdictInfo.icon} {verdictInfo.label} — Score: {review.score}/10
                                    </div>
                                )}
                                <div style={{ width: '100%', height: '4px', background: '#1a1a1a', borderRadius: '2px', overflow: 'hidden', marginBottom: '16px' }}>
                                    <div style={S.scoreBar(review.score)} />
                                </div>
                                <p style={{ fontSize: '0.75rem', color: '#ccc', lineHeight: 1.5 }}>
                                    {review.summary}
                                </p>
                            </div>

                            {review.issues.length > 0 && (
                                <div style={S.section}>
                                    <div style={S.sectionTitle}>Issues ({review.issues.length})</div>
                                    {review.issues.map((issue, i) => (
                                        <div key={i} style={S.issueCard(issue.severity)}>
                                            <div style={S.issueTitle}>
                                                <span style={S.issueSeverity(issue.severity)}>{issue.severity}</span>
                                                <span>{issue.title}</span>
                                            </div>
                                            {issue.file && (
                                                <div style={{ fontSize: '0.6rem', color: '#555', marginBottom: '4px' }}>
                                                    📍 {issue.file}{issue.line ? `:${issue.line}` : ''}
                                                </div>
                                            )}
                                            <div style={S.issueDesc}>{issue.description}</div>
                                            {issue.suggestion && (
                                                <div style={S.issueSuggestion}>💡 {issue.suggestion}</div>
                                            )}
                                        </div>
                                    ))}
                                </div>
                            )}

                            {review.positives?.length > 0 && (
                                <div style={S.section}>
                                    <div style={S.sectionTitle}>✓ Positives</div>
                                    {review.positives.map((p, i) => (
                                        <div key={i} style={S.listItem}>
                                            <span style={{ color: '#10b981' }}>✓</span> {p}
                                        </div>
                                    ))}
                                </div>
                            )}

                            {review.risks?.length > 0 && (
                                <div style={S.section}>
                                    <div style={S.sectionTitle}>⚠ Risks</div>
                                    {review.risks.map((r, i) => (
                                        <div key={i} style={S.listItem}>
                                            <span style={{ color: '#f59e0b' }}>⚠</span> {r}
                                        </div>
                                    ))}
                                </div>
                            )}
                        </>
                    )}
                </div>
            </div>
        </div>,
        document.body,
    );
}
