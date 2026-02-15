/**
 * Libre Bird — Main Application Logic
 * Free, offline, privacy-first AI assistant
 */

// ── State ─────────────────────────────────────────────────────
const state = {
    currentView: 'chat',
    currentConversation: null,
    conversations: [],
    messages: [],
    tasks: [],
    taskFilter: 'all',
    includeContext: true,
    isStreaming: false,
    modelLoaded: false,
    settings: {},
    voiceActive: false,
    voicePolling: null,
    attachedFile: null,
};

// ── API Helpers ───────────────────────────────────────────────
const api = {
    async get(url) {
        const res = await fetch(url);
        if (!res.ok) {
            const err = await res.json().catch(() => ({ detail: res.statusText }));
            throw new Error(err.detail || 'Request failed');
        }
        return res.json();
    },
    async post(url, body = {}) {
        const res = await fetch(url, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body),
        });
        if (!res.ok && !res.headers.get('content-type')?.includes('text/event-stream')) {
            const err = await res.json().catch(() => ({ detail: res.statusText }));
            throw new Error(err.detail || 'Request failed');
        }
        return res;
    },
    async put(url, body = {}) {
        const res = await fetch(url, {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body),
        });
        if (!res.ok) {
            const err = await res.json().catch(() => ({ detail: res.statusText }));
            throw new Error(err.detail || 'Request failed');
        }
        return res.json();
    },
    async del(url) {
        const res = await fetch(url, { method: 'DELETE' });
        if (!res.ok) throw new Error('Delete failed');
        return res.json();
    },
};

// ── Initialization ────────────────────────────────────────────
document.addEventListener('DOMContentLoaded', async () => {
    setupNavigation();
    setupChat();
    setupTasks();
    setupSettings();
    setupVoice();
    await refreshStatus();
    await loadConversations();
    // Periodic status refresh
    setInterval(refreshStatus, 10000);
});

async function refreshStatus() {
    try {
        const status = await api.get('/api/status');
        state.modelLoaded = status.model_loaded;

        // Update context status indicator
        const statusEl = document.getElementById('context-status');
        const dot = statusEl.querySelector('.status-dot');
        const label = statusEl.querySelector('span');

        if (status.context_paused) {
            dot.className = 'status-dot paused';
            label.textContent = 'Context: paused';
        } else if (status.context_collecting) {
            dot.className = 'status-dot online';
            label.textContent = 'Context: active';
        } else {
            dot.className = 'status-dot offline';
            label.textContent = 'Context: off';
        }

        // Show warning if no model loaded
        updateModelWarning();
    } catch (e) {
        const statusEl = document.getElementById('context-status');
        const dot = statusEl.querySelector('.status-dot');
        const label = statusEl.querySelector('span');
        dot.className = 'status-dot offline';
        label.textContent = 'Server: offline';
    }
}

function updateModelWarning() {
    let warning = document.querySelector('.no-model-warning');
    if (!state.modelLoaded) {
        if (!warning) {
            warning = document.createElement('div');
            warning.className = 'no-model-warning';
            warning.innerHTML = '⚠️ <span>No model loaded. <a id="go-settings">Go to Settings</a> to load a GGUF model.</span>';
            const chatContainer = document.querySelector('.chat-container');
            chatContainer.insertBefore(warning, chatContainer.querySelector('.chat-messages'));
            warning.querySelector('#go-settings').addEventListener('click', () => switchView('settings'));
        }
    } else if (warning) {
        warning.remove();
    }
}

// ── Navigation ────────────────────────────────────────────────
function setupNavigation() {
    const navItems = document.querySelectorAll('.nav-item');
    navItems.forEach(item => {
        item.addEventListener('click', () => {
            const view = item.dataset.view;
            switchView(view);
        });
    });
}

function switchView(viewName) {
    state.currentView = viewName;

    // Update nav
    document.querySelectorAll('.nav-item').forEach(n => n.classList.remove('active'));
    document.querySelector(`[data-view="${viewName}"]`)?.classList.add('active');

    // Update views
    document.querySelectorAll('.view').forEach(v => v.classList.remove('active'));
    document.getElementById(`view-${viewName}`)?.classList.add('active');

    // Show/hide conversations list
    const convList = document.getElementById('conversations-list');
    convList.style.display = viewName === 'chat' ? 'flex' : 'none';

    // Load view data
    if (viewName === 'journal') loadJournals();
    if (viewName === 'tasks') loadTasks();
    if (viewName === 'settings') loadSettings();
}

// ── Slash Commands Definition ─────────────────────────────────
const SLASH_COMMANDS = [
    { cmd: '/summarize', desc: 'Summarize the conversation', icon: '📝' },
    { cmd: '/explain', desc: 'Explain something simply', icon: '💡' },
    { cmd: '/translate', desc: 'Translate to another language', icon: '🌐' },
    { cmd: '/email', desc: 'Draft an email', icon: '✉️' },
    { cmd: '/code', desc: 'Write or fix code', icon: '💻' },
    { cmd: '/help', desc: 'Show available commands', icon: '❓' },
];

// ── Chat ──────────────────────────────────────────────────────
function setupChat() {
    const input = document.getElementById('chat-input');
    const sendBtn = document.getElementById('send-btn');
    const newChatBtn = document.getElementById('new-chat-btn');
    const contextBtn = document.getElementById('context-btn');

    // Auto-resize textarea
    input.addEventListener('input', () => {
        input.style.height = 'auto';
        input.style.height = Math.min(input.scrollHeight, 150) + 'px';
        // Slash command hints
        handleSlashHints(input.value);
    });

    // Send on Enter (Shift+Enter for newline)
    input.addEventListener('keydown', (e) => {
        if (e.key === 'Enter' && !e.shiftKey) {
            e.preventDefault();
            sendMessage();
        }
    });

    sendBtn.addEventListener('click', sendMessage);
    newChatBtn.addEventListener('click', startNewConversation);

    contextBtn.addEventListener('click', () => {
        state.includeContext = !state.includeContext;
        contextBtn.classList.toggle('active', state.includeContext);
        contextBtn.querySelector('span').textContent = state.includeContext ? 'Context On' : 'Context Off';
    });

    // ── Conversation Search ────────────────────────────────
    const searchInput = document.getElementById('conv-search-input');
    if (searchInput) {
        searchInput.addEventListener('input', () => {
            renderConversations(searchInput.value.trim().toLowerCase());
        });
    }

    // ── File Drop ──────────────────────────────────────────
    setupFileDrop();

    // ── Model Quick-Switch ────────────────────────────────
    setupModelPill();

    // ── File attachment remove ─────────────────────────────
    const removeBtn = document.getElementById('file-attachment-remove');
    if (removeBtn) {
        removeBtn.addEventListener('click', () => {
            state.attachedFile = null;
            document.getElementById('file-attachment').style.display = 'none';
            const uploadBtn = document.getElementById('upload-btn');
            if (uploadBtn) uploadBtn.classList.remove('has-file');
        });
    }
}

