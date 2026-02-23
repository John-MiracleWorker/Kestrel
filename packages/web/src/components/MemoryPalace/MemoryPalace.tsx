import { useState, useEffect, useRef, useCallback } from 'react';
import { createPortal } from 'react-dom';
import { request } from '../../api/client';

/* ── Types ─────────────────────────────────────────────────────── */
interface MemoryNode {
    id: string;
    label: string;
    entity_type: 'file' | 'person' | 'decision' | 'concept' | 'error' | 'project' | 'tool' | 'conversation';
    description?: string;
    weight: number;
    mentions: number;
    last_seen?: string;
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
    project: '#06b6d4',
    tool: '#ec4899',
    conversation: '#6366f1',
};

const ENTITY_ICONS: Record<string, string> = {
    file: '📄',
    person: '👤',
    decision: '⚡',
    concept: '💡',
    error: '🔴',
    project: '📦',
    tool: '🔧',
    conversation: '💬',
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
        width: '300px',
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
        flexWrap: 'wrap' as const,
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
    descriptionBox: {
        fontSize: '0.7rem',
        color: '#aaa',
        lineHeight: '1.5',
        padding: '8px 10px',
        background: '#111',
        borderRadius: '6px',
        border: '1px solid #1a1a1a',
        marginBottom: '12px',
    },
    metaBadge: (color: string) => ({
        display: 'inline-block',
        fontSize: '0.6rem',
        color: color,
        background: `${color}15`,
        border: `1px solid ${color}30`,
        borderRadius: '4px',
        padding: '2px 8px',
        fontWeight: 600,
        textTransform: 'uppercase' as const,
        letterSpacing: '0.06em',
    }),
};

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
                    nodesRef.current = [];
                    linksRef.current = [];
                }
                setNodeCount(nodesRef.current.length);
                setLinkCount(linksRef.current.length);
            })
            .catch((err: unknown) => {
                console.error('Memory graph fetch failed:', err);
                nodesRef.current = [];
                linksRef.current = [];
                setNodeCount(0);
                setLinkCount(0);
            });
    }, [isVisible, workspaceId]);

    // Cooling alpha — starts at 1.0, decays toward 0 so the sim settles
    const alphaRef = useRef(1.0);

    // Reheat when data changes
    useEffect(() => {
        alphaRef.current = 1.0;
    }, [nodeCount]);

    // Physics step — mutates nodesRef directly, no setState
    const simulate = useCallback(() => {
        const nodes = nodesRef.current;
        const links = linksRef.current;
        if (!nodes.length) return;

        // Cool down — once alpha is near zero the sim is frozen
        const alpha = alphaRef.current;
        if (alpha < 0.001) return; // fully settled
        alphaRef.current *= 0.995; // slow exponential decay

        const canvas = canvasRef.current;
        const w = canvas ? canvas.getBoundingClientRect().width : 800;
        const h = canvas ? canvas.getBoundingClientRect().height : 600;

        // Build index for O(1) lookup instead of .find() in link loop
        const idx = new Map<string, number>();
        for (let i = 0; i < nodes.length; i++) idx.set(nodes[i].id, i);

        // Repulsion (scaled by alpha so it weakens as sim cools)
        const repStrength = 400 * alpha;
        for (let i = 0; i < nodes.length; i++) {
            for (let j = i + 1; j < nodes.length; j++) {
                const dx = nodes[j].x - nodes[i].x;
                const dy = nodes[j].y - nodes[i].y;
                const distSq = dx * dx + dy * dy;
                const dist = Math.max(Math.sqrt(distSq), 1);
                const force = repStrength / distSq;
                const fx = (dx / dist) * force;
                const fy = (dy / dist) * force;
                nodes[i].vx -= fx;
                nodes[i].vy -= fy;
                nodes[j].vx += fx;
                nodes[j].vy += fy;
            }
        }
        // Attraction (links)
        const springStrength = 0.008 * alpha;
        for (const link of links) {
            const si = idx.get(link.source);
            const ti = idx.get(link.target);
            if (si === undefined || ti === undefined) continue;
            const src = nodes[si], tgt = nodes[ti];
            const dx = tgt.x - src.x;
            const dy = tgt.y - src.y;
            const dist = Math.sqrt(dx * dx + dy * dy);
            if (dist < 1) continue;
            const force = (dist - 120) * springStrength;
            const fx = (dx / dist) * force;
            const fy = (dy / dist) * force;
            src.vx += fx;
            src.vy += fy;
            tgt.vx -= fx;
            tgt.vy -= fy;
        }
        // Centering + damping + velocity clamping + integration
        const cx = w / 2, cy = h / 2;
        const maxV = 8; // velocity cap to prevent explosions
        for (const n of nodes) {
            // Centering gravity
            n.vx += (cx - n.x) * 0.005 * alpha;
            n.vy += (cy - n.y) * 0.005 * alpha;
            // Damping (friction)
            n.vx *= 0.85;
            n.vy *= 0.85;
            // Clamp velocity
            const speed = Math.sqrt(n.vx * n.vx + n.vy * n.vy);
            if (speed > maxV) {
                n.vx = (n.vx / speed) * maxV;
                n.vy = (n.vy / speed) * maxV;
            }
            // Integrate position
            if (!isDragging.current || dragNode.current?.id !== n.id) {
                n.x += n.vx;
                n.y += n.vy;
            }
            n.x = Math.max(20, Math.min(w - 20, n.x));
            n.y = Math.max(20, Math.min(h - 20, n.y));
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
            const isConnected = selected && (link.source === selected.id || link.target === selected.id);

            ctx.beginPath();
            ctx.moveTo(src.x, src.y);
            ctx.lineTo(tgt.x, tgt.y);
            ctx.strokeStyle = isConnected ? '#444' : isHighlighted ? '#2a2a2a' : '#151515';
            ctx.lineWidth = isConnected ? 1.5 : isHighlighted ? 1 : 0.5;
            ctx.stroke();

            // Label on hover or when selected
            if ((hovered && (link.source === hovered || link.target === hovered)) ||
                (selected && (link.source === selected.id || link.target === selected.id))) {
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
            const isConversation = node.entity_type === 'conversation';
            const color = ENTITY_COLORS[node.entity_type] || '#888';
            const radius = isConversation ? 6 : 4 + node.weight * 1.2;

            ctx.save();
            if (isHovered || isSelected) {
                ctx.shadowBlur = 16;
                ctx.shadowColor = color;
            } else if (query && !isFiltered) {
                ctx.globalAlpha = 0.15;
            }

            // Conversations get a diamond shape, entities get circles
            if (isConversation) {
                ctx.beginPath();
                ctx.moveTo(node.x, node.y - radius);
                ctx.lineTo(node.x + radius, node.y);
                ctx.lineTo(node.x, node.y + radius);
                ctx.lineTo(node.x - radius, node.y);
                ctx.closePath();
            } else {
                ctx.beginPath();
                ctx.arc(node.x, node.y, radius, 0, 2 * Math.PI);
            }
            ctx.fillStyle = color;
            ctx.fill();
            ctx.strokeStyle = isSelected ? '#fff' : `${color}66`;
            ctx.lineWidth = isSelected ? 2 : 1;
            ctx.stroke();

            // Always show labels (truncated for smaller nodes)
            const showLabel = isHovered || isSelected || node.weight >= 3 || isConversation;
            if (showLabel) {
                const labelText = node.label.length > 20 ? node.label.slice(0, 18) + '…' : node.label;
                ctx.fillStyle = isFiltered ? '#d0d0d0' : '#444';
                ctx.font = `${isHovered ? 11 : 10}px JetBrains Mono`;
                ctx.textAlign = 'center';
                ctx.fillText(labelText, node.x, node.y + radius + 12);
            }

            // Type icon on hover
            if (isHovered) {
                const icon = ENTITY_ICONS[node.entity_type] || '•';
                ctx.font = '12px sans-serif';
                ctx.fillText(icon, node.x, node.y - radius - 6);
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
                // Reheat simulation slightly so neighbors react
                alphaRef.current = Math.max(alphaRef.current, 0.3);
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
        projects: nodes.filter((n) => n.entity_type === 'project').length,
        tools: nodes.filter((n) => n.entity_type === 'tool').length,
        conversations: nodes.filter((n) => n.entity_type === 'conversation').length,
    };

    const selectedLinks = selectedNode
        ? links.filter((l) => l.source === selectedNode.id || l.target === selectedNode.id)
        : [];

    const formatDate = (d?: string) => {
        if (!d) return '—';
        try {
            return new Date(d).toLocaleDateString('en-US', {
                month: 'short', day: 'numeric', year: '2-digit',
                hour: '2-digit', minute: '2-digit',
            });
        } catch { return '—'; }
    };

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
                        {/* Node header */}
                        <div
                            style={{
                                fontSize: '1rem',
                                color: ENTITY_COLORS[selectedNode.entity_type],
                                marginBottom: '6px',
                                fontWeight: 600,
                            }}
                        >
                            {ENTITY_ICONS[selectedNode.entity_type]} {selectedNode.label}
                        </div>

                        {/* Type badge */}
                        <div style={{ marginBottom: '12px' }}>
                            <span style={S.metaBadge(ENTITY_COLORS[selectedNode.entity_type] || '#888')}>
                                {selectedNode.entity_type}
                            </span>
                        </div>

                        {/* Description */}
                        {selectedNode.description && (
                            <>
                                <div style={S.sectionTitle}>// Description</div>
                                <div style={S.descriptionBox}>
                                    {selectedNode.description}
                                </div>
                            </>
                        )}

                        {/* Stats */}
                        <div style={S.sectionTitle}>// Stats</div>
                        <div style={{ display: 'flex', gap: '20px', marginBottom: '8px' }}>
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
                            <div>
                                <div style={{ fontSize: '0.6rem', color: '#555' }}>Last Seen</div>
                                <div style={{ fontSize: '0.75rem', color: '#e0e0e0' }}>
                                    {formatDate(selectedNode.last_seen)}
                                </div>
                            </div>
                        </div>

                        {/* Connections */}
                        <div style={S.sectionTitle}>// Connections ({selectedLinks.length})</div>
                        {selectedLinks.map((l, i) => {
                            const otherId = l.source === selectedNode.id ? l.target : l.source;
                            const otherNode = nodes.find(n => n.id === otherId);
                            const otherColor = otherNode ? ENTITY_COLORS[otherNode.entity_type] || '#888' : '#888';
                            const otherIcon = otherNode ? ENTITY_ICONS[otherNode.entity_type] || '•' : '•';
                            const otherLabel = otherNode?.label || otherId.slice(0, 12);
                            return (
                                <div
                                    key={i}
                                    style={{
                                        ...S.linkItem,
                                        cursor: 'pointer',
                                        padding: '6px 4px',
                                    }}
                                    onClick={() => {
                                        if (otherNode) setSelectedNode(otherNode);
                                    }}
                                >
                                    <span style={{ color: '#00f3ff', fontSize: '0.65rem' }}>{l.relation}</span>
                                    {' → '}
                                    <span style={{ color: otherColor }}>
                                        {otherIcon} {otherLabel}
                                    </span>
                                </div>
                            );
                        })}
                        {selectedLinks.length === 0 && (
                            <div style={{ fontSize: '0.7rem', color: '#444' }}>No connections</div>
                        )}
                    </div>
                )}
            </div>

            {/* Stats Bar */}
            <div style={S.statsBar}>
                {Object.entries({
                    conversations: 'conversation',
                    files: 'file',
                    people: 'person',
                    projects: 'project',
                    tools: 'tool',
                    decisions: 'decision',
                    concepts: 'concept',
                    errors: 'error',
                }).map(([key, type]) => {
                    const count = stats[key as keyof typeof stats] || 0;
                    if (count === 0) return null;
                    return (
                        <div key={key} style={S.statPill(ENTITY_COLORS[type])}>
                            <span style={S.dot(ENTITY_COLORS[type])} />
                            {key.charAt(0).toUpperCase() + key.slice(1)}: {count}
                        </div>
                    );
                })}
                <div style={{ marginLeft: 'auto' }}>
                    Total: {nodeCount} nodes · {linkCount} edges
                </div>
            </div>
        </div>,
        document.body,
    );
}
