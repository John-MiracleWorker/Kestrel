import { useState, useEffect } from 'react';
import { createPortal } from 'react-dom';
import { request } from '../../api/client';

/* ── Types ─────────────────────────────────────────────────────── */
interface DocFile {
    id: string;
    title: string;
    category: string;
    lastUpdated: string;
    content: string;
}

interface DocsPanelProps {
    workspaceId: string;
    isVisible: boolean;
    onClose: () => void;
}

/* ── Mock Docs ────────────────────────────────────────────────── */
const MOCK_DOCS: DocFile[] = [
    {
        id: 'overview', title: 'Overview', category: 'General',
        lastUpdated: new Date().toISOString(),
        content: `# Kestrel Platform Overview

Kestrel is an autonomous AI assistant platform built with a microservices architecture.

## Core Components

### Brain Service (Python)
The central intelligence service handling AI conversations, memory management, and tool execution.
- **gRPC server** on port 50051
- Manages LLM providers (local + cloud)
- RAG pipeline with vector similarity search
- Agent loop with planning, tool use, and reflection

### Gateway Service (Node.js)
REST + WebSocket API gateway that bridges frontend to brain.
- **Fastify** on port 8741
- JWT authentication with refresh tokens
- gRPC client for brain communication
- WebSocket for real-time streaming

### Frontend (React + Vite)
Modern single-page application with terminal-inspired dark UI.
- Real-time chat via WebSocket
- Settings panel for model/provider configuration
- Moltbook knowledge base viewer
- Notification system

## Architecture Diagram

\`\`\`
┌─────────┐     REST/WS     ┌─────────┐     gRPC      ┌────────┐
│Frontend │───────────────▸│ Gateway │──────────────▸│ Brain  │
│ (React) │◂───────────────│(Fastify)│◂──────────────│(Python)│
└─────────┘                 └────┬────┘               └───┬────┘
                                 │                        │
                            ┌────▼────┐             ┌────▼────┐
                            │  Redis  │             │Postgres │
                            └─────────┘             └─────────┘
\`\`\`
`,
    },
    {
        id: 'api-chat', title: '/api/chat', category: 'API Reference',
        lastUpdated: new Date(Date.now() - 3600000).toISOString(),
        content: `# Chat API

## POST /api/workspaces/:workspaceId/conversations/:conversationId/messages

Send a message and receive an AI response.

### Request
\`\`\`json
{
    "content": "Hello, help me with...",
    "role": "user"
}
\`\`\`

### Response (Streamed via WebSocket)
\`\`\`json
{
    "id": "msg-uuid",
    "content": "I can help with...",
    "role": "assistant",
    "toolActivity": [...],
    "timestamp": "2026-02-21T..."
}
\`\`\`

### Authentication
Requires \`Authorization: Bearer <token>\` header.
`,
    },
    {
        id: 'api-workspaces', title: '/api/workspaces', category: 'API Reference',
        lastUpdated: new Date(Date.now() - 7200000).toISOString(),
        content: `# Workspaces API

## GET /api/workspaces
List all workspaces for the authenticated user.

## POST /api/workspaces
Create a new workspace.

### Request
\`\`\`json
{
    "name": "My Workspace",
    "description": "Optional description"
}
\`\`\`

## PUT /api/workspaces/:workspaceId
Update workspace settings.

## DELETE /api/workspaces/:workspaceId
Delete a workspace and all associated data.
`,
    },
    {
        id: 'brain', title: 'Brain Service', category: 'Components',
        lastUpdated: new Date(Date.now() - 1800000).toISOString(),
        content: `# Brain Service

The Brain is the core intelligence engine of Kestrel.

## Key Modules

### server.py
Main gRPC service implementation. Handles:
- \`Chat\` — Streaming chat with LLM
- \`ParseCronJob\` — NL to cron expression
- Provider configuration management
- Memory and persona management

### agent/loop.py
Autonomous agent loop with:
- Planning phase (task decomposition)
- Tool execution (23 registered tools)
- Reflection and self-correction
- Safety sandboxing

### memory/graph.py
Knowledge graph with temporal decay:
- Entity extraction from conversations
- Relationship tracking
- Weight-based relevance scoring
- Periodic consolidation

### providers/
LLM provider abstraction:
- \`LocalProvider\` — Ollama/local models
- \`CloudProvider\` — Gemini, OpenAI, Anthropic
`,
    },
    {
        id: 'gateway', title: 'Gateway Service', category: 'Components',
        lastUpdated: new Date(Date.now() - 5400000).toISOString(),
        content: `# Gateway Service

Node.js API gateway built with Fastify.

## Route Groups

| Prefix | Module | Purpose |
|--------|--------|---------|
| /api/auth | auth.ts | Login, register, refresh |
| /api/workspaces | workspaces.ts | Workspace CRUD |
| /api/workspaces/:id/conversations | conversations.ts | Chat management |
| /api/workspaces/:id/automation | automation.ts | Cron jobs, webhooks |
| /api/workspaces/:id/mcp-tools | mcp.ts | MCP server management |
| /ws | websocket.ts | Real-time streaming |

## Brain Client
\`BrainClient\` in \`brain/client.ts\` wraps gRPC calls with:
- Automatic reconnection
- Request/response logging
- Error translation to HTTP status codes
`,
    },
    {
        id: 'frontend', title: 'Frontend', category: 'Components',
        lastUpdated: new Date(Date.now() - 900000).toISOString(),
        content: `# Frontend

React 19 + Vite 6 application.

## Key Components

### App.tsx
Root component. Manages:
- Authentication state
- Workspace/conversation selection
- Panel visibility (Settings, Moltbook, Memory Palace, Screen Share)

### ChatView.tsx
Main chat interface with:
- Message rendering with rich content (markdown, code, mermaid)
- Streaming message display
- Tool activity indicators
- Agent activity feed

### SettingsPanel.tsx
Configuration UI with tabs:
- Model — LLM provider selection
- Persona — AI personality settings
- Memory — RAG configuration
- Tools — Tool management
- Automation — NL cron jobs
- Integrations — Telegram, GitHub

### Sidebar.tsx
Navigation sidebar with:
- Workspace selector
- Conversation list
- Quick action buttons
`,
    },
    {
        id: 'data-flow', title: 'Data Flow', category: 'Architecture',
        lastUpdated: new Date(Date.now() - 10800000).toISOString(),
        content: `# Data Flow

## Chat Message Flow
1. User types message in ChatView
2. \`useChat\` hook sends via WebSocket
3. Gateway receives and forwards to Brain via gRPC \`Chat\` stream
4. Brain processes: memory retrieval → agent loop → LLM call
5. Response chunks streamed back through gRPC → WS → UI

## Memory Flow
1. After each conversation, Brain extracts entities
2. Entities stored in \`memory_entities\` table with embeddings
3. Relationships stored in \`memory_relations\` table
4. Periodic decay reduces stale entity weights
5. Retrieved via similarity search during chat context building

## Authentication Flow
1. User logs in → Gateway issues JWT + refresh token
2. Access token (1h) sent in Authorization header
3. Refresh token (7d) used to get new access token
4. Tokens stored in localStorage on frontend
`,
    },
];