async function sendMessage() {
    const input = document.getElementById('chat-input');
    let message = input.value.trim();
    if (!message || state.isStreaming) return;

    // Handle /help client-side
    if (message === '/help') {
        input.value = '';
        const helpText = SLASH_COMMANDS.map(c => `${c.icon} **${c.cmd}** — ${c.desc}`).join('\n');
        appendMessage('user', '/help');
        appendMessage('assistant', `## Available Commands\n\n${helpText}\n\n---\nYou can also **drop files** into the chat to analyze them.`);
        document.getElementById('slash-hints').style.display = 'none';
        return;
    }

    // Attach file content to message if present
    if (state.attachedFile) {
        const fileCtx = `[Attached file: ${state.attachedFile.name}]\n\`\`\`${state.attachedFile.ext.replace('.', '')}\n${state.attachedFile.content}\n\`\`\`\n\n`;
        message = fileCtx + (message || `Analyze this ${state.attachedFile.ext} file.`);
        state.attachedFile = null;
        document.getElementById('file-attachment').style.display = 'none';
    }

    // Hide slash hints
    const hintsEl = document.getElementById('slash-hints');
    if (hintsEl) hintsEl.style.display = 'none';

    // Clear suggestions
    const sugBar = document.getElementById('suggestions-bar');
    if (sugBar) sugBar.innerHTML = '';

    // Clear welcome message
    const welcome = document.querySelector('.welcome-message');
    if (welcome) welcome.remove();

    // Add user message to UI
    appendMessage('user', input.value.trim() || message.substring(0, 100));
    input.value = '';
    input.style.height = 'auto';

    // Show typing indicator
    const typingEl = appendTyping();

    // Stream the response
    state.isStreaming = true;
    document.getElementById('send-btn').disabled = true;

    try {
        const res = await api.post('/api/chat', {
            message,
            conversation_id: state.currentConversation,
            include_context: state.includeContext,
            temperature: parseFloat(state.settings.temperature || 0.7),
            max_tokens: parseInt(state.settings.max_tokens || 2048),
        });

        // Remove typing indicator
        typingEl.remove();

        // Create assistant message bubble
        const assistantEl = appendMessage('assistant', '');
        const bubble = assistantEl.querySelector('.message-bubble');

        // Thinking block (collapsible)
        let thinkingEl = null;
        let thinkingContent = null;
        let thinkingText = '';
        let toolIndicator = null;

        // Read SSE stream
        const reader = res.body.getReader();
        const decoder = new TextDecoder();
        let fullResponse = '';
        let buffer = '';
        let currentEvent = 'message';

        while (true) {
            const { value, done } = await reader.read();
            if (done) break;

            buffer += decoder.decode(value, { stream: true });

            // Parse SSE events (event: + data: lines)
            const lines = buffer.split('\n');
            buffer = lines.pop() || '';

            for (const line of lines) {
                if (line.startsWith('event:')) {
                    currentEvent = line.slice(6).trim();
                } else if (line.startsWith('data:')) {
                    const data = line.slice(5).trim();
                    if (!data) continue;
                    try {
                        const parsed = JSON.parse(data);

                        if (currentEvent === 'thinking') {
                            // Create thinking block if needed
                            if (!thinkingEl) {
                                thinkingEl = document.createElement('details');
                                thinkingEl.className = 'thinking-block';
                                thinkingEl.open = true;
                                thinkingEl.innerHTML = `<summary>🧠 Thinking…</summary><div class="thinking-content"></div>`;
                                bubble.appendChild(thinkingEl);
                                thinkingContent = thinkingEl.querySelector('.thinking-content');
                            }
                            if (parsed.token) {
                                thinkingText += parsed.token;
                                thinkingContent.innerHTML = formatMarkdown(thinkingText);
                                scrollToBottom();
                            }
                        } else if (currentEvent === 'thinking_done') {
                            // Collapse thinking and update label
                            if (thinkingEl) {
                                thinkingEl.open = false;
                                thinkingEl.querySelector('summary').textContent = '🧠 Thought process';
                            }
                        } else if (currentEvent === 'tool') {
                            // Show tool indicator — parse new format with step info
                            const toolName = parsed.name || parsed.tool || 'tool';
                            const stepNum = parsed.step || 0;
                            const maxSteps = parsed.max_steps || 10;
                            const friendlyNames = {
                                get_datetime: '🕐 Getting time…',
                                web_search: '🔍 Searching the web…',
                                calculator: '🧮 Calculating…',
                                get_weather: '🌤️ Checking weather…',
                                open_url: '🌐 Opening URL…',
                                search_files: '📂 Searching files…',
                                get_system_info: '💻 Getting system info…',
                                image_generate: '🎨 Generating image (this may take a while)…',
                                read_url: '📖 Reading webpage…',
                                read_screen: '👁️ Reading your screen…',
                                run_code: '⚙️ Running code…',
                                shell_command: '💻 Running command…',
                                speak: '🔊 Speaking…',
                                knowledge_search: '🧠 Searching knowledge…',
                                knowledge_add: '🧠 Saving to knowledge…',
                                set_reminder: '⏰ Setting reminder…',
                                clipboard: '📋 Using clipboard…',
                                open_app: '📱 Opening app…',
                                system_control: '⚙️ System control…',
                                music_control: '🎵 Controlling music…',
                                file_operations: '📁 Working with files…',
                                keyboard: '⌨️ Typing…',
                                read_document: '📄 Reading document…',
                                read_notifications: '🔔 Checking notifications…',
                                analyze_screen: '👁️ Analyzing screen (VLM)…',
                                manage_preferences: '🧠 Learning preferences…',
                                workflow: '🔄 Running workflow…',
                            };
                            if (!toolIndicator) {
                                toolIndicator = document.createElement('div');
                                toolIndicator.className = 'tool-indicator';
                                toolIndicator._startTime = Date.now();

                                // Spinner
                                const spinner = document.createElement('div');
                                spinner.className = 'tool-indicator-spinner';
                                toolIndicator.appendChild(spinner);

                                // Content wrapper
                                const content = document.createElement('div');
                                content.className = 'tool-indicator-content';

                                const label = document.createElement('div');
                                label.className = 'tool-indicator-label';
                                content.appendChild(label);

                                // Step badge for multi-step chains
                                const stepBadge = document.createElement('div');
                                stepBadge.className = 'tool-indicator-step';
                                content.appendChild(stepBadge);

                                const elapsed = document.createElement('div');
                                elapsed.className = 'tool-indicator-elapsed';
                                elapsed.textContent = '0s elapsed';
                                content.appendChild(elapsed);

                                toolIndicator.appendChild(content);
                                bubble.appendChild(toolIndicator);

                                // Live elapsed timer
                                toolIndicator._timer = setInterval(() => {
                                    const secs = Math.floor((Date.now() - toolIndicator._startTime) / 1000);
                                    if (secs < 60) {
                                        elapsed.textContent = `${secs}s elapsed`;
                                    } else {
                                        const m = Math.floor(secs / 60);
                                        const s = secs % 60;
                                        elapsed.textContent = `${m}m ${s.toString().padStart(2, '0')}s elapsed`;
                                    }
                                }, 1000);
                            }
                            const labelEl = toolIndicator.querySelector('.tool-indicator-label');
                            if (labelEl) labelEl.textContent = friendlyNames[toolName] || `🔧 Using ${toolName}…`;
                            const stepEl = toolIndicator.querySelector('.tool-indicator-step');
                            if (stepEl && stepNum > 0) {
                                stepEl.textContent = `Step ${stepNum}`;
                                stepEl.style.display = 'inline-block';
                            }
                            scrollToBottom();
                        } else if (currentEvent === 'token') {
                            // Remove tool indicator when answer starts
                            if (toolIndicator) {
                                if (toolIndicator._timer) clearInterval(toolIndicator._timer);
                                toolIndicator.remove();
                                toolIndicator = null;
                            }
                            if (parsed.token) {
                                fullResponse += parsed.token;
                                // Render after thinking block
                                let answerEl = bubble.querySelector('.answer-content');
                                if (!answerEl) {
                                    answerEl = document.createElement('div');
                                    answerEl.className = 'answer-content';
                                    bubble.appendChild(answerEl);
                                }
                                answerEl.innerHTML = formatMarkdown(fullResponse);
                                scrollToBottom();
                            }
                        } else if (currentEvent === 'done' || parsed.conversation_id) {
                            if (parsed.conversation_id) {
                                state.currentConversation = parsed.conversation_id;
                                await loadConversations();
                            }
                        }
                    } catch (e) { }
                    currentEvent = 'message'; // Reset after processing data
                }
            }
        }

        if (!fullResponse) {
            let answerEl = bubble.querySelector('.answer-content');
            if (!answerEl) {
                answerEl = document.createElement('div');
                answerEl.className = 'answer-content';
                bubble.appendChild(answerEl);
            }
            if (!thinkingText) {
                answerEl.textContent = '(Empty response)';
            }
        }
    } catch (e) {
        typingEl?.remove();
        appendMessage('assistant', `❌ Error: ${e.message}`);
    }

    state.isStreaming = false;
    document.getElementById('send-btn').disabled = false;

    // Generate smart follow-up suggestions
    if (fullResponse) {
        showSuggestions(fullResponse, text => {
            document.getElementById('chat-input').value = text;
            sendMessage();
        });
    }
}

