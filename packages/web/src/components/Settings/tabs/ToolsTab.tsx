import { useState, useEffect } from 'react';
import { S, CATEGORY_ICONS } from '../constants';
import { RiskBadge, Toggle } from '../Shared';
import { tools as toolsApi, request, ToolInfo } from '../../../api/client';
import { McpServer } from '../types';

interface ToolsTabProps {
    workspaceId: string;
    disabledTools: Set<string>;
    setDisabledTools: React.Dispatch<React.SetStateAction<Set<string>>>;
    setSaveStatus: (status: string | null) => void;
    setError: (error: string | null) => void;
}

export function ToolsTab({
    workspaceId,
    disabledTools,
    setDisabledTools,
    setSaveStatus,
    setError
}: ToolsTabProps) {
    const [toolsList, setToolsList] = useState<ToolInfo[]>([]);
    const [toolsLoading, setToolsLoading] = useState(false);
    const [showCreateTool, setShowCreateTool] = useState(false);
    const [newToolName, setNewToolName] = useState('');
    const [newToolDesc, setNewToolDesc] = useState('');
    const [newToolCode, setNewToolCode] = useState('');

    const [mcpServers, setMcpServers] = useState<McpServer[]>([]);
    const [mcpLoading, setMcpLoading] = useState(false);
    const [showAddMcp, setShowAddMcp] = useState(false);
    const [mcpName, setMcpName] = useState('');
    const [mcpUrl, setMcpUrl] = useState('');
    const [mcpDesc, setMcpDesc] = useState('');
    const [mcpTransport, setMcpTransport] = useState<'stdio' | 'http' | 'sse'>('stdio');
    const [mcpSaving, setMcpSaving] = useState(false);
    const [mcpSearchQuery, setMcpSearchQuery] = useState('');
    const [mcpSearchResults, setMcpSearchResults] = useState<Array<{ name: string; description: string; transport: string; source: string; requires_env?: string[] }>>([]);
    const [mcpSearching, setMcpSearching] = useState(false);
    const [mcpConfiguring, setMcpConfiguring] = useState<string | null>(null);
    const [mcpEnvValues, setMcpEnvValues] = useState<Record<string, string>>({});

    useEffect(() => {
        setToolsLoading(true);
        toolsApi.list(workspaceId)
            .then(data => setToolsList(data?.tools || []))
            .catch(() => setToolsList([]))
            .finally(() => setToolsLoading(false));

        setMcpLoading(true);
        request(`/workspaces/${workspaceId}/mcp-tools`)
            .then((d: any) => setMcpServers(d?.tools || []))
            .catch(() => setMcpServers([]))
            .finally(() => setMcpLoading(false));
    }, [workspaceId]);

    const toggleTool = (toolName: string) => {
        setDisabledTools(prev => {
            const next = new Set(prev);
            if (next.has(toolName)) next.delete(toolName);
            else next.add(toolName);
            return next;
        });
    };

    return (
        <div>
            <div style={S.sectionTitle}>// TOOL REGISTRY</div>
            <p style={{ fontSize: '0.75rem', color: '#666', marginBottom: '12px', lineHeight: 1.5 }}>
                Manage the tools Kestrel can use. Disable tools to restrict capabilities.
                Kestrel can also create new tools on the fly (with your approval).
            </p>

            <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '16px' }}>
                <span style={{ fontSize: '0.7rem', color: '#444' }}>
                    {toolsList.length} tools registered · {disabledTools.size} disabled
                </span>
                <button style={{ ...S.btnGhost, padding: '6px 14px', fontSize: '0.7rem' }}
                    onClick={() => setShowCreateTool(!showCreateTool)}>
                    {showCreateTool ? '✕ Cancel' : '+ Create Tool'}
                </button>
            </div>

            {showCreateTool && (
                <div style={{
                    padding: '16px', marginBottom: '16px', background: '#0d0d0d',
                    border: '1px solid #1a1a1a', borderRadius: '4px',
                }}>
                    <div style={{ ...S.sectionTitle, marginBottom: '12px' }}>// NEW TOOL</div>
                    <div style={S.field}>
                        <label style={S.label}>Tool Name</label>
                        <input style={S.input} value={newToolName}
                            onChange={e => setNewToolName(e.target.value)}
                            placeholder="e.g. calculate_average"
                            onFocus={e => { e.target.style.borderColor = '#00f3ff'; }}
                            onBlur={e => { e.target.style.borderColor = '#333'; }} />
                    </div>
                    <div style={S.field}>
                        <label style={S.label}>Description</label>
                        <input style={S.input} value={newToolDesc}
                            onChange={e => setNewToolDesc(e.target.value)}
                            placeholder="What does this tool do?"
                            onFocus={e => { e.target.style.borderColor = '#00f3ff'; }}
                            onBlur={e => { e.target.style.borderColor = '#333'; }} />
                    </div>
                    <div style={S.field}>
                        <label style={S.label}>Python Code</label>
                        <textarea style={{ ...S.textarea, minHeight: 120, fontSize: '0.75rem' }}
                            value={newToolCode}
                            onChange={e => setNewToolCode(e.target.value)}
                            placeholder={`def run(args):\n    # args is a dict of parameters\n    result = args['numbers']\n    return sum(result) / len(result)`}
                            onFocus={e => { e.target.style.borderColor = '#00f3ff'; }}
                            onBlur={e => { e.target.style.borderColor = '#333'; }} />
                    </div>
                    <div style={{ display: 'flex', gap: '8px' }}>
                        <button style={S.btnPrimary} onClick={async () => {
                            if (!newToolName.trim()) return;
                            try {
                                const goalParts = [`Create a new Python skill/tool named "${newToolName}"`];
                                if (newToolDesc.trim()) goalParts.push(`that ${newToolDesc}`);
                                if (newToolCode.trim()) goalParts.push(`using this implementation:\n\n\`\`\`python\n${newToolCode}\n\`\`\``);
                                await request(`/workspaces/${workspaceId}/tasks`, {
                                    method: 'POST',
                                    body: { goal: goalParts.join(' ') },
                                });
                                setShowCreateTool(false);
                                setNewToolName(''); setNewToolDesc(''); setNewToolCode('');
                                setSaveStatus('Tool creation task dispatched — check Tasks panel');
                                setTimeout(() => setSaveStatus(null), 4000);
                            } catch (err: any) {
                                setError(err.message || 'Failed to dispatch tool creation');
                                setTimeout(() => setError(null), 3000);
                            }
                        }}>Create Tool</button>
                        <div style={{ fontSize: '0.65rem', color: '#666', display: 'flex', alignItems: 'center' }}>
                            Requires approval before use
                        </div>
                    </div>
                </div>
            )}

            {toolsLoading ? (
                <div style={{ textAlign: 'center', color: '#444', padding: '24px', fontSize: '0.8rem' }}>
                    Loading tools...
                </div>
            ) : (
                <div>
                    {toolsList.map(tool => {
                        const isDisabled = disabledTools.has(tool.name);
                        const isSystem = ['task_complete', 'ask_human'].includes(tool.name);
                        return (
                            <div key={tool.name} style={{
                                ...S.toolCard,
                                opacity: isDisabled ? 0.4 : 1,
                                transition: 'opacity 0.2s',
                            }}>
                                <span style={{ fontSize: '1rem', width: '24px', textAlign: 'center', color: '#555' }}>
                                    {CATEGORY_ICONS[tool.category] || '○'}
                                </span>
                                <div style={{ flex: 1 }}>
                                    <div style={{ display: 'flex', alignItems: 'center', gap: '8px', marginBottom: '4px' }}>
                                        <span style={{ fontSize: '0.8rem', fontWeight: 500, color: '#e0e0e0' }}>{tool.name}</span>
                                        <RiskBadge level={tool.riskLevel} />
                                        <span style={{
                                            fontSize: '0.6rem', padding: '1px 6px', borderRadius: '3px',
                                            background: '#1a1a1a', color: '#555',
                                        }}>{tool.category}</span>
                                    </div>
                                    <div style={{ fontSize: '0.7rem', color: '#666' }}>{tool.description}</div>
                                </div>
                                {!isSystem && (
                                    <Toggle value={!isDisabled} onChange={() => toggleTool(tool.name)} />
                                )}
                            </div>
                        );
                    })}
                </div>
            )}

            {/* ── MCP Servers ─────────────────────────────── */}
            <div style={{ marginTop: '32px', borderTop: '1px solid #1a1a1a', paddingTop: '24px' }}>
                <div style={S.sectionTitle}>// MCP SERVERS</div>
                <p style={{ fontSize: '0.75rem', color: '#666', marginBottom: '12px', lineHeight: 1.5 }}>
                    Connect external MCP (Model Context Protocol) servers to give Kestrel new capabilities.
                    Servers can provide file access, database queries, API integrations, and more.
                </p>

                {/* Search Bar */}
                <div style={{ marginBottom: '16px' }}>
                    <div style={{ display: 'flex', gap: '8px', marginBottom: '8px' }}>
                        <input
                            style={{ ...S.input, flex: 1, borderColor: mcpSearching ? '#a855f7' : '#333' }}
                            value={mcpSearchQuery}
                            onChange={e => setMcpSearchQuery(e.target.value)}
                            placeholder="Search MCP servers...  e.g. github, database, slack"
                            onKeyDown={async e => {
                                if (e.key === 'Enter' && mcpSearchQuery.trim()) {
                                    setMcpSearching(true);
                                    try {
                                        const d = (await request(`/mcp/search?q=${encodeURIComponent(mcpSearchQuery)}`)) as {
                                            results?: Array<{ name: string; description: string; transport: string; source: string }>;
                                        };
                                        setMcpSearchResults(d?.results || []);
                                    } catch { setMcpSearchResults([]); }
                                    finally { setMcpSearching(false); }
                                }
                            }}
                            onFocus={e => { e.target.style.borderColor = '#a855f7'; }}
                            onBlur={e => { if (!mcpSearching) e.target.style.borderColor = '#333'; }}
                        />
                        <button
                            style={{ ...S.btnPrimary, background: '#a855f7', padding: '8px 16px', fontSize: '0.75rem', opacity: mcpSearching ? 0.5 : 1 }}
                            disabled={mcpSearching || !mcpSearchQuery.trim()}
                            onClick={async () => {
                                if (!mcpSearchQuery.trim()) return;
                                setMcpSearching(true);
                                try {
                                    const d = (await request(`/mcp/search?q=${encodeURIComponent(mcpSearchQuery)}`)) as {
                                        results?: Array<{ name: string; description: string; transport: string; source: string }>;
                                    };
                                    setMcpSearchResults(d?.results || []);
                                } catch { setMcpSearchResults([]); }
                                finally { setMcpSearching(false); }
                            }}
                        >
                            {mcpSearching ? '...' : '🔍'}
                        </button>
                    </div>

                    {/* Marketplace Link */}
                    <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
                        <span style={{ fontSize: '0.65rem', color: '#555' }}>Searches official MCP catalog + Smithery registry</span>
                        <a
                            href="https://smithery.ai"
                            target="_blank"
                            rel="noopener noreferrer"
                            style={{
                                fontSize: '0.68rem', color: '#a855f7', textDecoration: 'none',
                                fontFamily: "'JetBrains Mono', monospace",
                                borderBottom: '1px dotted #a855f7',
                            }}
                        >
                            Browse Marketplace →
                        </a>
                    </div>

                    {/* Search Results */}
                    {mcpSearchResults.length > 0 && (
                        <div style={{ marginTop: '12px', maxHeight: '280px', overflowY: 'auto' }}>
                            {mcpSearchResults.map(r => {
                                const isInstalled = mcpServers.some(s => s.name === r.name);
                                const needsConfig = r.requires_env && r.requires_env.length > 0;
                                const isConfiguring = mcpConfiguring === r.name;

                                const doInstall = async (envVars?: Record<string, string>) => {
                                    try {
                                        await request(`/workspaces/${workspaceId}/mcp-tools`, {
                                            method: 'POST',
                                            body: {
                                                name: r.name,
                                                description: r.description,
                                                serverUrl: `npx -y ${r.name}`,
                                                transport: r.transport || 'stdio',
                                                config: envVars ? { env: envVars } : {},
                                            },
                                        });
                                        setMcpServers(prev => [...prev, {
                                            name: r.name, description: r.description,
                                            server_url: `npx -y ${r.name}`,
                                            transport: r.transport || 'stdio', enabled: true,
                                        }]);
                                        setMcpConfiguring(null);
                                        setMcpEnvValues({});
                                        setSaveStatus(`Installed ${r.name}`);
                                        setTimeout(() => setSaveStatus(null), 2000);
                                    } catch {
                                        setError('Failed to install');
                                        setTimeout(() => setError(null), 3000);
                                    }
                                };

                                return (
                                    <div key={r.name} style={{
                                        padding: '10px 12px', background: '#0d0d0d',
                                        border: isConfiguring ? '1px solid #a855f7' : '1px solid #1a1a1a',
                                        borderRadius: '4px', marginBottom: '6px',
                                        opacity: isInstalled ? 0.5 : 1,
                                        transition: 'border-color 0.2s',
                                    }}>
                                        <div style={{ display: 'flex', alignItems: 'center', gap: '10px' }}>
                                            <span style={{ fontSize: '0.85rem', color: r.source === 'official' ? '#00f3ff' : '#a855f7', flexShrink: 0 }}>
                                                {r.source === 'official' ? '★' : '◆'}
                                            </span>
                                            <div style={{ flex: 1, minWidth: 0 }}>
                                                <div style={{ fontSize: '0.75rem', fontWeight: 500, color: '#e0e0e0', marginBottom: '2px', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                                                    {r.name}
                                                    {needsConfig && <span style={{ color: '#f59e0b', fontSize: '0.6rem', marginLeft: '6px' }}>🔑 requires config</span>}
                                                </div>
                                                <div style={{ fontSize: '0.65rem', color: '#666', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                                                    {r.description}
                                                </div>
                                            </div>
                                            <button
                                                style={{
                                                    ...S.btnGhost, padding: '4px 12px', fontSize: '0.65rem',
                                                    color: isInstalled ? '#555' : isConfiguring ? '#f59e0b' : '#10b981',
                                                    borderColor: isInstalled ? '#333' : isConfiguring ? '#f59e0b' : '#10b981',
                                                }}
                                                disabled={isInstalled}
                                                onClick={() => {
                                                    if (needsConfig && !isConfiguring) {
                                                        setMcpConfiguring(r.name);
                                                        setMcpEnvValues({});
                                                    } else if (!needsConfig) {
                                                        doInstall();
                                                    }
                                                }}
                                            >
                                                {isInstalled ? 'Installed' : isConfiguring ? '▾ Configure' : needsConfig ? '🔑 Configure' : '+ Install'}
                                            </button>
                                        </div>

                                        {/* Env var configuration form */}
                                        {isConfiguring && r.requires_env && (
                                            <div style={{
                                                marginTop: '10px', padding: '10px 12px',
                                                background: '#111', borderRadius: '4px',
                                                border: '1px solid #a855f733',
                                            }}>
                                                <div style={{ fontSize: '0.7rem', color: '#a855f7', fontWeight: 600, marginBottom: '8px' }}>
                                                    🔑 Required Configuration
                                                </div>
                                                {r.requires_env.map(envKey => (
                                                    <div key={envKey} style={{ marginBottom: '8px' }}>
                                                        <label style={{ fontSize: '0.65rem', color: '#888', display: 'block', marginBottom: '3px' }}>
                                                            {envKey}
                                                        </label>
                                                        <input
                                                            style={{ ...S.input, fontSize: '0.75rem' }}
                                                            type={envKey.toLowerCase().includes('token') || envKey.toLowerCase().includes('key') || envKey.toLowerCase().includes('secret') ? 'password' : 'text'}
                                                            placeholder={`Enter ${envKey}...`}
                                                            value={mcpEnvValues[envKey] || ''}
                                                            onChange={e => setMcpEnvValues(prev => ({ ...prev, [envKey]: e.target.value }))}
                                                        />
                                                    </div>
                                                ))}
                                                <div style={{ display: 'flex', gap: '8px', marginTop: '10px' }}>
                                                    <button
                                                        style={{
                                                            ...S.btnPrimary, padding: '6px 16px', fontSize: '0.7rem',
                                                            background: '#a855f7',
                                                            opacity: r.requires_env.every(k => mcpEnvValues[k]?.trim()) ? 1 : 0.4,
                                                        }}
                                                        disabled={!r.requires_env.every(k => mcpEnvValues[k]?.trim())}
                                                        onClick={() => doInstall(mcpEnvValues)}
                                                    >
                                                        ✓ Install with Config
                                                    </button>
                                                    <button
                                                        style={{ ...S.btnGhost, padding: '6px 12px', fontSize: '0.7rem' }}
                                                        onClick={() => { setMcpConfiguring(null); setMcpEnvValues({}); }}
                                                    >
                                                        Cancel
                                                    </button>
                                                </div>
                                            </div>
                                        )}
                                    </div>
                                );
                            })}
                        </div>
                    )}
                </div>

                <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '16px' }}>
                    <span style={{ fontSize: '0.7rem', color: '#444' }}>
                        {mcpServers.length} servers connected
                    </span>
                    <button style={{ ...S.btnGhost, padding: '6px 14px', fontSize: '0.7rem' }}
                        onClick={() => {
                            setShowAddMcp(!showAddMcp);
                            if (!mcpLoading && mcpServers.length === 0) {
                                setMcpLoading(true);
                                request(`/workspaces/${workspaceId}/mcp-tools`)
                                    .then((d: any) => setMcpServers(d?.tools || []))
                                    .catch(() => setMcpServers([]))
                                    .finally(() => setMcpLoading(false));
                            }
                        }}>
                        {showAddMcp ? '✕ Cancel' : '+ Add MCP Server'}
                    </button>
                </div>

                {showAddMcp && (
                    <div style={{
                        padding: '16px', marginBottom: '16px', background: '#0d0d0d',
                        border: '1px solid #1a1a1a', borderRadius: '4px',
                    }}>
                        <div style={{ ...S.sectionTitle, marginBottom: '12px' }}>// ADD MCP SERVER</div>
                        <div style={S.field}>
                            <label style={S.label}>Server Name</label>
                            <input style={S.input} value={mcpName}
                                onChange={e => setMcpName(e.target.value)}
                                placeholder="e.g. filesystem, github, postgres"
                                onFocus={e => { e.target.style.borderColor = '#a855f7'; }}
                                onBlur={e => { e.target.style.borderColor = '#333'; }} />
                        </div>
                        <div style={S.field}>
                            <label style={S.label}>Server URL / Command</label>
                            <input style={S.input} value={mcpUrl}
                                onChange={e => setMcpUrl(e.target.value)}
                                placeholder="npx -y @modelcontextprotocol/server-filesystem /path"
                                onFocus={e => { e.target.style.borderColor = '#a855f7'; }}
                                onBlur={e => { e.target.style.borderColor = '#333'; }} />
                        </div>
                        <div style={S.field}>
                            <label style={S.label}>Description (optional)</label>
                            <input style={S.input} value={mcpDesc}
                                onChange={e => setMcpDesc(e.target.value)}
                                placeholder="What does this server provide?"
                                onFocus={e => { e.target.style.borderColor = '#a855f7'; }}
                                onBlur={e => { e.target.style.borderColor = '#333'; }} />
                        </div>
                        <div style={S.field}>
                            <label style={S.label}>Transport</label>
                            <select style={S.select} value={mcpTransport}
                                onChange={e => setMcpTransport(e.target.value as 'stdio' | 'http' | 'sse')}>
                                <option value="stdio">stdio (local process)</option>
                                <option value="http">HTTP (remote)</option>
                                <option value="sse">SSE (server-sent events)</option>
                            </select>
                        </div>
                        <div style={{ display: 'flex', gap: '8px', alignItems: 'center' }}>
                            <button
                                style={{ ...S.btnPrimary, background: '#a855f7', opacity: mcpSaving ? 0.5 : 1 }}
                                disabled={mcpSaving || !mcpName || !mcpUrl}
                                onClick={async () => {
                                    setMcpSaving(true);
                                    try {
                                        await request(`/workspaces/${workspaceId}/mcp-tools`, {
                                            method: 'POST',
                                            body: {
                                                name: mcpName,
                                                description: mcpDesc,
                                                serverUrl: mcpUrl,
                                                transport: mcpTransport,
                                            },
                                        });
                                        setMcpServers(prev => [...prev, {
                                            name: mcpName, description: mcpDesc,
                                            server_url: mcpUrl, transport: mcpTransport, enabled: true,
                                        }]);
                                        setMcpName(''); setMcpUrl(''); setMcpDesc('');
                                        setMcpTransport('stdio'); setShowAddMcp(false);
                                        setSaveStatus('MCP server added');
                                        setTimeout(() => setSaveStatus(null), 2000);
                                    } catch {
                                        setError('Failed to add MCP server');
                                        setTimeout(() => setError(null), 3000);
                                    } finally { setMcpSaving(false); }
                                }}
                            >
                                {mcpSaving ? 'Adding...' : 'Add Server'}
                            </button>
                            <span style={{ fontSize: '0.65rem', color: '#666' }}>
                                Kestrel will connect on next message
                            </span>
                        </div>
                    </div>
                )}

                {/* Installed MCP server list */}
                {mcpLoading ? (
                    <div style={{ textAlign: 'center', color: '#444', padding: '16px', fontSize: '0.78rem' }}>
                        Loading servers...
                    </div>
                ) : mcpServers.length > 0 ? (
                    <div>
                        {mcpServers.map(srv => (
                            <div key={srv.name} style={{
                                ...S.toolCard,
                                borderLeft: `2px solid ${srv.enabled ? '#a855f7' : '#333'}`,
                                opacity: srv.enabled ? 1 : 0.5,
                            }}>
                                <span style={{ fontSize: '1rem', width: '24px', textAlign: 'center', color: '#a855f7' }}>⧉</span>
                                <div style={{ flex: 1, minWidth: 0 }}>
                                    <div style={{ fontSize: '0.8rem', fontWeight: 500, color: '#e0e0e0', marginBottom: '2px' }}>
                                        {srv.name}
                                    </div>
                                    <div style={{ fontSize: '0.68rem', color: '#666', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                                        {srv.description || srv.server_url}
                                    </div>
                                    <div style={{ fontSize: '0.6rem', color: '#444', marginTop: '3px' }}>
                                        {srv.transport.toUpperCase()}
                                        {srv.installed_at && ` · Added ${new Date(srv.installed_at).toLocaleDateString()}`}
                                    </div>
                                </div>
                                <button
                                    style={{ ...S.btnGhost, padding: '4px 10px', fontSize: '0.65rem', color: '#ef4444', borderColor: '#ef4444' }}
                                    onClick={async () => {
                                        try {
                                            await request(`/workspaces/${workspaceId}/mcp-tools/${srv.name}`, { method: 'DELETE' });
                                            setMcpServers(prev => prev.filter(s => s.name !== srv.name));
                                        } catch { /* ignore */ }
                                    }}
                                >
                                    Remove
                                </button>
                            </div>
                        ))}
                    </div>
                ) : (
                    <div style={{ textAlign: 'center', color: '#333', padding: '20px', fontSize: '0.75rem' }}>
                        No MCP servers connected. Add one above or let Kestrel discover servers automatically.
                    </div>
                )}
            </div>
        </div>
    );
}