const CATEGORIES = ['General', 'Architecture', 'API Reference', 'Components'];

/* ── Styles ────────────────────────────────────────────────────── */
const S = {
    overlay: {
        position: 'fixed' as const, top: 0, left: 0, right: 0, bottom: 0,
        background: 'rgba(0,0,0,0.85)', backdropFilter: 'blur(8px)',
        zIndex: 10000, display: 'flex', fontFamily: 'JetBrains Mono, monospace',
    },
    nav: {
        width: '220px', background: '#080808', borderRight: '1px solid #222',
        overflowY: 'auto' as const, padding: '16px 0',
    },
    navHeader: {
        padding: '0 16px 16px', fontSize: '0.75rem', fontWeight: 700,
        color: '#00f3ff', letterSpacing: '0.06em',
        borderBottom: '1px solid #222', marginBottom: '8px',
    },
    categoryTitle: {
        fontSize: '0.6rem', color: '#444', textTransform: 'uppercase' as const,
        letterSpacing: '0.08em', padding: '12px 16px 4px',
    },
    navItem: (active: boolean) => ({
        padding: '6px 16px', fontSize: '0.7rem', cursor: 'pointer',
        color: active ? '#e0e0e0' : '#666',
        background: active ? 'rgba(0,243,255,0.06)' : 'transparent',
        borderLeft: active ? '2px solid #00f3ff' : '2px solid transparent',
        transition: 'all 0.15s',
    }),
    content: {
        flex: 1, display: 'flex', flexDirection: 'column' as const,
    },
    contentHeader: {
        display: 'flex', alignItems: 'center', justifyContent: 'space-between',
        padding: '12px 24px', borderBottom: '1px solid #222', background: '#080808',
    },
    closeBtn: {
        background: 'none', border: 'none', color: '#555',
        fontSize: '1.1rem', cursor: 'pointer', padding: '4px 8px',
    },
    driftBanner: {
        display: 'flex', alignItems: 'center', gap: '8px',
        padding: '8px 24px', background: 'rgba(245,158,11,0.08)',
        borderBottom: '1px solid rgba(245,158,11,0.2)',
        fontSize: '0.65rem', color: '#f59e0b',
    },
    contentBody: {
        flex: 1, overflowY: 'auto' as const, padding: '24px',
    },
    markdown: {
        fontSize: '0.78rem', color: '#e0e0e0', lineHeight: 1.7,
    },
    regenBtn: {
        background: 'rgba(168,85,247,0.1)', border: '1px solid rgba(168,85,247,0.3)',
        borderRadius: '4px', color: '#a855f7', fontSize: '0.65rem',
        padding: '4px 12px', cursor: 'pointer',
    },
    freshness: (stale: boolean) => ({
        display: 'inline-flex', alignItems: 'center', gap: '4px',
        fontSize: '0.6rem', color: stale ? '#f59e0b' : '#10b981',
    }),
    freshnesssDot: (stale: boolean) => ({
        width: '5px', height: '5px', borderRadius: '50%',
        background: stale ? '#f59e0b' : '#10b981',
    }),
    codeBlock: {
        background: '#0d0d0d', border: '1px solid #1a1a1a', borderRadius: '4px',
        padding: '12px', margin: '12px 0', fontFamily: 'JetBrains Mono, monospace',
        fontSize: '0.72rem', color: '#00f3ff', overflowX: 'auto' as const,
        whiteSpace: 'pre-wrap' as const,
    },
    table: {
        width: '100%', borderCollapse: 'collapse' as const, margin: '12px 0',
        fontSize: '0.7rem',
    },
};