function appendMessage(role, content) {
    const container = document.getElementById('chat-messages');
    const div = document.createElement('div');
    div.className = `message ${role}`;
    const avatar = role === 'assistant' ? '🕊️' : '👤';
    div.innerHTML = `
        <div class="message-avatar">${avatar}</div>
        <div class="message-bubble">${content ? formatMarkdown(content) : ''}</div>
    `;
    container.appendChild(div);
    scrollToBottom();
    return div;
}

function appendTyping() {
    const container = document.getElementById('chat-messages');
    const div = document.createElement('div');
    div.className = 'message assistant';
    div.innerHTML = `
        <div class="message-avatar">🕊️</div>
        <div class="message-bubble">
            <div class="typing-indicator">
                <span></span><span></span><span></span>
            </div>
        </div>
    `;
    container.appendChild(div);
    scrollToBottom();
    return div;
}

function scrollToBottom() {
    const container = document.getElementById('chat-messages');
    container.scrollTop = container.scrollHeight;
}

function formatMarkdown(text) {
    if (!text) return '';

    // Pass 1: Extract fenced code blocks and protect them
    const codeBlocks = [];
    let processed = text.replace(/```(\w*)\n([\s\S]*?)```/g, (_, lang, code) => {
        const idx = codeBlocks.length;
        const escapedCode = code.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
        const langLabel = lang ? `<span class="code-lang">${lang}</span>` : '';
        const copyBtn = `<button class="code-copy" onclick="navigator.clipboard.writeText(this.parentElement.querySelector('code').textContent)">📋</button>`;
        codeBlocks.push(`<div class="code-block">${langLabel}${copyBtn}<pre><code class="lang-${lang || 'text'}">${escapedCode}</code></pre></div>`);
        return `\x00CODE${idx}\x00`;
    });
    // Pass 1.5: Detect image paths and convert to inline images
    // Matches: /Users/.../Pictures/libre-bird/filename.png or /generated/filename.png
    processed = processed.replace(
        /(?:\/Users\/[^\s]*\/Pictures\/libre-bird\/|\/generated\/)([a-zA-Z0-9_-]+\.png)/g,
        (match, filename) => {
            const idx = codeBlocks.length;
            codeBlocks.push(
                `<div class="generated-image-card">` +
                `<img src="/generated/${filename}" alt="Generated image" class="generated-image" onclick="this.classList.toggle('expanded')" />` +
                `<div class="generated-image-caption">🎨 Generated image</div>` +
                `</div>`
            );
            return `\x00CODE${idx}\x00`;
        }
    );

    // Escape HTML in remaining text
    processed = processed.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');

    // Pass 2: Block-level elements (process line by line)
    const lines = processed.split('\n');
    let html = '';
    let inList = false;
    let listType = '';
    let inBlockquote = false;
    let inTable = false;
    let tableRows = [];

    function closeList() {
        if (inList) { html += `</${listType}>`; inList = false; }
    }
    function closeBlockquote() {
        if (inBlockquote) { html += '</blockquote>'; inBlockquote = false; }
    }
    function closeTable() {
        if (inTable) {
            // Build table
            let t = '<table>';
            tableRows.forEach((row, i) => {
                const tag = i === 0 ? 'th' : 'td';
                const cells = row.split('|').filter(c => c.trim() !== '');
                t += '<tr>' + cells.map(c => `<${tag}>${c.trim()}</${tag}>`).join('') + '</tr>';
            });
            t += '</table>';
            html += t;
            inTable = false;
            tableRows = [];
        }
    }

    for (let i = 0; i < lines.length; i++) {
        const line = lines[i];

        // Code block placeholder
        if (/\x00CODE\d+\x00/.test(line)) {
            closeList(); closeBlockquote(); closeTable();
            html += line;
            continue;
        }

        // Table rows (detect |...|...|)
        const tableMatch = line.match(/^\|(.+)\|$/);
        if (tableMatch) {
            closeList(); closeBlockquote();
            // Skip separator rows (|---|---|)
            if (/^\|[\s\-:|]+\|$/.test(line)) continue;
            if (!inTable) inTable = true;
            tableRows.push(line);
            continue;
        } else {
            closeTable();
        }

        // Horizontal rule
        if (/^(\-{3,}|\*{3,}|_{3,})$/.test(line.trim())) {
            closeList(); closeBlockquote();
            html += '<hr>';
            continue;
        }

        // Headings
        const headingMatch = line.match(/^(#{1,4})\s+(.+)$/);
        if (headingMatch) {
            closeList(); closeBlockquote();
            const level = headingMatch[1].length;
            html += `<h${level}>${inlineFormat(headingMatch[2])}</h${level}>`;
            continue;
        }

        // Blockquote
        if (line.startsWith('&gt; ') || line === '&gt;') {
            closeList();
            if (!inBlockquote) { html += '<blockquote>'; inBlockquote = true; }
            html += inlineFormat(line.replace(/^&gt;\s?/, '')) + '<br>';
            continue;
        } else {
            closeBlockquote();
        }

        // Unordered list
        const ulMatch = line.match(/^(\s*)[*\-+]\s+(.+)$/);
        if (ulMatch) {
            closeBlockquote();
            if (!inList || listType !== 'ul') {
                closeList();
                html += '<ul>'; inList = true; listType = 'ul';
            }
            html += `<li>${inlineFormat(ulMatch[2])}</li>`;
            continue;
        }

        // Ordered list
        const olMatch = line.match(/^(\s*)\d+\.\s+(.+)$/);
        if (olMatch) {
            closeBlockquote();
            if (!inList || listType !== 'ol') {
                closeList();
                html += '<ol>'; inList = true; listType = 'ol';
            }
            html += `<li>${inlineFormat(olMatch[2])}</li>`;
            continue;
        }

        // Regular line
        closeList();
        if (line.trim() === '') {
            html += '<br>';
        } else {
            html += `<p>${inlineFormat(line)}</p>`;
        }
    }

    closeList(); closeBlockquote(); closeTable();

    // Pass 3: Restore code blocks
    html = html.replace(/\x00CODE(\d+)\x00/g, (_, idx) => codeBlocks[parseInt(idx)]);

    return html;
}

function inlineFormat(text) {
    return text
        // Bold
        .replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>')
        // Strikethrough
        .replace(/~~(.+?)~~/g, '<del>$1</del>')
        // Italic
        .replace(/\*(.+?)\*/g, '<em>$1</em>')
        .replace(/_(.+?)_/g, '<em>$1</em>')
        // Inline code
        .replace(/`([^`]+)`/g, '<code>$1</code>')
        // Links
        .replace(/\[([^\]]+)\]\(([^)]+)\)/g, '<a href="$2" target="_blank" rel="noopener">$1</a>');
}

// ── Conversations ─────────────────────────────────────────────
async function loadConversations() {
    try {
        state.conversations = await api.get('/api/conversations');
        renderConversations();
    } catch (e) { }
}

function renderConversations(searchQuery = '') {
    const container = document.getElementById('conversations-items');
    let convs = state.conversations;

    // Filter by search query
    if (searchQuery) {
        convs = convs.filter(c => (c.title || '').toLowerCase().includes(searchQuery));
    }

    if (convs.length === 0) {
        container.innerHTML = `<div style="padding: 16px; font-size: 0.8rem; color: var(--text-tertiary); text-align: center;">${searchQuery ? 'No matching conversations' : 'No conversations yet'}</div>`;
        return;
    }

    container.innerHTML = convs.map(c => `
        <div class="conv-item ${c.id === state.currentConversation ? 'active' : ''}" data-id="${c.id}">
            <span class="conv-item-title">${escapeHtml(c.title)}</span>
            <button class="conv-item-delete" data-id="${c.id}" title="Delete">
                <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                    <polyline points="3 6 5 6 21 6"></polyline>
                    <path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"></path>
                </svg>
            </button>
        </div>
    `).join('');

    // Click handlers
    container.querySelectorAll('.conv-item').forEach(el => {
        el.addEventListener('click', (e) => {
            if (e.target.closest('.conv-item-delete')) return;
            loadConversation(parseInt(el.dataset.id));
        });
    });

    container.querySelectorAll('.conv-item-delete').forEach(btn => {
        btn.addEventListener('click', async (e) => {
            e.stopPropagation();
            const id = parseInt(btn.dataset.id);
            await api.del(`/api/conversations/${id}`);
            if (state.currentConversation === id) {
                startNewConversation();
            }
            await loadConversations();
        });
    });
}

async function loadConversation(id) {
    state.currentConversation = id;
    renderConversations();

    // Load messages
    const messages = await api.get(`/api/conversations/${id}/messages`);

    const container = document.getElementById('chat-messages');
    container.innerHTML = '';

    if (messages.length === 0) {
        container.innerHTML = '<div class="welcome-message"><div class="welcome-icon">🕊️</div><h1>Start a conversation</h1><p>Ask me anything. I\'m right here on your Mac.</p></div>';
    } else {
        messages.forEach(m => {
            if (m.role !== 'system') {
                appendMessage(m.role, m.content);
            }
        });
    }
}

function startNewConversation() {
    state.currentConversation = null;
    const container = document.getElementById('chat-messages');
    container.innerHTML = `
        <div class="welcome-message">
            <div class="welcome-icon">🕊️</div>
            <h1>Welcome to Libre Bird</h1>
            <p>Your free, offline, privacy-first AI assistant.<br/>All data stays on your Mac. Always.</p>
            <div class="welcome-tips">
                <div class="tip-card">
                    <span class="tip-icon">💬</span>
                    <span>Ask me anything — I see your screen context</span>
                </div>
                <div class="tip-card">
                    <span class="tip-icon">📓</span>
                    <span>I'll journal your day automatically</span>
                </div>
                <div class="tip-card">
                    <span class="tip-icon">✅</span>
                    <span>I extract tasks from your activity</span>
                </div>
            </div>
        </div>
    `;
    renderConversations();
}

// ── Journal ───────────────────────────────────────────────────
async function loadJournals() {
    const container = document.getElementById('journal-content');
    const generateBtn = document.getElementById('generate-journal-btn');

    generateBtn.onclick = generateJournal;

    try {
        const journals = await api.get('/api/journal');
        if (journals.length === 0) {
            container.innerHTML = `
                <div class="empty-state">
                    <div class="empty-icon">📓</div>
                    <h3>No journal entries yet</h3>
                    <p>Keep Libre Bird running to collect activity data, then generate your daily summary.</p>
                </div>
            `;
            return;
        }

        container.innerHTML = journals.map(j => `
            <div class="journal-entry" data-date="${j.entry_date}">
                <h3>📓 Daily Journal</h3>
                <div class="journal-date">${formatDate(j.entry_date)}</div>
                <div class="journal-summary">${escapeHtml(j.summary)}</div>
            </div>
        `).join('');

        // Click to expand
        container.querySelectorAll('.journal-entry').forEach(el => {
            el.style.cursor = 'pointer';
            el.addEventListener('click', () => loadJournalDetail(el.dataset.date));
        });
    } catch (e) {
        container.innerHTML = `<div class="empty-state"><div class="empty-icon">⚠️</div><h3>Could not load journals</h3><p>${escapeHtml(e.message)}</p></div>`;
    }
}

async function loadJournalDetail(entryDate) {
    try {
        const journal = await api.get(`/api/journal/${entryDate}`);
        const container = document.getElementById('journal-content');

        let activitiesHtml = '';
        if (journal.activities?.length) {
            activitiesHtml = `
                <div class="journal-activities">
                    ${journal.activities.map(a => `<span class="activity-tag">${escapeHtml(a)}</span>`).join('')}
                </div>
            `;
        }

        container.innerHTML = `
            <div class="journal-entry">
                <h3>📓 Daily Journal</h3>
                <div class="journal-date">${formatDate(journal.entry_date)}</div>
                <div class="journal-summary">${escapeHtml(journal.summary)}</div>
                ${activitiesHtml}
            </div>
        `;
    } catch (e) { }
}

async function generateJournal() {
    const btn = document.getElementById('generate-journal-btn');
    const origText = btn.innerHTML;
    btn.innerHTML = '⏳ Generating...';
    btn.disabled = true;

    try {
        const result = await (await api.post('/api/journal/generate')).json();
        await loadJournals();
    } catch (e) {
        alert(`Could not generate journal: ${e.message}`);
    }

    btn.innerHTML = origText;
    btn.disabled = false;
}

// ── Tasks ─────────────────────────────────────────────────────
function setupTasks() {
    // Filter buttons
    document.querySelectorAll('.filter-btn').forEach(btn => {
        btn.addEventListener('click', () => {
            document.querySelectorAll('.filter-btn').forEach(b => b.classList.remove('active'));
            btn.classList.add('active');
            state.taskFilter = btn.dataset.filter;
            renderTasks();
        });
    });

    // Add task modal
    const addBtn = document.getElementById('add-task-btn');
    const modal = document.getElementById('task-modal');
    const closeBtn = document.getElementById('task-modal-close');
    const cancelBtn = document.getElementById('task-cancel');
    const saveBtn = document.getElementById('task-save');

    addBtn.addEventListener('click', () => modal.style.display = 'flex');
    closeBtn.addEventListener('click', () => modal.style.display = 'none');
    cancelBtn.addEventListener('click', () => modal.style.display = 'none');
    modal.addEventListener('click', (e) => {
        if (e.target === modal) modal.style.display = 'none';
    });

    saveBtn.addEventListener('click', async () => {
        const title = document.getElementById('task-title').value.trim();
        if (!title) return;

        await api.post('/api/tasks', {
            title,
            description: document.getElementById('task-desc').value.trim(),
            priority: document.getElementById('task-priority').value,
        });

        document.getElementById('task-title').value = '';
        document.getElementById('task-desc').value = '';
        document.getElementById('task-priority').value = 'medium';
        modal.style.display = 'none';
        await loadTasks();
    });
}

async function loadTasks() {
    try {
        state.tasks = await api.get('/api/tasks');
        renderTasks();
    } catch (e) { }
}

function renderTasks() {
    const container = document.getElementById('tasks-list');
    let tasks = state.tasks;

    if (state.taskFilter !== 'all') {
        tasks = tasks.filter(t => t.status === state.taskFilter);
    }

    if (tasks.length === 0) {
        container.innerHTML = `
            <div class="empty-state">
                <div class="empty-icon">✅</div>
                <h3>No tasks ${state.taskFilter !== 'all' ? 'with this filter' : 'yet'}</h3>
                <p>Tasks are automatically extracted from your journal, or add them manually.</p>
            </div>
        `;
        return;
    }

    container.innerHTML = tasks.map(t => {
        const statusClass = t.status === 'done' ? 'done' : t.status === 'in_progress' ? 'in-progress' : '';
        const nextStatus = t.status === 'todo' ? 'in_progress' : t.status === 'in_progress' ? 'done' : 'todo';
        return `
            <div class="task-item ${t.status}" data-id="${t.id}">
                <button class="task-checkbox ${statusClass}" data-id="${t.id}" data-next="${nextStatus}" title="Toggle status"></button>
                <div class="task-info">
                    <div class="task-title">${escapeHtml(t.title)}</div>
                    ${t.description ? `<div class="task-meta">${escapeHtml(t.description)}</div>` : ''}
                    ${t.source ? `<div class="task-meta">Source: ${t.source}</div>` : ''}
                </div>
                <span class="task-priority ${t.priority}">${t.priority}</span>
                <button class="task-delete" data-id="${t.id}" title="Delete">✕</button>
            </div>
        `;
    }).join('');

    // Checkbox handlers
    container.querySelectorAll('.task-checkbox').forEach(btn => {
        btn.addEventListener('click', async () => {
            await api.put(`/api/tasks/${btn.dataset.id}`, { status: btn.dataset.next });
            await loadTasks();
        });
    });

    // Delete handlers
    container.querySelectorAll('.task-delete').forEach(btn => {
        btn.addEventListener('click', async () => {
            await api.del(`/api/tasks/${btn.dataset.id}`);
            await loadTasks();
        });
    });
}

// ── Settings ──────────────────────────────────────────────────
async function loadSettings() {
    try {
        state.settings = await api.get('/api/settings');

        // Populate values
        document.getElementById('setting-n-ctx').value = state.settings.n_ctx || '8192';
        document.getElementById('setting-temperature').value = state.settings.temperature || '0.7';
        document.getElementById('temp-value').textContent = state.settings.temperature || '0.7';
        document.getElementById('setting-max-tokens').value = state.settings.max_tokens || '2048';
        document.getElementById('setting-context-enabled').checked = state.settings.context_enabled !== 'false';
        document.getElementById('setting-context-interval').value = state.settings.context_interval || '30';

        // Gemini API key status
        const geminiStatus = document.getElementById('gemini-key-status');
        if (state.settings.gemini_api_key_set) {
            geminiStatus.innerHTML = '✅ API key configured (' + state.settings.gemini_api_key + ')';
            geminiStatus.style.color = 'var(--accent)';
        } else {
            geminiStatus.innerHTML = '⚠️ No API key set — image generation will not work';
            geminiStatus.style.color = 'var(--warning, #f59e0b)';
        }

        // Proactive toggle state
        try {
            const proactiveState = await api.get('/api/proactive/suggestions');
            document.getElementById('proactive-toggle').checked = proactiveState.enabled;
        } catch (e) { }

        // Load models
        await loadModels();

        // Load skills
        await loadSkills();

        // Attach handlers
        setupSettingHandlers();
    } catch (e) { }
}

function setupSettingHandlers() {
    // Temperature slider
    const tempSlider = document.getElementById('setting-temperature');
    tempSlider.oninput = () => {
        document.getElementById('temp-value').textContent = tempSlider.value;
    };
    tempSlider.onchange = () => {
        saveSetting('temperature', tempSlider.value);
    };

    // Max tokens
    document.getElementById('setting-max-tokens').onchange = (e) => {
        saveSetting('max_tokens', e.target.value);
    };

    // Context toggle
    document.getElementById('setting-context-enabled').onchange = (e) => {
        saveSetting('context_enabled', e.target.checked ? 'true' : 'false');
    };

    // Context interval
    document.getElementById('setting-context-interval').onchange = (e) => {
        saveSetting('context_interval', e.target.value);
    };

    // Context length
    document.getElementById('setting-n-ctx').onchange = (e) => {
        saveSetting('n_ctx', e.target.value);
    };

    // Proactive toggle
    document.getElementById('proactive-toggle').onchange = async () => {
        try {
            const result = await api.post('/api/proactive/toggle');
            document.getElementById('proactive-toggle').checked = result.enabled;
        } catch (e) { }
    };

    // Gemini API key save
    document.getElementById('save-gemini-key').onclick = async () => {
        const keyInput = document.getElementById('setting-gemini-key');
        const key = keyInput.value.trim();
        if (!key) return;
        try {
            await api.put('/api/settings', { key: 'gemini_api_key', value: key });
            state.settings.gemini_api_key_set = true;
            const status = document.getElementById('gemini-key-status');
            status.innerHTML = '✅ API key saved successfully!';
            status.style.color = 'var(--accent)';
            keyInput.value = '';
            keyInput.placeholder = 'Key saved — enter new key to update';
        } catch (e) {
            const status = document.getElementById('gemini-key-status');
            status.innerHTML = '❌ Failed to save API key';
            status.style.color = '#ef4444';
        }
    };

    // Gemini API key show/hide toggle
    document.getElementById('toggle-gemini-key').onclick = () => {
        const keyInput = document.getElementById('setting-gemini-key');
        const isPassword = keyInput.type === 'password';
        keyInput.type = isPassword ? 'text' : 'password';
        document.getElementById('toggle-gemini-key').textContent = isPassword ? '🙈' : '👁';
    };
}

async function saveSetting(key, value) {
    try {
        await api.put('/api/settings', { key, value });
        state.settings[key] = value;
    } catch (e) {
        console.error('Failed to save setting:', e);
    }
}

// ── Skills ────────────────────────────────────────────────────
async function loadSkills() {
    const grid = document.getElementById('skills-grid');
    if (!grid) return;
    try {
        const data = await api.get('/api/skills');
        renderSkills(data.skills || []);
    } catch (e) {
        grid.innerHTML = '<div style="padding: 12px; color: var(--text-tertiary); font-size: 0.82rem;">Could not load skills.</div>';
    }
}

function renderSkills(skills) {
    const grid = document.getElementById('skills-grid');
    if (!grid) return;
    if (skills.length === 0) {
        grid.innerHTML = '<div style="padding: 12px; color: var(--text-tertiary); font-size: 0.82rem;">No skills installed.</div>';
        return;
    }

    grid.innerHTML = skills.map(s => {
        const catClass = s.category === 'community' ? 'community' : 'builtin';
        const catLabel = s.category === 'community' ? 'Community' : 'Built-in';
        return `
            <div class="skill-card ${s.enabled ? '' : 'disabled'}">
                <div class="skill-card-header">
                    <span class="skill-icon">${s.icon || '🧩'}</span>
                    <span class="skill-badge ${catClass}">${catLabel}</span>
                    <label class="toggle skill-toggle">
                        <input type="checkbox" data-skill="${s.name}" ${s.enabled ? 'checked' : ''}>
                        <span class="toggle-slider"></span>
                    </label>
                </div>
                <div class="skill-card-body">
                    <strong>${escapeHtml(s.display_name)}</strong>
                    <span class="skill-desc">${escapeHtml(s.description)}</span>
                    <span class="skill-tools">${s.tool_count || 0} tool${s.tool_count !== 1 ? 's' : ''}</span>
                </div>
            </div>
        `;
    }).join('');

    // Toggle handlers
    grid.querySelectorAll('input[data-skill]').forEach(input => {
        input.addEventListener('change', async () => {
            const name = input.dataset.skill;
            const enabled = input.checked;
            try {
                await api.post(`/api/skills/${name}/toggle`, { enabled });
                // Re-render to update visual state
                await loadSkills();
            } catch (e) {
                input.checked = !enabled; // revert on error
                console.error('Skill toggle failed:', e);
            }
        });
    });
}

async function loadModels() {
    try {
        const data = await api.get('/api/models');
        const container = document.getElementById('models-list');
        const currentModel = document.getElementById('current-model');

        if (data.loaded) {
            const name = data.loaded.split('/').pop();
            currentModel.textContent = name;
        } else {
            currentModel.textContent = 'No model loaded';
        }

        if (data.models.length === 0) {
            container.innerHTML = `
                <div style="padding: 12px; font-size: 0.82rem; color: var(--text-tertiary);">
                    No GGUF models found. Place model files in the <code style="background: rgba(0,0,0,.3); padding: 2px 6px; border-radius: 4px; font-family: var(--font-mono);">models/</code> directory.
                </div>
            `;
            return;
        }

        container.innerHTML = data.models.map(m => {
            const isLoaded = data.loaded === m.path;
            return `
                <div class="model-item ${isLoaded ? 'loaded' : ''}">
                    <span class="model-name">${escapeHtml(m.name)}</span>
                    <span class="model-size">${m.size_gb} GB</span>
                    <button class="model-load-btn ${isLoaded ? 'loaded' : ''}"
                            data-path="${escapeHtml(m.path)}"
                            ${isLoaded ? 'disabled' : ''}>
                        ${isLoaded ? '✓ Loaded' : 'Load'}
                    </button>
                </div>
            `;
        }).join('');

        // Load button handlers
        container.querySelectorAll('.model-load-btn:not(.loaded)').forEach(btn => {
            btn.addEventListener('click', async () => {
                btn.textContent = 'Loading...';
                btn.disabled = true;

                try {
                    const nCtx = parseInt(document.getElementById('setting-n-ctx').value);
                    await (await api.post('/api/models/load', {
                        model_path: btn.dataset.path,
                        n_ctx: nCtx,
                    })).json();
                    await refreshStatus();
                    await loadModels();
                } catch (e) {
                    btn.textContent = 'Load';
                    btn.disabled = false;
                    alert(`Failed to load model: ${e.message}`);
                }
            });
        });
    } catch (e) {
        console.error('Failed to load models:', e);
    }
}

// ── Voice Input & TTS ─────────────────────────────────────────
function setupVoice() {
    const micBtn = document.getElementById('mic-btn');
    if (!micBtn) return;

    micBtn.addEventListener('click', toggleVoice);

    // Auto-start polling — voice listener runs in the background on the server
    state.voiceActive = true;
    micBtn.classList.add('active');
    state.voicePolling = setInterval(pollVoice, 1200);
}

async function toggleVoice() {
    const micBtn = document.getElementById('mic-btn');
    if (state.voiceActive) {
        // Manually stop the always-on listener
        try { await api.post('/api/voice/stop'); } catch (e) { /* ignore */ }
        state.voiceActive = false;
        micBtn.classList.remove('active');
        micBtn.classList.remove('listening');
        if (state.voicePolling) {
            clearInterval(state.voicePolling);
            state.voicePolling = null;
        }
    } else {
        // Re-enable the listener
        try {
            await api.post('/api/voice/start');
            state.voiceActive = true;
            micBtn.classList.add('active');
            state.voicePolling = setInterval(pollVoice, 1200);
        } catch (e) {
            console.error('Voice start failed:', e);
        }
    }
}

async function pollVoice() {
    try {
        const res = await api.get('/api/voice/status');
        // Handle transcriptions (strings from the server)
        if (res.transcriptions && res.transcriptions.length > 0) {
            const input = document.getElementById('chat-input');
            for (const text of res.transcriptions) {
                const trimmed = (typeof text === 'string' ? text : text.text || '').trim();
                if (trimmed) {
                    input.value = (input.value ? input.value + ' ' : '') + trimmed;
                    input.dispatchEvent(new Event('input'));
                }
            }
            // Auto-send after voice transcription
            const sendBtn = document.getElementById('send-btn');
            if (sendBtn && document.getElementById('chat-input').value.trim()) {
                sendBtn.click();
            }
        }
        // Update mic button visual state based on detailed status
        const micBtn = document.getElementById('mic-btn');
        if (micBtn) {
            // Remove all voice states first
            micBtn.classList.remove('listening', 'wake-detected', 'recording', 'transcribing');

            if (res.status === 'wake_word_detected') {
                micBtn.classList.add('wake-detected');
                showVoiceToast('🎤 Hey Libre! Listening...');
            } else if (res.status === 'recording') {
                micBtn.classList.add('recording');
            } else if (res.status === 'transcribing') {
                micBtn.classList.add('transcribing');
            } else if (res.listening) {
                micBtn.classList.add('listening');
            }

            // Show running state
            if (res.running) {
                micBtn.classList.add('active');
            }
        }
    } catch (e) {
        // Server might be down, ignore
        console.warn('Voice poll error:', e);
    }
}

function showVoiceToast(message) {
    // Remove any existing voice toast
    const existing = document.querySelector('.voice-toast');
    if (existing) existing.remove();

    const toast = document.createElement('div');
    toast.className = 'voice-toast';
    toast.textContent = message;
    document.body.appendChild(toast);
    // Trigger animation
    requestAnimationFrame(() => toast.classList.add('show'));
    setTimeout(() => {
        toast.classList.remove('show');
        setTimeout(() => toast.remove(), 300);
    }, 2500);
}

async function speakText(text) {
    try {
        await api.post('/api/tts/speak', { text });
    } catch (e) {
        console.warn('TTS failed:', e);
    }
}

async function stopSpeaking() {
    try {
        await api.post('/api/tts/stop');
    } catch (e) { /* ignore */ }
}

// ── Slash Command Hints ───────────────────────────────────────
function handleSlashHints(value) {
    const hintsEl = document.getElementById('slash-hints');
    if (!hintsEl) return;

    if (value.startsWith('/') && value.length < 20) {
        const query = value.toLowerCase();
        const matches = SLASH_COMMANDS.filter(c => c.cmd.startsWith(query));
        if (matches.length > 0 && value !== '/help') {
            hintsEl.style.display = 'flex';
            hintsEl.innerHTML = matches.map(c => `
                <button class="slash-hint" data-cmd="${c.cmd}">
                    <span class="slash-hint-icon">${c.icon}</span>
                    <span class="slash-hint-cmd">${c.cmd}</span>
                    <span class="slash-hint-desc">${c.desc}</span>
                </button>
            `).join('');

            hintsEl.querySelectorAll('.slash-hint').forEach(btn => {
                btn.addEventListener('click', () => {
                    const input = document.getElementById('chat-input');
                    input.value = btn.dataset.cmd + ' ';
                    input.focus();
                    hintsEl.style.display = 'none';
                });
            });
            return;
        }
    }
    hintsEl.style.display = 'none';
}

// ── File Drop ─────────────────────────────────────────────────
function setupFileDrop() {
    const chatContainer = document.querySelector('.chat-container');
    const overlay = document.getElementById('file-drop-overlay');
    if (!chatContainer || !overlay) return;

    const TEXT_EXTENSIONS = ['.txt', '.md', '.csv', '.json', '.xml', '.yaml', '.yml',
        '.toml', '.env', '.log', '.py', '.js', '.ts', '.jsx', '.tsx',
        '.html', '.css', '.scss', '.sql', '.sh', '.bash', '.zsh',
        '.swift', '.kt', '.java', '.c', '.cpp', '.h', '.rs', '.go',
        '.rb', '.php', '.r', '.lua', '.conf', '.cfg', '.ini'];

    // Shared file processing logic
    async function handleFileAttach(file) {
        const ext = '.' + file.name.split('.').pop().toLowerCase();

        if (!TEXT_EXTENSIONS.includes(ext)) {
            appendMessage('assistant', `⚠️ **Unsupported file type** (${ext}). I can read text-based files like .txt, .py, .js, .json, .csv, .md, etc.`);
            return;
        }

        try {
            let content = await file.text();
            const MAX_CHARS = 12000; // ~3000 tokens
            if (content.length > MAX_CHARS) {
                content = content.substring(0, MAX_CHARS) + '\n\n[... truncated — file too large ...]';
            }
            state.attachedFile = { name: file.name, content, ext };
            // Show attachment pill
            document.getElementById('file-attachment').style.display = 'flex';
            document.getElementById('file-attachment-name').textContent = `📎 ${file.name}`;
            // Highlight upload button
            const uploadBtn = document.getElementById('upload-btn');
            if (uploadBtn) uploadBtn.classList.add('has-file');
        } catch (err) {
            appendMessage('assistant', `❌ Could not read file: ${err.message}`);
        }
    }

    // ── Upload button click ──────────────────────────────────
    const uploadBtn = document.getElementById('upload-btn');
    const fileInput = document.getElementById('file-input');
    if (uploadBtn && fileInput) {
        uploadBtn.addEventListener('click', () => fileInput.click());
        fileInput.addEventListener('change', async () => {
            if (fileInput.files.length > 0) {
                await handleFileAttach(fileInput.files[0]);
                fileInput.value = ''; // Reset so same file can be re-selected
            }
        });
    }

    // ── Drag and drop ────────────────────────────────────────
    let dragCounter = 0;

    chatContainer.addEventListener('dragenter', (e) => {
        e.preventDefault();
        dragCounter++;
        overlay.classList.add('visible');
    });

    chatContainer.addEventListener('dragleave', (e) => {
        e.preventDefault();
        dragCounter--;
        if (dragCounter <= 0) {
            dragCounter = 0;
            overlay.classList.remove('visible');
        }
    });

    chatContainer.addEventListener('dragover', (e) => {
        e.preventDefault();
    });

    chatContainer.addEventListener('drop', async (e) => {
        e.preventDefault();
        dragCounter = 0;
        overlay.classList.remove('visible');

        const files = e.dataTransfer.files;
        if (files.length === 0) return;
        await handleFileAttach(files[0]);
    });
}

// ── Smart Suggestions ─────────────────────────────────────────
function showSuggestions(aiResponse, onSelect) {
    const bar = document.getElementById('suggestions-bar');
    if (!bar) return;

    // Generate contextual follow-ups based on the AI's response
    const suggestions = generateSuggestions(aiResponse);
    if (suggestions.length === 0) {
        bar.innerHTML = '';
        return;
    }

    bar.innerHTML = suggestions.map(s => `
        <button class="suggestion-chip">${escapeHtml(s)}</button>
    `).join('');

    bar.querySelectorAll('.suggestion-chip').forEach((chip, i) => {
        chip.addEventListener('click', () => {
            bar.innerHTML = '';
            onSelect(suggestions[i]);
        });
    });
}

function generateSuggestions(response) {
    const suggestions = [];
    const lower = response.toLowerCase();

    // Context-aware suggestions
    if (lower.includes('code') || lower.includes('function') || lower.includes('```')) {
        suggestions.push('Explain this code step by step');
        suggestions.push('Can you optimize this?');
    }
    if (lower.includes('error') || lower.includes('bug') || lower.includes('fix')) {
        suggestions.push('What caused this error?');
        suggestions.push('How can I prevent this?');
    }
    if (lower.includes('list') || lower.includes('steps') || lower.includes('1.')) {
        suggestions.push('Tell me more about the first point');
        suggestions.push('Can you elaborate?');
    }
    if (lower.includes('example')) {
        suggestions.push('Show me another example');
    }

    // Always offer these if nothing else matched
    if (suggestions.length === 0) {
        suggestions.push('Tell me more');
        suggestions.push('Can you give an example?');
    }
    suggestions.push('Summarize this');

    return suggestions.slice(0, 3);
}

