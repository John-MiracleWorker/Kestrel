/**
 * UIArtifactRenderer — Renders persistent interactive components
 * created by the agent (React, HTML, Markdown) with live preview,
 * code view, version history, and copy-to-clipboard.
 */
import React, { useState, useMemo, useCallback } from 'react';
import './UIArtifactRenderer.css';

// ── Types ────────────────────────────────────────────────────────────

export interface UIArtifact {
    id: string;
    title: string;
    description: string;
    component_type: 'react' | 'html' | 'markdown';
    component_code: string;
    props_schema: Record<string, unknown>;
    data_source: string;
    version: number;
    created_at: string;
    updated_at: string;
}

interface UIArtifactRendererProps {
    artifact: UIArtifact;
    onRequestUpdate?: (artifactId: string, instruction: string) => void;
    compact?: boolean;
}

// ── Component Type Config ────────────────────────────────────────────

const TYPE_CONFIG: Record<string, { icon: string; label: string; badgeClass: string }> = {
    react: { icon: '⚛️', label: 'React', badgeClass: 'ui-artifact-panel__type-badge--react' },
    html: { icon: '🌐', label: 'HTML', badgeClass: 'ui-artifact-panel__type-badge--html' },
    markdown: {
        icon: '📝',
        label: 'Markdown',
        badgeClass: 'ui-artifact-panel__type-badge--markdown',
    },
};

// ── Single Artifact Renderer ─────────────────────────────────────────

