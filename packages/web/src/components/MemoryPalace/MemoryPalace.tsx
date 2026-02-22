import { useState, useEffect, useRef, useCallback } from 'react';
import { createPortal } from 'react-dom';
import { request } from '../../api/client';

/* ── Types ─────────────────────────────────────────────────────── */
interface MemoryNode {
    id: string;
    label: string;
    entity_type: 'file' | 'person' | 'decision' | 'concept' | 'error';
    weight: number;
    mentions: number;
    x: number;
    y: number;
    vx: number;
    vy: number;
}

interface MemoryLink {
    source: string;
    target: string;
    relation: string;
}

interface MemoryPalaceProps {
    workspaceId: string;
    isVisible: boolean;
    onClose: () => void;
}

const ENTITY_COLORS: Record<string, string> = {
    file: '#3b82f6',
    person: '#10b981',
    decision: '#f59e0b',
    concept: '#a855f7',
    error: '#ef4444',
};

const ENTITY_ICONS: Record<string, string> = {
    file: '📄',
    person: '👤',
    decision: '⚡',
    concept: '💡',
    error: '🔴',
};

/* ── Styles ────────────────────────────────────────────────────── */
const S = {
    overlay: {
        position: 'fixed' as const,
        top: 0,
        left: 0,
        right: 0,
        bottom: 0,
        background: 'rgba(0,0,0,0.85)',
        backdropFilter: 'blur(8px)',
        zIndex: 10000,
        display: 'flex',
        flexDirection: 'column' as const,
        fontFamily: 'JetBrains Mono, monospace',
    },
    header: {
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'space-between',
        padding: '16px 24px',
        borderBottom: '1px solid #222',
        background: '#080808',
    },
    title: {
        fontSize: '0.8rem',
        fontWeight: 700,
        color: '#a855f7',
        letterSpacing: '0.08em',
        textTransform: 'uppercase' as const,
    },
    searchInput: {
        background: '#111',
        border: '1px solid #333',
        borderRadius: '4px',
        padding: '6px 12px',
        color: '#e0e0e0',
        fontSize: '0.7rem',
        outline: 'none',
        width: '260px',
        fontFamily: 'inherit',
    },
    closeBtn: {
        background: 'none',
        border: 'none',
        color: '#555',
        fontSize: '1.2rem',
        cursor: 'pointer',
        padding: '4px 8px',
    },
    body: {
        flex: 1,
        display: 'flex',
        position: 'relative' as const,
        overflow: 'hidden',
    },
    canvas: {
        flex: 1,
        background: '#0a0a0a',
    },
    detailPanel: {
        width: '280px',
        background: '#0a0a0a',
        borderLeft: '1px solid #222',
        padding: '16px',
        overflowY: 'auto' as const,
        transition: 'transform 0.2s',
    },
    sectionTitle: {
        fontSize: '0.65rem',
        color: '#555',
        textTransform: 'uppercase' as const,
        letterSpacing: '0.08em',
        marginBottom: '8px',
        marginTop: '16px',
    },
    statsBar: {
        display: 'flex',
        gap: '16px',
        padding: '10px 24px',
        background: '#080808',
        borderTop: '1px solid #222',
        fontSize: '0.6rem',
        color: '#555',
        fontFamily: 'inherit',
    },
    statPill: (_color: string) => ({
        display: 'inline-flex',
        alignItems: 'center',
        gap: '4px',
    }),
    dot: (color: string) => ({
        width: '6px',
        height: '6px',
        borderRadius: '50%',
        background: color,
        display: 'inline-block',
    }),
    nodeDetail: {
        fontSize: '0.75rem',
        color: '#e0e0e0',
        marginBottom: '6px',
    },
    linkItem: {
        fontSize: '0.7rem',
        color: '#888',
        padding: '4px 0',
        borderBottom: '1px solid #1a1a1a',
    },
};