/* ── Simple Markdown Renderer ─────────────────────────────────── */
function renderMarkdown(content: string): React.ReactElement[] {
    const lines = content.split('\n');
    const elements: React.ReactElement[] = [];
    let inCode = false;
    let codeContent = '';
    let key = 0;

    for (const line of lines) {
        if (line.startsWith('```')) {
            if (inCode) {
                elements.push(<pre key={key++} style={S.codeBlock}>{codeContent.trim()}</pre>);
                codeContent = '';
                inCode = false;
            } else {
                inCode = true;
            }
            continue;
        }
        if (inCode) { codeContent += line + '\n'; continue; }
        if (!line.trim()) { elements.push(<br key={key++} />); continue; }
        if (line.startsWith('# ')) {
            elements.push(<h1 key={key++} style={{ fontSize: '1.1rem', color: '#e0e0e0', margin: '20px 0 8px', fontWeight: 700 }}>{line.slice(2)}</h1>);
        } else if (line.startsWith('## ')) {
            elements.push(<h2 key={key++} style={{ fontSize: '0.9rem', color: '#ccc', margin: '16px 0 6px', fontWeight: 600 }}>{line.slice(3)}</h2>);
        } else if (line.startsWith('### ')) {
            elements.push(<h3 key={key++} style={{ fontSize: '0.8rem', color: '#aaa', margin: '12px 0 4px', fontWeight: 600 }}>{line.slice(4)}</h3>);
        } else if (line.startsWith('| ')) {
            // Table row
            const cells = line.split('|').slice(1, -1).map(c => c.trim());
            if (cells.every(c => c.match(/^-+$/))) continue; // separator
            elements.push(
                <tr key={key++} style={{ borderBottom: '1px solid #1a1a1a' }}>
                    {cells.map((c, i) => (
                        <td key={i} style={{ padding: '4px 8px', color: '#e0e0e0', fontSize: '0.7rem' }}>{c}</td>
                    ))}
                </tr>
            );
        } else if (line.startsWith('- ')) {
            elements.push(
                <div key={key++} style={{ paddingLeft: '16px', fontSize: '0.75rem', color: '#ccc', margin: '2px 0' }}>
                    • {renderInlineCode(line.slice(2))}
                </div>
            );
        } else {
            elements.push(<p key={key++} style={{ margin: '4px 0', fontSize: '0.75rem', color: '#ccc' }}>{renderInlineCode(line)}</p>);
        }
    }
    // Wrap table rows
    const final: React.ReactElement[] = [];
    let tableRows: React.ReactElement[] = [];
    for (const el of elements) {
        if (el.type === 'tr') {
            tableRows.push(el);
        } else {
            if (tableRows.length) {
                final.push(
                    <table key={`table-${key++}`} style={S.table}>
                        <tbody>{tableRows}</tbody>
                    </table>
                );
                tableRows = [];
            }
            final.push(el);
        }
    }
    if (tableRows.length) {
        final.push(<table key={`table-${key++}`} style={S.table}><tbody>{tableRows}</tbody></table>);
    }
    return final;
}