export function UIArtifactRenderer({
    artifact,
    onRequestUpdate,
    compact,
}: UIArtifactRendererProps) {
    const [viewMode, setViewMode] = useState<'preview' | 'code'>('preview');
    const [showToast, setShowToast] = useState(false);

    const typeConfig = TYPE_CONFIG[artifact.component_type] || TYPE_CONFIG.html;

    // Build sandboxed HTML for iframe preview
    const previewHtml = useMemo(() => {
        if (artifact.component_type === 'html') {
            return artifact.component_code;
        }
        if (artifact.component_type === 'markdown') {
            // Simple markdown-to-html for preview (bold, italic, headers, code, lists)
            const md = artifact.component_code
                .replace(/^### (.+)$/gm, '<h3>$1</h3>')
                .replace(/^## (.+)$/gm, '<h2>$1</h2>')
                .replace(/^# (.+)$/gm, '<h1>$1</h1>')
                .replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>')
                .replace(/\*(.+?)\*/g, '<em>$1</em>')
                .replace(
                    /`(.+?)`/g,
                    '<code style="background:rgba(0,0,0,0.1);padding:2px 6px;border-radius:3px">$1</code>',
                )
                .replace(/^- (.+)$/gm, '<li>$1</li>')
                .replace(/\n/g, '<br/>');
            return `<div style="font-family:Inter,-apple-system,sans-serif;padding:20px;line-height:1.7;color:#1e293b">${md}</div>`;
        }
        // React component — wrap in a minimal runtime
        return `
<!DOCTYPE html>
<html>
<head>
    <style>
        body { margin: 0; padding: 16px; font-family: Inter, -apple-system, sans-serif; color: #1e293b; }
        * { box-sizing: border-box; }
    </style>
    <script src="https://unpkg.com/react@18/umd/react.production.min.js" crossorigin></script>
    <script src="https://unpkg.com/react-dom@18/umd/react-dom.production.min.js" crossorigin></script>
    <script src="https://unpkg.com/@babel/standalone/babel.min.js"></script>
</head>
<body>
    <div id="root"></div>
    <script type="text/babel">
        ${artifact.component_code}
        const root = ReactDOM.createRoot(document.getElementById('root'));
        root.render(React.createElement(typeof App !== 'undefined' ? App : () => React.createElement('div', null, 'No App component exported')));
    </script>
</body>
</html>`;
    }, [artifact.component_code, artifact.component_type]);

    const handleCopy = useCallback(() => {
        void navigator.clipboard.writeText(artifact.component_code).then(() => {
            setShowToast(true);
            setTimeout(() => setShowToast(false), 2000);
        });
    }, [artifact.component_code]);

    const codeLines = useMemo(() => artifact.component_code.split('\n'), [artifact.component_code]);

    const formattedDate = useMemo(() => {
        try {
            return new Date(artifact.updated_at).toLocaleDateString('en-US', {
                month: 'short',
                day: 'numeric',
                hour: '2-digit',
                minute: '2-digit',
            });
        } catch {
            return artifact.updated_at;
        }
    }, [artifact.updated_at]);

    return (
        <div className="ui-artifact-panel">
            {/* Header */}
            <div className="ui-artifact-panel__header">
                <div className="ui-artifact-panel__title-group">
                    <span className="ui-artifact-panel__icon">{typeConfig.icon}</span>
                    <span className="ui-artifact-panel__title">{artifact.title}</span>
                    <span className={`ui-artifact-panel__type-badge ${typeConfig.badgeClass}`}>
                        {typeConfig.label}
                    </span>
                </div>
                <div className="ui-artifact-panel__actions">
                    <button
                        className={`ui-artifact-panel__action-btn ${viewMode === 'preview' ? 'ui-artifact-panel__action-btn--active' : ''}`}
                        onClick={() => setViewMode('preview')}
                        title="Preview"
                    >
                        👁️ Preview
                    </button>
                    <button
                        className={`ui-artifact-panel__action-btn ${viewMode === 'code' ? 'ui-artifact-panel__action-btn--active' : ''}`}
                        onClick={() => setViewMode('code')}
                        title="View source code"
                    >
                        {'</>'} Code
                    </button>
                    <button
                        className="ui-artifact-panel__action-btn"
                        onClick={handleCopy}
                        title="Copy source code"
                    >
                        📋
                    </button>
                    {onRequestUpdate && (
                        <button
                            className="ui-artifact-panel__action-btn"
                            onClick={() => {
                                const instruction = prompt('How should this artifact be updated?');
                                if (instruction) onRequestUpdate(artifact.id, instruction);
                            }}
                            title="Request update"
                        >
                            ✏️ Edit
                        </button>
                    )}
                </div>
            </div>

            {/* Content */}
            <div className="ui-artifact-panel__content">
                {showToast && <div className="ui-artifact-panel__toast">Copied!</div>}

                {viewMode === 'preview' ? (
                    <iframe
                        className="ui-artifact-panel__preview"
                        srcDoc={previewHtml}
                        sandbox="allow-scripts"
                        title={artifact.title}
                        style={{ height: compact ? '200px' : '360px' }}
                    />
                ) : (
                    <div className="ui-artifact-panel__code">
                        {codeLines.map((line, i) => (
                            <div key={i}>
                                <span className="line-number">{i + 1}</span>
                                {line}
                            </div>
                        ))}
                    </div>
                )}
            </div>

            {/* Footer */}
            <div className="ui-artifact-panel__meta">
                <span className="ui-artifact-panel__description">
                    {artifact.description || 'Agent-generated component'}
                </span>
                <span className="ui-artifact-panel__version">
                    v{artifact.version} · {formattedDate}
                </span>
            </div>
        </div>
    );
}

// ── Artifacts List ───────────────────────────────────────────────────

interface UIArtifactsListProps {
    workspaceId: string;
    artifacts?: UIArtifact[];
    onRequestUpdate?: (artifactId: string, instruction: string) => void;
    loading?: boolean;
}

export function UIArtifactsList({ artifacts, onRequestUpdate, loading }: UIArtifactsListProps) {
    if (loading) {
        return (
            <div className="ui-artifact-panel__empty">
                <div className="ui-artifact-panel__empty-icon">⏳</div>
                <div className="ui-artifact-panel__empty-text">Loading artifacts…</div>
            </div>
        );
    }

    if (!artifacts || artifacts.length === 0) {
        return (
            <div className="ui-artifact-panel__empty">
                <div className="ui-artifact-panel__empty-icon">🧩</div>
                <div className="ui-artifact-panel__empty-text">
                    No UI artifacts yet. Ask Kestrel to create a dashboard, chart, or any
                    interactive component.
                </div>
            </div>
        );
    }

    return (
        <div className="ui-artifacts-list">
            <div className="ui-artifacts-list__header">
                <div className="ui-artifacts-list__title">
                    🧩 UI Artifacts
                    <span className="ui-artifacts-list__count">{artifacts.length}</span>
                </div>
            </div>
            {artifacts.map((artifact) => (
                <UIArtifactRenderer
                    key={artifact.id}
                    artifact={artifact}
                    onRequestUpdate={onRequestUpdate}
                    compact
                />
            ))}
        </div>
    );
}

// ── Inline Artifact (for embedding in chat messages) ─────────────────

interface InlineArtifactProps {
    artifact: UIArtifact;
}

export function InlineArtifact({ artifact }: InlineArtifactProps) {
    const [expanded, setExpanded] = useState(false);
    const typeConfig = TYPE_CONFIG[artifact.component_type] || TYPE_CONFIG.html;

    return (
        <div style={{ margin: '12px 0' }}>
            <button
                onClick={() => setExpanded(!expanded)}
                style={{
                    display: 'flex',
                    alignItems: 'center',
                    gap: '8px',
                    background: 'rgba(59, 130, 246, 0.08)',
                    border: '1px solid rgba(59, 130, 246, 0.25)',
                    borderRadius: '8px',
                    padding: '8px 14px',
                    color: '#93c5fd',
                    cursor: 'pointer',
                    fontSize: '13px',
                    fontWeight: 500,
                    width: '100%',
                    textAlign: 'left',
                    transition: 'all 0.2s',
                }}
            >
                <span>{typeConfig.icon}</span>
                <span style={{ flex: 1 }}>{artifact.title}</span>
                <span
                    style={{
                        fontSize: '10px',
                        padding: '2px 6px',
                        borderRadius: '4px',
                        background: 'rgba(100,116,139,0.2)',
                        color: '#94a3b8',
                    }}
                >
                    v{artifact.version}
                </span>
                <span style={{ fontSize: '11px', opacity: 0.7 }}>{expanded ? '▲' : '▼'}</span>
            </button>
            {expanded && (
                <div style={{ marginTop: '8px' }}>
                    <UIArtifactRenderer artifact={artifact} compact />
                </div>
            )}
        </div>
    );
}

export default UIArtifactRenderer;