/* ── Mock Data Generator ──────────────────────────────────────── */
function generateMockData(): { nodes: MemoryNode[]; links: MemoryLink[] } {
    const nodeTemplates: Omit<MemoryNode, 'x' | 'y' | 'vx' | 'vy'>[] = [
        { id: 'server.py', label: 'server.py', entity_type: 'file', weight: 5, mentions: 28 },
        { id: 'client.ts', label: 'client.ts', entity_type: 'file', weight: 4, mentions: 15 },
        { id: 'auth.ts', label: 'auth.ts', entity_type: 'file', weight: 3, mentions: 12 },
        {
            id: 'cron_parser.py',
            label: 'cron_parser.py',
            entity_type: 'file',
            weight: 2,
            mentions: 5,
        },
        {
            id: 'SettingsPanel',
            label: 'SettingsPanel.tsx',
            entity_type: 'file',
            weight: 4,
            mentions: 18,
        },
        { id: 'ChatView', label: 'ChatView.tsx', entity_type: 'file', weight: 3, mentions: 10 },
        { id: 'user', label: 'User', entity_type: 'person', weight: 4, mentions: 22 },
        { id: 'kestrel', label: 'Kestrel', entity_type: 'person', weight: 3, mentions: 14 },
        { id: 'jwt-auth', label: 'Use JWT Auth', entity_type: 'decision', weight: 3, mentions: 8 },
        {
            id: 'grpc-arch',
            label: 'gRPC Architecture',
            entity_type: 'decision',
            weight: 4,
            mentions: 11,
        },
        {
            id: 'microservices',
            label: 'Microservices',
            entity_type: 'concept',
            weight: 5,
            mentions: 20,
        },
        { id: 'rag', label: 'RAG Pipeline', entity_type: 'concept', weight: 3, mentions: 9 },
        {
            id: 'memory-graph',
            label: 'Memory Graph',
            entity_type: 'concept',
            weight: 3,
            mentions: 7,
        },
        { id: 'agent-loop', label: 'Agent Loop', entity_type: 'concept', weight: 4, mentions: 13 },
        { id: 'sandbox', label: 'Sandboxing', entity_type: 'concept', weight: 2, mentions: 6 },
        { id: 'websocket', label: 'WebSocket', entity_type: 'concept', weight: 3, mentions: 10 },
        { id: 'provider-err', label: 'Provider 401', entity_type: 'error', weight: 2, mentions: 4 },
    ];

    const cx = 400,
        cy = 300;
    const nodes: MemoryNode[] = nodeTemplates.map((n, i) => {
        const angle = (2 * Math.PI * i) / nodeTemplates.length;
        const radius = 120 + Math.random() * 100;
        return {
            ...n,
            x: cx + Math.cos(angle) * radius,
            y: cy + Math.sin(angle) * radius,
            vx: 0,
            vy: 0,
        };
    });

    const links: MemoryLink[] = [
        { source: 'server.py', target: 'client.ts', relation: 'communicates_with' },
        { source: 'server.py', target: 'cron_parser.py', relation: 'imports' },
        { source: 'server.py', target: 'grpc-arch', relation: 'implements' },
        { source: 'client.ts', target: 'auth.ts', relation: 'depends_on' },
        { source: 'client.ts', target: 'jwt-auth', relation: 'implements' },
        { source: 'SettingsPanel', target: 'client.ts', relation: 'uses' },
        { source: 'ChatView', target: 'client.ts', relation: 'uses' },
        { source: 'user', target: 'server.py', relation: 'modified' },
        { source: 'user', target: 'jwt-auth', relation: 'decided' },
        { source: 'kestrel', target: 'agent-loop', relation: 'executes' },
        { source: 'kestrel', target: 'rag', relation: 'uses' },
        { source: 'microservices', target: 'grpc-arch', relation: 'related_to' },
        { source: 'rag', target: 'memory-graph', relation: 'feeds' },
        { source: 'agent-loop', target: 'sandbox', relation: 'uses' },
        { source: 'websocket', target: 'ChatView', relation: 'powers' },
        { source: 'provider-err', target: 'server.py', relation: 'occurred_in' },
        { source: 'provider-err', target: 'auth.ts', relation: 'related_to' },
    ];

    return { nodes, links };
}