function renderInlineCode(text: string): React.ReactNode {
    const parts = text.split(/(`[^`]+`)/g);
    return parts.map((part, i) => {
        if (part.startsWith('`') && part.endsWith('`')) {
            return <code key={i} style={{ background: '#1a1a1a', padding: '1px 4px', borderRadius: '2px', color: '#00f3ff', fontSize: '0.7rem' }}>{part.slice(1, -1)}</code>;
        }
        return part;
    });
}

/* ── Component ────────────────────────────────────────────────── */
export function DocsPanel({ workspaceId, isVisible, onClose }: DocsPanelProps) {
    const [docs, setDocs] = useState<DocFile[]>(MOCK_DOCS);
    const [selectedDoc, setSelectedDoc] = useState<string>('overview');
    const [regenerating, setRegenerating] = useState(false);
    const [driftCount] = useState(3);

    const activeDoc = docs.find(d => d.id === selectedDoc) || docs[0];
    const timeSince = (date: string) => {
        const diff = Date.now() - new Date(date).getTime();
        if (diff < 60000) return 'just now';
        if (diff < 3600000) return `${Math.floor(diff / 60000)}m ago`;
        if (diff < 86400000) return `${Math.floor(diff / 3600000)}h ago`;
        return `${Math.floor(diff / 86400000)}d ago`;
    };
    const isStale = activeDoc ? (Date.now() - new Date(activeDoc.lastUpdated).getTime() > 3600000) : false;

    const handleRegenerate = async () => {
        setRegenerating(true);
        setTimeout(() => {
            setRegenerating(false);
        }, 2000);
    };

    if (!isVisible) return null;

    return createPortal(
        <div style={S.overlay}>
            {/* Left Nav */}
            <div style={S.nav}>
                <div style={S.navHeader}>✦ Kestrel Docs</div>
                {CATEGORIES.map(cat => (
                    <div key={cat}>
                        <div style={S.categoryTitle}>{cat}</div>
                        {docs.filter(d => d.category === cat).map(doc => (
                            <div
                                key={doc.id}
                                style={S.navItem(selectedDoc === doc.id)}
                                onClick={() => setSelectedDoc(doc.id)}
                                onMouseEnter={e => { if (selectedDoc !== doc.id) (e.target as HTMLElement).style.color = '#aaa'; }}
                                onMouseLeave={e => { if (selectedDoc !== doc.id) (e.target as HTMLElement).style.color = '#666'; }}
                            >
                                {doc.title}
                            </div>
                        ))}
                    </div>
                ))}
            </div>

            {/* Right Content */}
            <div style={S.content}>
                <div style={S.contentHeader}>
                    <div style={{ display: 'flex', alignItems: 'center', gap: '12px' }}>
                        <span style={S.freshness(isStale)}>
                            <span style={S.freshnesssDot(isStale)} />
                            {timeSince(activeDoc.lastUpdated)}
                            {isStale && ' (stale)'}
                        </span>
                    </div>
                    <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
                        <button style={S.regenBtn} onClick={handleRegenerate} disabled={regenerating}>
                            {regenerating ? 'Regenerating...' : '✦ Regenerate'}
                        </button>
                        <button style={S.closeBtn} onClick={onClose}>✕</button>
                    </div>
                </div>

                {driftCount > 0 && (
                    <div style={S.driftBanner}>
                        ⚠ {driftCount} files changed since last sync
                    </div>
                )}

                <div style={S.contentBody}>
                    <div style={S.markdown}>
                        {renderMarkdown(activeDoc.content)}
                    </div>
                </div>
            </div>
        </div>,
        document.body
    );
}
