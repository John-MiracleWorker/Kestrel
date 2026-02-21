/**
 * RichContent — Parses assistant message content for structured blocks
 * and renders them as interactive UI components.
 *
 * Supported blocks:
 *   ```chart:bar {...data}```  → BarChart
 *   ```chart:line {...data}``` → LineChart
 *   ```table {...data}```     → DataTable
 *   Regular code blocks        → CodeBlock with copy button
 *   Plain text/markdown        → Rendered as-is
 */
import React, { useState, useMemo } from 'react';

// ── Block Parser ─────────────────────────────────────────────────────
interface ContentBlock {
    type: 'text' | 'code' | 'chart' | 'table';
    content: string;
    language?: string;
    chartType?: 'bar' | 'line' | 'pie';
    data?: unknown;
}

function parseBlocks(content: string): ContentBlock[] {
    const blocks: ContentBlock[] = [];
    const codeBlockRegex = /```(\w*(?::[\w]+)?)\n([\s\S]*?)```/g;
    let lastIndex = 0;
    let match;

    while ((match = codeBlockRegex.exec(content)) !== null) {
        // Text before this block
        if (match.index > lastIndex) {
            const text = content.slice(lastIndex, match.index).trim();
            if (text) blocks.push({ type: 'text', content: text });
        }

        const lang = match[1];
        const body = match[2].trim();

        if (lang.startsWith('chart:')) {
            const chartType = lang.split(':')[1] as 'bar' | 'line' | 'pie';
            try {
                const data = JSON.parse(body);
                blocks.push({ type: 'chart', content: body, chartType, data });
            } catch {
                blocks.push({ type: 'code', content: body, language: lang });
            }
        } else if (lang === 'table') {
            try {
                const data = JSON.parse(body);
                blocks.push({ type: 'table', content: body, data });
            } catch {
                blocks.push({ type: 'code', content: body, language: lang });
            }
        } else {
            blocks.push({ type: 'code', content: body, language: lang || 'text' });
        }

        lastIndex = match.index + match[0].length;
    }

    // Remaining text
    if (lastIndex < content.length) {
        const text = content.slice(lastIndex).trim();
        if (text) blocks.push({ type: 'text', content: text });
    }

    return blocks.length > 0 ? blocks : [{ type: 'text', content }];
}

// ── Main Component ───────────────────────────────────────────────────
export function RichContent({ content }: { content: string }) {
    const blocks = useMemo(() => parseBlocks(content), [content]);

    return (
        <div style={{ display: 'flex', flexDirection: 'column', gap: '12px' }}>
            {blocks.map((block, i) => {
                switch (block.type) {
                    case 'code':
                        return <CodeBlock key={i} code={block.content} language={block.language || ''} />;
                    case 'chart':
                        return <ChartBlock key={i} type={block.chartType || 'bar'} data={block.data} />;
                    case 'table':
                        return <TableBlock key={i} data={block.data} />;
                    default:
                        return <span key={i} style={{ whiteSpace: 'pre-wrap' }}>{block.content}</span>;
                }
            })}
        </div>
    );
}

// ── CodeBlock ────────────────────────────────────────────────────────
function CodeBlock({ code, language }: { code: string; language: string }) {
    const [copied, setCopied] = useState(false);

    const handleCopy = () => {
        navigator.clipboard.writeText(code);
        setCopied(true);
        setTimeout(() => setCopied(false), 2000);
    };

    return (
        <div
            style={{
                background: 'rgba(0,0,0,0.4)',
                border: '1px solid var(--border-subtle)',
                borderRadius: '6px',
                overflow: 'hidden',
            }}
        >
            <div
                style={{
                    display: 'flex',
                    justifyContent: 'space-between',
                    alignItems: 'center',
                    padding: '6px 12px',
                    background: 'rgba(255,255,255,0.03)',
                    borderBottom: '1px solid var(--border-subtle)',
                }}
            >
                <span
                    style={{
                        fontFamily: 'var(--font-mono)',
                        fontSize: '0.65rem',
                        color: 'var(--accent-cyan)',
                        textTransform: 'uppercase',
                        letterSpacing: '0.1em',
                    }}
                >
                    {language}
                </span>
                <button
                    onClick={handleCopy}
                    style={{
                        background: 'none',
                        border: '1px solid var(--text-dim)',
                        color: copied ? '#10b981' : 'var(--text-dim)',
                        padding: '2px 8px',
                        borderRadius: '4px',
                        cursor: 'pointer',
                        fontFamily: 'var(--font-mono)',
                        fontSize: '0.65rem',
                        transition: 'all 0.2s',
                    }}
                >
                    {copied ? '✓ COPIED' : 'COPY'}
                </button>
            </div>
            <pre
                style={{
                    margin: 0,
                    padding: '12px',
                    fontFamily: 'var(--font-mono)',
                    fontSize: '0.82rem',
                    lineHeight: 1.5,
                    color: 'var(--text-primary)',
                    overflowX: 'auto',
                    whiteSpace: 'pre',
                }}
            >
                {code}
            </pre>
        </div>
    );
}

// ── ChartBlock ───────────────────────────────────────────────────────
interface ChartData {
    labels?: string[];
    values?: number[];
    datasets?: Array<{ label: string; values: number[]; color?: string }>;
    title?: string;
}