/* ── Component ────────────────────────────────────────────────── */
export function MemoryPalace({ workspaceId, isVisible, onClose }: MemoryPalaceProps) {
    const canvasRef = useRef<HTMLCanvasElement>(null);
    const animRef = useRef<number>(0);

    // Mutable refs for physics — avoids React re-renders on every frame
    const nodesRef = useRef<MemoryNode[]>([]);
    const linksRef = useRef<MemoryLink[]>([]);

    // React state only for things that affect the DOM (detail panel, stats)
    const [nodeCount, setNodeCount] = useState(0);
    const [linkCount, setLinkCount] = useState(0);
    const [selectedNode, setSelectedNode] = useState<MemoryNode | null>(null);
    const [searchQuery, setSearchQuery] = useState('');
    const [hoveredNode, setHoveredNode] = useState<string | null>(null);
    const hoveredRef = useRef<string | null>(null);
    const searchRef = useRef('');
    const selectedRef = useRef<MemoryNode | null>(null);
    const isDragging = useRef(false);
    const dragNode = useRef<MemoryNode | null>(null);

    // Keep refs in sync with state
    useEffect(() => {
        hoveredRef.current = hoveredNode;
    }, [hoveredNode]);
    useEffect(() => {
        searchRef.current = searchQuery;
    }, [searchQuery]);
    useEffect(() => {
        selectedRef.current = selectedNode;
    }, [selectedNode]);

    // Load memory graph data from the real API
    useEffect(() => {
        if (!isVisible) return;
        request(`/workspaces/${workspaceId}/memory/graph`)
            .then((raw: unknown) => {
                const data = raw as { nodes?: MemoryNode[]; links?: MemoryLink[] };
                if (data?.nodes?.length) {
                    nodesRef.current = data.nodes;
                    linksRef.current = data.links || [];
                } else {
                    // Empty graph — only show mock data in development
                    if (import.meta.env.DEV) {
                        const mock = generateMockData();
                        nodesRef.current = mock.nodes;
                        linksRef.current = mock.links;
                    } else {
                        nodesRef.current = [];
                        linksRef.current = [];
                    }
                }
                setNodeCount(nodesRef.current.length);
                setLinkCount(linksRef.current.length);
            })
            .catch((err: unknown) => {
                console.error('Memory graph fetch failed:', err);
                // On network failure, show empty graph — don't silently show fake data
                nodesRef.current = [];
                linksRef.current = [];
                setNodeCount(0);
                setLinkCount(0);
            });
    }, [isVisible, workspaceId]);

    // Physics step — mutates nodesRef directly, no setState
    const simulate = useCallback(() => {
        const nodes = nodesRef.current;
        const links = linksRef.current;
        if (!nodes.length) return;

        // Repulsion
        for (let i = 0; i < nodes.length; i++) {
            for (let j = i + 1; j < nodes.length; j++) {
                const dx = nodes[j].x - nodes[i].x;
                const dy = nodes[j].y - nodes[i].y;
                const dist = Math.max(Math.sqrt(dx * dx + dy * dy), 1);
                const force = 800 / (dist * dist);
                nodes[i].vx -= (dx / dist) * force;
                nodes[i].vy -= (dy / dist) * force;
                nodes[j].vx += (dx / dist) * force;
                nodes[j].vy += (dy / dist) * force;
            }
        }
        // Attraction (links)
        for (const link of links) {
            const src = nodes.find((n) => n.id === link.source);
            const tgt = nodes.find((n) => n.id === link.target);
            if (!src || !tgt) continue;
            const dx = tgt.x - src.x;
            const dy = tgt.y - src.y;
            const dist = Math.sqrt(dx * dx + dy * dy);
            if (dist < 1) continue;
            const force = (dist - 100) * 0.01;
            src.vx += (dx / dist) * force;
            src.vy += (dy / dist) * force;
            tgt.vx -= (dx / dist) * force;
            tgt.vy -= (dy / dist) * force;
        }
        // Centering + damping + integration
        const cx = 400,
            cy = 300;
        for (const n of nodes) {
            n.vx += (cx - n.x) * 0.001;
            n.vy += (cy - n.y) * 0.001;
            n.vx *= 0.9;
            n.vy *= 0.9;
            if (!isDragging.current || dragNode.current?.id !== n.id) {
                n.x += n.vx;
                n.y += n.vy;
            }
            n.x = Math.max(20, Math.min(780, n.x));
            n.y = Math.max(20, Math.min(580, n.y));
        }
    }, []);

    // Render canvas — reads from refs, no React deps
    const draw = useCallback(() => {
        const canvas = canvasRef.current;
        if (!canvas) return;
        const ctx = canvas.getContext('2d');
        if (!ctx) return;

        const nodes = nodesRef.current;
        const links = linksRef.current;
        const hovered = hoveredRef.current;
        const query = searchRef.current;
        const selected = selectedRef.current;

        const rect = canvas.getBoundingClientRect();
        canvas.width = rect.width * window.devicePixelRatio;
        canvas.height = rect.height * window.devicePixelRatio;
        ctx.scale(window.devicePixelRatio, window.devicePixelRatio);

        ctx.clearRect(0, 0, rect.width, rect.height);

        const filtered = query
            ? nodes.filter((n) => n.label.toLowerCase().includes(query.toLowerCase()))
            : nodes;
        const filteredIds = new Set(filtered.map((n) => n.id));

        // Draw links
        for (const link of links) {
            const src = nodes.find((n) => n.id === link.source);
            const tgt = nodes.find((n) => n.id === link.target);
            if (!src || !tgt) continue;
            const isHighlighted = filteredIds.has(src.id) && filteredIds.has(tgt.id);
            ctx.beginPath();
            ctx.moveTo(src.x, src.y);
            ctx.lineTo(tgt.x, tgt.y);
            ctx.strokeStyle = isHighlighted ? '#333' : '#1a1a1a';
            ctx.lineWidth = isHighlighted ? 1 : 0.5;
            ctx.stroke();

            // Label on hover
            if (hovered && (link.source === hovered || link.target === hovered)) {
                const mx = (src.x + tgt.x) / 2;
                const my = (src.y + tgt.y) / 2;
                ctx.fillStyle = '#555';
                ctx.font = '9px JetBrains Mono';
                ctx.textAlign = 'center';
                ctx.fillText(link.relation, mx, my - 4);
            }
        }

        // Draw nodes
        for (const node of nodes) {
            const isFiltered = filteredIds.has(node.id);
            const isHovered = hovered === node.id;
            const isSelected = selected?.id === node.id;
            const color = ENTITY_COLORS[node.entity_type] || '#888';
            const radius = 4 + node.weight * 1.5;

            ctx.save();
            if (isHovered || isSelected) {
                ctx.shadowBlur = 16;
                ctx.shadowColor = color;
            } else if (query && !isFiltered) {
                ctx.globalAlpha = 0.15;
            }

            ctx.beginPath();
            ctx.arc(node.x, node.y, radius, 0, 2 * Math.PI);
            ctx.fillStyle = color;
            ctx.fill();
            ctx.strokeStyle = isSelected ? '#fff' : `${color}66`;
            ctx.lineWidth = isSelected ? 2 : 1;
            ctx.stroke();

            // Label
            if (isHovered || isSelected || node.weight >= 4) {
                ctx.fillStyle = isFiltered ? '#e0e0e0' : '#555';
                ctx.font = `${isHovered ? 11 : 10}px JetBrains Mono`;
                ctx.textAlign = 'center';
                ctx.fillText(node.label, node.x, node.y + radius + 12);
            }

            ctx.restore();
        }
    }, []);

    // Animation loop — runs in rAF, no React state touched
    useEffect(() => {
        if (!isVisible || !nodeCount) return;
        let running = true;
        const loop = () => {
            if (!running) return;
            simulate();
            draw();
            animRef.current = requestAnimationFrame(loop);
        };
        animRef.current = requestAnimationFrame(loop);
        return () => {
            running = false;
            cancelAnimationFrame(animRef.current);
        };
    }, [isVisible, nodeCount, simulate, draw]);

    // Mouse interaction
    const handleMouseMove = (e: React.MouseEvent) => {
        const canvas = canvasRef.current;
        if (!canvas) return;
        const rect = canvas.getBoundingClientRect();
        const mx = e.clientX - rect.left;
        const my = e.clientY - rect.top;

        if (isDragging.current && dragNode.current) {
            dragNode.current.x = mx;
            dragNode.current.y = my;
            return;
        }

        let found: string | null = null;
        for (const node of nodesRef.current) {
            const r = 4 + node.weight * 1.5;
            if (Math.hypot(mx - node.x, my - node.y) < r + 4) {
                found = node.id;
                break;
            }
        }
        setHoveredNode(found);
    };

    const handleMouseDown = (_e: React.MouseEvent) => {
        if (hoveredNode) {
            const node = nodesRef.current.find((n) => n.id === hoveredNode);
            if (node) {
                isDragging.current = true;
                dragNode.current = node;
            }
        }
    };

    const handleMouseUp = () => {
        isDragging.current = false;
        dragNode.current = null;
    };

    const handleClick = () => {
        if (hoveredNode) {
            setSelectedNode(nodesRef.current.find((n) => n.id === hoveredNode) || null);
        } else {
            setSelectedNode(null);
        }
    };

    if (!isVisible) return null;

    const nodes = nodesRef.current;
    const links = linksRef.current;

    const stats = {
        files: nodes.filter((n) => n.entity_type === 'file').length,
        people: nodes.filter((n) => n.entity_type === 'person').length,
        decisions: nodes.filter((n) => n.entity_type === 'decision').length,
        concepts: nodes.filter((n) => n.entity_type === 'concept').length,
        errors: nodes.filter((n) => n.entity_type === 'error').length,
        totalEdges: links.length,
    };

    const selectedLinks = selectedNode
        ? links.filter((l) => l.source === selectedNode.id || l.target === selectedNode.id)
        : [];

    return createPortal(
        <div style={S.overlay}>
            {/* Header */}
            <div style={S.header}>
                <div style={S.title}>🧠 Memory Palace</div>
                <input
                    style={S.searchInput}
                    placeholder="Search memory..."
                    value={searchQuery}
                    onChange={(e) => setSearchQuery(e.target.value)}
                    onFocus={(e) => {
                        (e.target as HTMLInputElement).style.borderColor = '#a855f7';
                    }}
                    onBlur={(e) => {
                        (e.target as HTMLInputElement).style.borderColor = '#333';
                    }}
                />
                <button style={S.closeBtn} onClick={onClose}>
                    ✕
                </button>
            </div>

            {/* Body */}
            <div style={S.body}>
                <canvas
                    ref={canvasRef}
                    style={S.canvas}
                    onMouseMove={handleMouseMove}
                    onMouseDown={handleMouseDown}
                    onMouseUp={handleMouseUp}
                    onClick={handleClick}
                />

                {selectedNode && (
                    <div style={S.detailPanel}>
                        <div
                            style={{
                                fontSize: '0.9rem',
                                color: ENTITY_COLORS[selectedNode.entity_type],
                                marginBottom: '4px',
                            }}
                        >
                            {ENTITY_ICONS[selectedNode.entity_type]} {selectedNode.label}
                        </div>
                        <div
                            style={{
                                fontSize: '0.65rem',
                                color: '#555',
                                textTransform: 'uppercase',
                                marginBottom: '12px',
                            }}
                        >
                            {selectedNode.entity_type}
                        </div>

                        <div style={{ display: 'flex', gap: '16px', marginBottom: '16px' }}>
                            <div>
                                <div style={{ fontSize: '0.6rem', color: '#555' }}>Weight</div>
                                <div style={{ fontSize: '0.85rem', color: '#e0e0e0' }}>
                                    {selectedNode.weight.toFixed(1)}
                                </div>
                            </div>
                            <div>
                                <div style={{ fontSize: '0.6rem', color: '#555' }}>Mentions</div>
                                <div style={{ fontSize: '0.85rem', color: '#e0e0e0' }}>
                                    {selectedNode.mentions}
                                </div>
                            </div>
                        </div>

                        <div style={S.sectionTitle}>// Connections</div>
                        {selectedLinks.map((l, i) => (
                            <div key={i} style={S.linkItem}>
                                <span style={{ color: '#00f3ff' }}>{l.relation}</span>
                                {' → '}
                                <span style={{ color: '#e0e0e0' }}>
                                    {l.source === selectedNode.id ? l.target : l.source}
                                </span>
                            </div>
                        ))}
                        {selectedLinks.length === 0 && (
                            <div style={{ fontSize: '0.7rem', color: '#444' }}>No connections</div>
                        )}
                    </div>
                )}
            </div>

            {/* Stats Bar */}
            <div style={S.statsBar}>
                {Object.entries({
                    files: 'file',
                    people: 'person',
                    decisions: 'decision',
                    concepts: 'concept',
                    errors: 'error',
                }).map(([key, type]) => (
                    <div key={key} style={S.statPill(ENTITY_COLORS[type])}>
                        <span style={S.dot(ENTITY_COLORS[type])} />
                        {key.charAt(0).toUpperCase() + key.slice(1)}:{' '}
                        {stats[key as keyof typeof stats]}
                    </div>
                ))}
                <div style={{ marginLeft: 'auto' }}>
                    Total: {nodeCount} nodes · {linkCount} edges
                </div>
            </div>
        </div>,
        document.body,
    );
}