// ── Model Quick-Switch ────────────────────────────────────────
function setupModelPill() {
    const pill = document.getElementById('model-pill');
    if (!pill) return;

    pill.addEventListener('click', async () => {
        try {
            const data = await api.get('/api/models');
            if (!data.models || data.models.length === 0) {
                return;
            }
            // Cycle to the next model
            const currentIdx = data.models.findIndex(m => m.path === data.loaded);
            const nextIdx = (currentIdx + 1) % data.models.length;
            const next = data.models[nextIdx];

            if (next.path === data.loaded) return; // Only one model

            pill.querySelector('.model-pill-name').textContent = 'Loading...';
            pill.disabled = true;

            const nCtx = parseInt(state.settings.n_ctx || '8192');
            await (await api.post('/api/models/load', {
                model_path: next.path,
                n_ctx: nCtx,
            })).json();

            await refreshStatus();
            updateModelPill();
            pill.disabled = false;
        } catch (e) {
            console.error('Model switch failed:', e);
            pill.disabled = false;
            updateModelPill();
        }
    });

    // Initial update
    updateModelPill();
}

async function updateModelPill() {
    try {
        const data = await api.get('/api/models');
        const nameEl = document.getElementById('model-pill-name');
        const dotEl = document.querySelector('.model-pill-dot');
        if (data.loaded) {
            const name = data.loaded.split('/').pop().replace('.gguf', '');
            // Shorten for the pill
            nameEl.textContent = name.length > 20 ? name.substring(0, 18) + '…' : name;
            dotEl.classList.add('active');
        } else {
            nameEl.textContent = 'No model';
            dotEl.classList.remove('active');
        }
    } catch (e) { /* ignore */ }
}

// ── Utility ───────────────────────────────────────────────────
function escapeHtml(text) {
    if (!text) return '';
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
}

function formatDate(dateStr) {
    try {
        const d = new Date(dateStr + 'T00:00:00');
        return d.toLocaleDateString('en-US', {
            weekday: 'long',
            year: 'numeric',
            month: 'long',
            day: 'numeric',
        });
    } catch (e) {
        return dateStr;
    }
}
