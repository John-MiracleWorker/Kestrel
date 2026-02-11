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
}

async function sendMessage() {
    const input = document.getElementById('chat-input');
    const message = input.value.trim();
    if (!message || state.isStreaming) return;

    // Clear welcome message
    const welcome = document.querySelector('.welcome-message');
    if (welcome) welcome.remove();

    // Add user message to UI
    appendMessage('user', message);
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
                            // Show tool indicator
                            const toolName = parsed.tool || 'tool';
                            const friendlyNames = {
                                get_datetime: '🕐 Getting time…',
                                web_search: '🔍 Searching the web…',
                                calculator: '🧮 Calculating…',
                                get_weather: '🌤️ Checking weather…',
                                open_url: '🌐 Opening URL…',
                                search_files: '📂 Searching files…',
                                get_system_info: '💻 Getting system info…',
                            };
                            if (!toolIndicator) {
                                toolIndicator = document.createElement('div');
                                toolIndicator.className = 'tool-indicator';
                                bubble.appendChild(toolIndicator);
                            }
                            toolIndicator.textContent = friendlyNames[toolName] || `🔧 Using ${toolName}…`;
                            scrollToBottom();
                        } else if (currentEvent === 'token') {
                            // Remove tool indicator when answer starts
                            if (toolIndicator) {
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
    // Basic markdown rendering
    let html = text
        // Escape HTML
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        // Code blocks
        .replace(/```(\w*)\n([\s\S]*?)```/g, '<pre><code>$2</code></pre>')
        // Inline code
        .replace(/`([^`]+)`/g, '<code>$1</code>')
        // Bold
        .replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>')
        // Italic
        .replace(/\*(.+?)\*/g, '<em>$1</em>')
        // Links
        .replace(/\[([^\]]+)\]\(([^)]+)\)/g, '<a href="$2" target="_blank" rel="noopener">$1</a>')
        // Line breaks
        .replace(/\n/g, '<br>');

    return html;
}

// ── Conversations ─────────────────────────────────────────────
async function loadConversations() {
    try {
        state.conversations = await api.get('/api/conversations');
        renderConversations();
    } catch (e) { }
}

function renderConversations() {
    const container = document.getElementById('conversations-items');
    if (state.conversations.length === 0) {
        container.innerHTML = '<div style="padding: 16px; font-size: 0.8rem; color: var(--text-tertiary); text-align: center;">No conversations yet</div>';
        return;
    }

    container.innerHTML = state.conversations.map(c => `
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

        // Load models
        await loadModels();

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
}

async function saveSetting(key, value) {
    try {
        await api.put('/api/settings', { key, value });
        state.settings[key] = value;
    } catch (e) {
        console.error('Failed to save setting:', e);
    }
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