function ChartBlock({ type, data }: { type: string; data: unknown }) {
    const d = data as ChartData;
    if (!d?.labels || (!d.values && !d.datasets)) {
        return <span style={{ color: 'var(--text-dim)' }}>[Invalid chart data]</span>;
    }

    const values = d.values || d.datasets?.[0]?.values || [];
    const maxVal = Math.max(...values, 1);
    const colors = ['#00f3ff', '#a855f7', '#f59e0b', '#10b981', '#ef4444', '#6366f1'];

    if (type === 'pie') {
        const total = values.reduce((s, v) => s + v, 0) || 1;
        return (
            <div style={{
                background: 'rgba(0,0,0,0.3)', border: '1px solid var(--border-subtle)',
                borderRadius: '6px', padding: '16px',
            }}>
                {d.title && <div style={{ fontFamily: 'var(--font-mono)', fontSize: '0.7rem', color: 'var(--text-dim)', marginBottom: '10px', letterSpacing: '0.08em' }}>{d.title}</div>}
                <div style={{ display: 'flex', flexDirection: 'column', gap: '6px' }}>
                    {d.labels?.map((label, i) => (
                        <div key={i} style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
                            <div style={{ width: `${(values[i] / total) * 100}%`, minWidth: '20px', height: '20px', background: colors[i % colors.length], borderRadius: '3px' }} />
                            <span style={{ fontFamily: 'var(--font-mono)', fontSize: '0.7rem', color: 'var(--text-primary)', whiteSpace: 'nowrap' }}>
                                {label}: {values[i]} ({((values[i] / total) * 100).toFixed(0)}%)
                            </span>
                        </div>
                    ))}
                </div>
            </div>
        );
    }

    // Bar / Line chart
    return (
        <div style={{
            background: 'rgba(0,0,0,0.3)', border: '1px solid var(--border-subtle)',
            borderRadius: '6px', padding: '16px',
        }}>
            {d.title && <div style={{ fontFamily: 'var(--font-mono)', fontSize: '0.7rem', color: 'var(--text-dim)', marginBottom: '10px', letterSpacing: '0.08em' }}>{d.title}</div>}
            <div style={{ display: 'flex', alignItems: 'flex-end', gap: '4px', height: '120px' }}>
                {d.labels?.map((label, i) => (
                    <div key={i} style={{ flex: 1, display: 'flex', flexDirection: 'column', alignItems: 'center', height: '100%', justifyContent: 'flex-end' }}>
                        <div
                            style={{
                                width: '100%',
                                maxWidth: '40px',
                                height: `${(values[i] / maxVal) * 100}%`,
                                background: type === 'line'
                                    ? `linear-gradient(to top, ${colors[i % colors.length]}44, ${colors[i % colors.length]})`
                                    : colors[i % colors.length],
                                borderRadius: '3px 3px 0 0',
                                transition: 'height 0.5s ease',
                                minHeight: '4px',
                            }}
                        />
                        <span style={{ fontFamily: 'var(--font-mono)', fontSize: '0.55rem', color: 'var(--text-dim)', marginTop: '4px', textAlign: 'center', maxWidth: '50px', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                            {label}
                        </span>
                    </div>
                ))}
            </div>
        </div>
    );
}

// ── TableBlock ───────────────────────────────────────────────────────
interface TableData {
    headers?: string[];
    rows?: string[][];
    title?: string;
}

function TableBlock({ data }: { data: unknown }) {
    const d = data as TableData;
    if (!d?.headers || !d?.rows) {
        return <span style={{ color: 'var(--text-dim)' }}>[Invalid table data]</span>;
    }

    const [sortCol, setSortCol] = useState<number | null>(null);
    const [sortAsc, setSortAsc] = useState(true);

    const sortedRows = useMemo(() => {
        if (sortCol === null) return d.rows!;
        return [...d.rows!].sort((a, b) => {
            const va = a[sortCol] || '';
            const vb = b[sortCol] || '';
            const na = parseFloat(va);
            const nb = parseFloat(vb);
            if (!isNaN(na) && !isNaN(nb)) return sortAsc ? na - nb : nb - na;
            return sortAsc ? va.localeCompare(vb) : vb.localeCompare(va);
        });
    }, [d.rows, sortCol, sortAsc]);

    const handleSort = (col: number) => {
        if (sortCol === col) { setSortAsc(!sortAsc); }
        else { setSortCol(col); setSortAsc(true); }
    };

    return (
        <div style={{
            background: 'rgba(0,0,0,0.3)', border: '1px solid var(--border-subtle)',
            borderRadius: '6px', overflow: 'hidden',
        }}>
            {d.title && <div style={{ padding: '8px 12px', fontFamily: 'var(--font-mono)', fontSize: '0.7rem', color: 'var(--text-dim)', borderBottom: '1px solid var(--border-subtle)', letterSpacing: '0.08em' }}>{d.title}</div>}
            <div style={{ overflowX: 'auto' }}>
                <table style={{ width: '100%', borderCollapse: 'collapse', fontFamily: 'var(--font-mono)', fontSize: '0.75rem' }}>
                    <thead>
                        <tr>
                            {d.headers.map((h, i) => (
                                <th
                                    key={i}
                                    onClick={() => handleSort(i)}
                                    style={{
                                        padding: '8px 12px', textAlign: 'left', color: 'var(--accent-cyan)',
                                        borderBottom: '1px solid var(--border-subtle)', cursor: 'pointer',
                                        userSelect: 'none', whiteSpace: 'nowrap',
                                    }}
                                >
                                    {h} {sortCol === i ? (sortAsc ? '▲' : '▼') : ''}
                                </th>
                            ))}
                        </tr>
                    </thead>
                    <tbody>
                        {sortedRows.map((row, i) => (
                            <tr key={i} style={{ background: i % 2 === 0 ? 'transparent' : 'rgba(255,255,255,0.02)' }}>
                                {row.map((cell, j) => (
                                    <td key={j} style={{ padding: '6px 12px', color: 'var(--text-primary)', borderBottom: '1px solid rgba(255,255,255,0.03)' }}>
                                        {cell}
                                    </td>
                                ))}
                            </tr>
                        ))}
                    </tbody>
                </table>
            </div>
        </div>
    );
}
