/**
 * Libre Bird Platform — Frontend Application
 * WebSocket-first, JWT auth, multi-workspace
 */

// ── State ─────────────────────────────────────────────────────
const state = {
    currentView: 'chat',
    currentConversation: null,
    conversations: [],
    messages: [],
    tasks: [],
    taskFilter: 'all',
    isStreaming: false,
    settings: {},
    attachedFile: null,
    currentMode: 'general',
    autonomous: true,
    selectedProvider: 'local',
    selectedCloudModel: null,
    user: null,
    token: null,
    ws: null,
    wsReconnectDelay: 1000,
    currentWorkspace: null,
    workspaces: [],
};

// ── Constants ─────────────────────────────────────────────────
const API_BASE = '';  // Same origin (proxied via Vite or served by Gateway)
const WS_URL = `${location.protocol === 'https:' ? 'wss' : 'ws'}://${location.host}/ws`;
const SLASH_COMMANDS = [
    { cmd: '/summarize', desc: 'Summarize the conversation', icon: '📝' },
    { cmd: '/explain', desc: 'Explain a concept', icon: '💡' },
    { cmd: '/translate', desc: 'Translate text', icon: '🌍' },
    { cmd: '/email', desc: 'Draft an email', icon: '✉️' },
    { cmd: '/code', desc: 'Write or fix code', icon: '💻' },
    { cmd: '/help', desc: 'Show available commands', icon: '❓' },
];

const TOOL_LABELS = {
    get_datetime: '🕐 Getting time…',
    web_search: '🔍 Searching the web…',
    calculator: '🧮 Calculating…',
    get_weather: '🌤️ Checking weather…',
    open_url: '🌐 Opening URL…',
    image_generate: '🎨 Generating image…',
    read_url: '📖 Reading webpage…',
    run_code: '⚙️ Running code…',
    shell_command: '💻 Running command…',
    knowledge_search: '🧠 Searching knowledge…',
    knowledge_add: '🧠 Saving to knowledge…',
    set_reminder: '⏰ Setting reminder…',
    file_operations: '📁 Working with files…',
    read_document: '📄 Reading document…',
    workflow: '🔄 Running workflow…',
};

// ── API Helpers ───────────────────────────────────────────────
function authHeaders() {
    const h = { 'Content-Type': 'application/json' };
    if (state.token) h['Authorization'] = `Bearer ${state.token}`;
    return h;
}

const api = {
    async get(url) {
        const res = await fetch(API_BASE + url, { headers: authHeaders() });
        if (res.status === 401) return handleAuthExpired();
        if (!res.ok) {
            const err = await res.json().catch(() => ({ detail: res.statusText }));
            throw new Error(err.detail || 'Request failed');
        }
        return res.json();
    },
    async post(url, body = {}) {
        const res = await fetch(API_BASE + url, {
            method: 'POST', headers: authHeaders(), body: JSON.stringify(body),
        });
        if (res.status === 401) return handleAuthExpired();
        if (!res.ok && !res.headers.get('content-type')?.includes('text/event-stream')) {
            const err = await res.json().catch(() => ({ detail: res.statusText }));
            throw new Error(err.detail || 'Request failed');
        }
        return res;
    },
    async put(url, body = {}) {
        const res = await fetch(API_BASE + url, {
            method: 'PUT', headers: authHeaders(), body: JSON.stringify(body),
        });
        if (res.status === 401) return handleAuthExpired();
        if (!res.ok) {
            const err = await res.json().catch(() => ({ detail: res.statusText }));
            throw new Error(err.detail || 'Request failed');
        }
        return res.json();
    },
    async del(url) {
        const res = await fetch(API_BASE + url, { method: 'DELETE', headers: authHeaders() });
        if (res.status === 401) return handleAuthExpired();
        if (!res.ok) throw new Error('Delete failed');
        return res.json();
    },
};

function handleAuthExpired() {
    state.token = null;
    state.user = null;
    localStorage.removeItem('lb_token');
    showAuth();
    throw new Error('Session expired');
}

// ── Auth ──────────────────────────────────────────────────────
function initAuth() {
    const token = localStorage.getItem('lb_token');
    if (token) {
        state.token = token;
        // Verify token is still valid
        api.get('/api/auth/me').then(user => {
            state.user = user;
            showApp();
        }).catch(() => {
            localStorage.removeItem('lb_token');
            showAuth();
        });
    } else {
        showAuth();
    }

    // Form toggles
    document.getElementById('show-register').onclick = () => {
        document.getElementById('login-form').classList.remove('active');
        document.getElementById('register-form').classList.add('active');
    };
    document.getElementById('show-login').onclick = () => {
        document.getElementById('register-form').classList.remove('active');
        document.getElementById('login-form').classList.add('active');
    };

    // Login
    document.getElementById('login-form').onsubmit = async (e) => {
        e.preventDefault();
        const errEl = document.getElementById('login-error');
        errEl.textContent = '';
        try {
            const res = await fetch(API_BASE + '/api/auth/login', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    email: document.getElementById('login-email').value,
                    password: document.getElementById('login-password').value,
                }),
            });
            if (!res.ok) {
                const data = await res.json().catch(() => ({}));
                throw new Error(data.detail || 'Invalid credentials');
            }
            const data = await res.json();
            state.token = data.token;
            state.user = data.user;
            localStorage.setItem('lb_token', data.token);
            showApp();
        } catch (err) {
            errEl.textContent = err.message;
        }
    };

    // Register
    document.getElementById('register-form').onsubmit = async (e) => {
        e.preventDefault();
        const errEl = document.getElementById('register-error');
        errEl.textContent = '';
        try {
            const res = await fetch(API_BASE + '/api/auth/register', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    name: document.getElementById('register-name').value,
                    email: document.getElementById('register-email').value,
                    password: document.getElementById('register-password').value,
                }),
            });
            if (!res.ok) {
                const data = await res.json().catch(() => ({}));
                throw new Error(data.detail || 'Registration failed');
            }
            const data = await res.json();
            state.token = data.token;
            state.user = data.user;
            localStorage.setItem('lb_token', data.token);
            showApp();
        } catch (err) {
            errEl.textContent = err.message;
        }
    };
}

function showAuth() {
    document.getElementById('auth-overlay').classList.remove('hidden');
    document.getElementById('app').classList.add('hidden');
}

function showApp() {
    document.getElementById('auth-overlay').classList.add('hidden');
    document.getElementById('app').classList.remove('hidden');

    // Set user info
    if (state.user) {
        document.getElementById('user-name').textContent = state.user.name || state.user.email;
        document.getElementById('user-avatar').textContent = (state.user.name || state.user.email)[0].toUpperCase();
    }

    connectWebSocket();
    loadWorkspaces();
    loadConversations();
    loadSettings();
}

// ── WebSocket ─────────────────────────────────────────────────
function connectWebSocket() {
    if (state.ws && state.ws.readyState === WebSocket.OPEN) return;

    const url = `${WS_URL}?token=${encodeURIComponent(state.token)}`;
    const ws = new WebSocket(url);
    state.ws = ws;

    ws.onopen = () => {
        state.wsReconnectDelay = 1000;
        updateStatus('online', 'Connected');
    };

    ws.onclose = () => {
        updateStatus('offline', 'Disconnected');
        // Reconnect with exponential backoff
        setTimeout(() => {
            if (state.token) connectWebSocket();
        }, state.wsReconnectDelay);
        state.wsReconnectDelay = Math.min(state.wsReconnectDelay * 2, 30000);
    };

    ws.onerror = () => {
        updateStatus('offline', 'Connection error');
    };

    ws.onmessage = (event) => {
        try {
            const msg = JSON.parse(event.data);
            handleWsMessage(msg);
        } catch (e) {
            console.error('WS parse error:', e);
        }
    };
}

function handleWsMessage(msg) {
    switch (msg.type) {
        case 'token':
            handleStreamToken(msg);
            break;
        case 'thinking':
            handleStreamThinking(msg);
            break;
        case 'thinking_done':
            handleStreamThinkingDone();
            break;
        case 'tool':
            handleStreamTool(msg);
            break;
        case 'progress':
            handleStreamProgress(msg);
            break;
        case 'done':
            handleStreamDone(msg);
            break;
        case 'error':
            handleStreamError(msg);
            break;
        case 'meta':
            if (msg.conversation_id) {
                state.currentConversation = msg.conversation_id;
            }
            break;
    }
}

function updateStatus(type, text) {
    const dot = document.getElementById('status-dot');
    const statusText = document.getElementById('status-text');
    dot.className = `status-dot ${type}`;
    statusText.textContent = text;
}

// ── Navigation ────────────────────────────────────────────────
function setupNavigation() {
    document.querySelectorAll('.nav-item').forEach(item => {
        item.addEventListener('click', () => {
            const view = item.dataset.view;
            switchView(view);
        });
    });
}

function switchView(viewName) {
    state.currentView = viewName;
    document.querySelectorAll('.nav-item').forEach(n => n.classList.toggle('active', n.dataset.view === viewName));
    document.querySelectorAll('.view').forEach(v => v.classList.toggle('active', v.id === `view-${viewName}`));

    if (viewName === 'journal') loadJournals();
    if (viewName === 'tasks') loadTasks();
    if (viewName === 'settings') loadSettings();
}

// ── Workspace ─────────────────────────────────────────────────
async function loadWorkspaces() {
    try {
        const data = await api.get('/api/workspaces');
        state.workspaces = data.workspaces || data || [];
        if (!state.currentWorkspace && state.workspaces.length > 0) {
            state.currentWorkspace = state.workspaces[0];
        }
        renderWorkspaces();
    } catch (e) {
        // Default workspace
        state.workspaces = [{ id: 1, name: 'Personal', role: 'owner' }];
        state.currentWorkspace = state.workspaces[0];
        renderWorkspaces();
    }
}

function renderWorkspaces() {
    const nameEl = document.getElementById('workspace-name');
    const roleEl = document.getElementById('workspace-role');
    const avatarEl = document.getElementById('workspace-avatar');
    const listEl = document.getElementById('workspace-list');

    if (state.currentWorkspace) {
        nameEl.textContent = state.currentWorkspace.name;
        roleEl.textContent = state.currentWorkspace.role || 'member';
        avatarEl.textContent = state.currentWorkspace.name[0].toUpperCase();
    }

    listEl.innerHTML = state.workspaces.map(w => `
        <button class="workspace-dropdown-item ${w.id === state.currentWorkspace?.id ? 'active' : ''}" data-ws-id="${w.id}">
            <div class="workspace-avatar">${w.name[0].toUpperCase()}</div>
            <span>${escapeHtml(w.name)}</span>
        </button>
    `).join('');

    listEl.querySelectorAll('.workspace-dropdown-item').forEach(btn => {
        btn.addEventListener('click', () => {
            const wsId = parseInt(btn.dataset.wsId);
            state.currentWorkspace = state.workspaces.find(w => w.id === wsId);
            renderWorkspaces();
            document.getElementById('workspace-dropdown').classList.remove('open');
            document.getElementById('workspace-btn').classList.remove('open');
            loadConversations();
        });
    });
}

function setupWorkspace() {
    const btn = document.getElementById('workspace-btn');
    const dropdown = document.getElementById('workspace-dropdown');

    btn.addEventListener('click', () => {
        const isOpen = dropdown.classList.toggle('open');
        btn.classList.toggle('open', isOpen);
    });

    document.addEventListener('click', (e) => {
        if (!e.target.closest('.workspace-switcher')) {
            dropdown.classList.remove('open');
            btn.classList.remove('open');
        }
    });
}

// ── Chat ──────────────────────────────────────────────────────
// Streaming state for the active response
let streamState = { thinkingEl: null, thinkingContent: null, thinkingText: '', toolIndicator: null, bubble: null, fullResponse: '' };

function setupChat() {
    const input = document.getElementById('chat-input');
    const sendBtn = document.getElementById('send-btn');

    // Auto-resize textarea
    input.addEventListener('input', () => {
        input.style.height = 'auto';
        input.style.height = Math.min(input.scrollHeight, 200) + 'px';
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

    // Suggestion chips
    document.querySelectorAll('.suggestion-chip').forEach(chip => {
        chip.addEventListener('click', () => {
            input.value = chip.dataset.prompt;
            sendMessage();
        });
    });

    // File drag & drop
    const chatMessages = document.getElementById('chat-messages');
    chatMessages.addEventListener('dragover', (e) => { e.preventDefault(); chatMessages.classList.add('drag-over'); });
    chatMessages.addEventListener('dragleave', () => chatMessages.classList.remove('drag-over'));
    chatMessages.addEventListener('drop', handleFileDrop);

    // Remove file
    document.getElementById('remove-file-btn')?.addEventListener('click', () => {
        state.attachedFile = null;
        document.getElementById('file-attachment').style.display = 'none';
    });

    // Provider selector
    const providerSelect = document.getElementById('provider-select');
    providerSelect.addEventListener('change', () => {
        state.selectedProvider = providerSelect.value;
        document.getElementById('model-tag').textContent = providerSelect.options[providerSelect.selectedIndex].text;
    });

    // Mode selector
    document.getElementById('mode-select').addEventListener('change', (e) => {
        state.currentMode = e.target.value;
    });

    // Auto toggle
    document.getElementById('auto-toggle').addEventListener('change', (e) => {
        state.autonomous = e.target.checked;
    });

    // Conversation search
    document.getElementById('conv-search').addEventListener('input', (e) => {
        renderConversations(e.target.value.toLowerCase());
    });

    // New chat button
    document.getElementById('new-chat-btn').addEventListener('click', startNewConversation);
}

function handleSlashHints(value) {
    const hintsEl = document.getElementById('slash-hints');
    if (value.startsWith('/') && value.length < 15) {
        const q = value.toLowerCase();
        const matches = SLASH_COMMANDS.filter(c => c.cmd.startsWith(q));
        if (matches.length > 0 && value !== '/help') {
            hintsEl.innerHTML = matches.map(c =>
                `<div class="slash-hint-item" data-cmd="${c.cmd}">
                    <span class="slash-hint-cmd">${c.icon} ${c.cmd}</span>
                    <span class="slash-hint-desc">${c.desc}</span>
                </div>`
            ).join('');
            hintsEl.style.display = 'block';
            hintsEl.querySelectorAll('.slash-hint-item').forEach(item => {
                item.addEventListener('click', () => {
                    document.getElementById('chat-input').value = item.dataset.cmd + ' ';
                    hintsEl.style.display = 'none';
                    document.getElementById('chat-input').focus();
                });
            });
            return;
        }
    }
    hintsEl.style.display = 'none';
}

function handleFileDrop(e) {
    e.preventDefault();
    document.getElementById('chat-messages').classList.remove('drag-over');
    const file = e.dataTransfer.files[0];
    if (!file) return;
    const reader = new FileReader();
    reader.onload = () => {
        state.attachedFile = {
            name: file.name,
            ext: file.name.substring(file.name.lastIndexOf('.')),
            content: reader.result,
        };
        document.getElementById('attached-file-name').textContent = file.name;
        document.getElementById('file-attachment').style.display = 'flex';
    };
    reader.readAsText(file);
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
        appendMessage('assistant', `## Available Commands\n\n${helpText}`);
        document.getElementById('slash-hints').style.display = 'none';
        return;
    }

    // Attach file content if present
    if (state.attachedFile) {
        const fileCtx = `[Attached file: ${state.attachedFile.name}]\n\`\`\`${state.attachedFile.ext.replace('.', '')}\n${state.attachedFile.content}\n\`\`\`\n\n`;
        message = fileCtx + (message || `Analyze this ${state.attachedFile.ext} file.`);
        state.attachedFile = null;
        document.getElementById('file-attachment').style.display = 'none';
    }

    // Clear UI
    document.getElementById('slash-hints').style.display = 'none';
    document.getElementById('suggestions-bar').innerHTML = '';
    const welcome = document.querySelector('.welcome-message');
    if (welcome) welcome.remove();

    // Add user message
    appendMessage('user', input.value.trim() || message.substring(0, 100));
    input.value = '';
    input.style.height = 'auto';

    // Show typing indicator
    const typingEl = appendTyping();

    // Mark streaming
    state.isStreaming = true;
    document.getElementById('send-btn').disabled = true;

    // Reset stream state
    streamState = { thinkingEl: null, thinkingContent: null, thinkingText: '', toolIndicator: null, bubble: null, fullResponse: '' };

    try {
        // Send via WebSocket if connected, otherwise fall back to REST
        if (state.ws && state.ws.readyState === WebSocket.OPEN) {
            // Create assistant bubble immediately
            typingEl.remove();
            const assistantEl = appendMessage('assistant', '');
            streamState.bubble = assistantEl.querySelector('.message-bubble');

            state.ws.send(JSON.stringify({
                type: 'chat',
                message,
                conversation_id: state.currentConversation,
                provider: state.selectedProvider,
                model: state.selectedCloudModel,
                temperature: parseFloat(state.settings.temperature || 0.7),
                max_tokens: parseInt(state.settings.max_tokens || 2048),
                mode: state.currentMode,
                autonomous: state.autonomous,
                workspace_id: state.currentWorkspace?.id,
            }));
        } else {
            // Fallback: REST + SSE
            const res = await api.post('/api/chat', {
                message,
                conversation_id: state.currentConversation,
                provider: state.selectedProvider,
                model: state.selectedCloudModel,
                temperature: parseFloat(state.settings.temperature || 0.7),
                max_tokens: parseInt(state.settings.max_tokens || 2048),
                mode: state.currentMode,
                autonomous: state.autonomous,
                workspace_id: state.currentWorkspace?.id,
            });

            typingEl.remove();
            const assistantEl = appendMessage('assistant', '');
            streamState.bubble = assistantEl.querySelector('.message-bubble');

            // Read SSE stream
            await readSSEStream(res);
        }
    } catch (e) {
        typingEl?.remove();
        appendMessage('assistant', `❌ Error: ${e.message}`);
        finishStreaming();
    }
}

// SSE fallback reader
async function readSSEStream(res) {
    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';
    let currentEvent = 'message';

    while (true) {
        const { value, done } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
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
                    handleWsMessage({ ...parsed, type: currentEvent });
                } catch (e) { /* skip non-JSON */ }
                currentEvent = 'message';
            }
        }
    }
    finishStreaming();
}

// ── Stream Handlers ───────────────────────────────────────────
function handleStreamToken(msg) {
    if (!streamState.bubble) return;
    // Remove tool indicator when answer starts
    if (streamState.toolIndicator) {
        if (streamState.toolIndicator._timer) clearInterval(streamState.toolIndicator._timer);
        streamState.toolIndicator.remove();
        streamState.toolIndicator = null;
    }
    if (msg.token) {
        streamState.fullResponse += msg.token;
        let answerEl = streamState.bubble.querySelector('.answer-content');
        if (!answerEl) {
            answerEl = document.createElement('div');
            answerEl.className = 'answer-content';
            streamState.bubble.appendChild(answerEl);
        }
        answerEl.innerHTML = formatMarkdown(streamState.fullResponse);
        scrollToBottom();
    }
}

function handleStreamThinking(msg) {
    if (!streamState.bubble) return;
    if (!streamState.thinkingEl) {
        streamState.thinkingEl = document.createElement('details');
        streamState.thinkingEl.className = 'thinking-block';
        streamState.thinkingEl.open = true;
        streamState.thinkingEl.innerHTML = `<summary>🧠 Thinking…</summary><div class="thinking-content"></div>`;
        streamState.bubble.appendChild(streamState.thinkingEl);
        streamState.thinkingContent = streamState.thinkingEl.querySelector('.thinking-content');
    }
    if (msg.token) {
        streamState.thinkingText += msg.token;
        streamState.thinkingContent.innerHTML = formatMarkdown(streamState.thinkingText);
        scrollToBottom();
    }
}

function handleStreamThinkingDone() {
    if (streamState.thinkingEl) {
        streamState.thinkingEl.open = false;
        streamState.thinkingEl.querySelector('summary').textContent = '🧠 Thought process';
    }
}

function handleStreamTool(msg) {
    if (!streamState.bubble) return;
    const toolName = msg.name || msg.tool || 'tool';

    if (!streamState.toolIndicator) {
        streamState.toolIndicator = document.createElement('div');
        streamState.toolIndicator.className = 'tool-indicator';
        streamState.toolIndicator._startTime = Date.now();

        const spinner = document.createElement('div');
        spinner.className = 'tool-indicator-spinner';
        streamState.toolIndicator.appendChild(spinner);

        const content = document.createElement('div');
        content.className = 'tool-indicator-content';

        const label = document.createElement('div');
        label.className = 'tool-indicator-label';
        content.appendChild(label);

        const stepBadge = document.createElement('div');
        stepBadge.className = 'tool-indicator-step';
        content.appendChild(stepBadge);

        const elapsed = document.createElement('div');
        elapsed.className = 'tool-indicator-elapsed';
        elapsed.textContent = '0s elapsed';
        content.appendChild(elapsed);

        streamState.toolIndicator.appendChild(content);
        streamState.bubble.appendChild(streamState.toolIndicator);

        streamState.toolIndicator._timer = setInterval(() => {
            const secs = Math.floor((Date.now() - streamState.toolIndicator._startTime) / 1000);
            elapsed.textContent = secs < 60 ? `${secs}s elapsed` : `${Math.floor(secs / 60)}m ${(secs % 60).toString().padStart(2, '0')}s elapsed`;
        }, 1000);
    }

    const labelEl = streamState.toolIndicator.querySelector('.tool-indicator-label');
    if (labelEl) labelEl.textContent = TOOL_LABELS[toolName] || `🔧 Using ${toolName}…`;

    const stepEl = streamState.toolIndicator.querySelector('.tool-indicator-step');
    if (stepEl && msg.step > 0) {
        stepEl.textContent = `Step ${msg.step}`;
        stepEl.style.display = 'inline-block';
    }
    scrollToBottom();
}

function handleStreamProgress(msg) {
    if (!streamState.bubble) return;
    let progressEl = streamState.bubble.querySelector('.tool-progress');
    if (!progressEl) {
        progressEl = document.createElement('div');
        progressEl.className = 'tool-progress';
        streamState.bubble.appendChild(progressEl);
    }
    progressEl.innerHTML = `
        <span class="tool-progress-step">Step ${msg.round || 0}/${msg.total || 10}</span>
        <span class="tool-progress-name">${msg.tool || ''}</span>
    `;
    scrollToBottom();
}

function handleStreamDone(msg) {
    if (msg.conversation_id) {
        state.currentConversation = msg.conversation_id;
        loadConversations();
    }
    finishStreaming();

    // Generate follow-up suggestions
    if (streamState.fullResponse) {
        showSuggestions(streamState.fullResponse);
    }
}

function handleStreamError(msg) {
    if (streamState.bubble) {
        let answerEl = streamState.bubble.querySelector('.answer-content');
        if (!answerEl) {
            answerEl = document.createElement('div');
            answerEl.className = 'answer-content';
            streamState.bubble.appendChild(answerEl);
        }
        answerEl.textContent = `❌ ${msg.error || 'An error occurred'}`;
    }
    finishStreaming();
}

function finishStreaming() {
    state.isStreaming = false;
    document.getElementById('send-btn').disabled = false;
    if (streamState.toolIndicator?._timer) {
        clearInterval(streamState.toolIndicator._timer);
    }
}

// ── Message Rendering ─────────────────────────────────────────
function appendMessage(role, content) {
    const container = document.getElementById('chat-messages');
    const div = document.createElement('div');
    div.className = `message ${role}`;

    const avatar = document.createElement('div');
    avatar.className = 'message-avatar';
    avatar.textContent = role === 'user' ? (state.user?.name?.[0] || 'U') : '🕊️';

    const bubble = document.createElement('div');
    bubble.className = 'message-bubble';
    if (content) {
        bubble.innerHTML = role === 'user' ? escapeHtml(content) : formatMarkdown(content);
    }

    div.appendChild(avatar);
    div.appendChild(bubble);
    container.appendChild(div);
    scrollToBottom();
    return div;
}

function appendTyping() {
    const container = document.getElementById('chat-messages');
    const div = document.createElement('div');
    div.className = 'message assistant typing';
    div.innerHTML = `
        <div class="message-avatar">🕊️</div>
        <div class="message-bubble">
            <div class="typing-indicator">
                <div class="typing-dot"></div>
                <div class="typing-dot"></div>
                <div class="typing-dot"></div>
            </div>
        </div>
    `;
    container.appendChild(div);
    scrollToBottom();
    return div;
}

function scrollToBottom() {
    const el = document.getElementById('chat-messages');
    el.scrollTop = el.scrollHeight;
}

// ── Conversations ─────────────────────────────────────────────
async function loadConversations() {
    try {
        const data = await api.get('/api/conversations');
        state.conversations = data.conversations || data || [];
        renderConversations();
    } catch (e) { /* silent */ }
}

function renderConversations(searchQuery = '') {
    const container = document.getElementById('conversations-items');
    let convs = state.conversations;
    if (searchQuery) {
        convs = convs.filter(c => (c.title || '').toLowerCase().includes(searchQuery));
    }

    if (convs.length === 0) {
        container.innerHTML = `<div style="padding: 16px; font-size: 0.8rem; color: var(--text-tertiary); text-align: center;">${searchQuery ? 'No matching conversations' : 'No conversations yet'}</div>`;
        return;
    }

    container.innerHTML = convs.map(c => `
        <div class="conv-item ${c.id === state.currentConversation ? 'active' : ''}" data-id="${c.id}">
            <span class="conv-item-title">${escapeHtml(c.title || 'New Chat')}</span>
            <button class="conv-item-delete" data-id="${c.id}" title="Delete">
                <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                    <polyline points="3 6 5 6 21 6"></polyline>
                    <path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"></path>
                </svg>
            </button>
        </div>
    `).join('');

    container.querySelectorAll('.conv-item').forEach(el => {
        el.addEventListener('click', (e) => {
            if (e.target.closest('.conv-item-delete')) return;
            loadConversation(el.dataset.id);
        });
    });

    container.querySelectorAll('.conv-item-delete').forEach(btn => {
        btn.addEventListener('click', async (e) => {
            e.stopPropagation();
            const id = btn.dataset.id;
            await api.del(`/api/conversations/${id}`);
            if (state.currentConversation == id) startNewConversation();
            await loadConversations();
        });
    });
}

async function loadConversation(id) {
    state.currentConversation = id;
    renderConversations();

    try {
        const data = await api.get(`/api/conversations/${id}/messages`);
        const messages = data.messages || data || [];
        const container = document.getElementById('chat-messages');
        container.innerHTML = '';

        if (messages.length === 0) {
            addWelcomeMessage();
        } else {
            messages.forEach(m => {
                if (m.role !== 'system') appendMessage(m.role, m.content);
            });
        }
    } catch (e) {
        console.error('Failed to load conversation:', e);
    }
}

function startNewConversation() {
    state.currentConversation = null;
    state.messages = [];
    renderConversations();

    const container = document.getElementById('chat-messages');
    container.innerHTML = '';
    addWelcomeMessage();
    document.getElementById('chat-input').focus();
}

function addWelcomeMessage() {
    const container = document.getElementById('chat-messages');
    const div = document.createElement('div');
    div.className = 'welcome-message';
    div.innerHTML = `
        <div class="welcome-icon">🕊️</div>
        <h1>What can I help with?</h1>
        <p>Ask me anything. Free, private, and yours.</p>
        <div class="welcome-suggestions">
            <button class="suggestion-chip" data-prompt="Summarize this article for me">📝 Summarize</button>
            <button class="suggestion-chip" data-prompt="Help me write some code">💻 Code</button>
            <button class="suggestion-chip" data-prompt="Draft a professional email">✉️ Email</button>
            <button class="suggestion-chip" data-prompt="Explain this concept simply">💡 Explain</button>
        </div>
    `;
    container.appendChild(div);
    div.querySelectorAll('.suggestion-chip').forEach(chip => {
        chip.addEventListener('click', () => {
            document.getElementById('chat-input').value = chip.dataset.prompt;
            sendMessage();
        });
    });
}

// ── Follow-up Suggestions ─────────────────────────────────────
function showSuggestions(response) {
    const bar = document.getElementById('suggestions-bar');
    // Simple heuristic suggestions based on response content
    const suggestions = [];
    if (response.length > 200) suggestions.push('Can you summarize that?');
    if (response.includes('```')) suggestions.push('Explain this code');
    if (response.includes('1.') || response.includes('- ')) suggestions.push('Tell me more about the first point');
    suggestions.push('Continue');

    bar.innerHTML = suggestions.slice(0, 3).map(s =>
        `<button class="suggestion-pill">${s}</button>`
    ).join('');

    bar.querySelectorAll('.suggestion-pill').forEach(pill => {
        pill.addEventListener('click', () => {
            document.getElementById('chat-input').value = pill.textContent;
            sendMessage();
        });
    });
}

// ── Journal ───────────────────────────────────────────────────
async function loadJournals() {
    const list = document.getElementById('journal-list');
    try {
        const data = await api.get('/api/journal');
        const entries = data.entries || data || [];
        if (entries.length === 0) {
            list.innerHTML = '<div style="padding: 20px; text-align: center; color: var(--text-tertiary);">No journal entries yet. Generate your first entry!</div>';
            return;
        }
        list.innerHTML = entries.map(e => `
            <div class="journal-entry" data-date="${e.date || e.id}">
                <div class="journal-date">${e.date || new Date(e.created_at).toLocaleDateString()}</div>
                <div class="journal-summary">${escapeHtml(e.summary || e.content?.substring(0, 120) || '')}</div>
            </div>
        `).join('');

        list.querySelectorAll('.journal-entry').forEach(el => {
            el.addEventListener('click', () => loadJournalDetail(el.dataset.date));
        });
    } catch (e) {
        list.innerHTML = '<div style="padding: 20px; text-align: center; color: var(--text-tertiary);">Could not load journal entries.</div>';
    }
}

async function loadJournalDetail(date) {
    const detail = document.getElementById('journal-detail');
    const list = document.getElementById('journal-list');
    try {
        const data = await api.get(`/api/journal/${date}`);
        detail.innerHTML = `
            <button class="btn-sm" onclick="document.getElementById('journal-detail').style.display='none'; document.getElementById('journal-list').style.display='flex';">← Back</button>
            <div style="margin-top: 16px;">${formatMarkdown(data.content || data.text || '')}</div>
        `;
        detail.style.display = 'block';
        list.style.display = 'none';
    } catch (e) {
        console.error('Failed to load journal detail:', e);
    }
}

function setupJournal() {
    document.getElementById('generate-journal-btn').addEventListener('click', async () => {
        const btn = document.getElementById('generate-journal-btn');
        btn.disabled = true;
        btn.innerHTML = '<div class="tool-indicator-spinner" style="width:14px;height:14px;border-width:2px;"></div> Generating…';
        try {
            await api.post('/api/journal/generate');
            await loadJournals();
        } catch (e) {
            console.error('Journal generation failed:', e);
        }
        btn.disabled = false;
        btn.innerHTML = '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 2v4M12 18v4M4.93 4.93l2.83 2.83M16.24 16.24l2.83 2.83M2 12h4M18 12h4M4.93 19.07l2.83-2.83M16.24 7.76l2.83-2.83"/></svg> Generate Today\'s Entry';
    });
}

// ── Tasks ─────────────────────────────────────────────────────
function setupTasks() {
    document.querySelectorAll('.filter-btn').forEach(btn => {
        btn.addEventListener('click', () => {
            document.querySelectorAll('.filter-btn').forEach(b => b.classList.remove('active'));
            btn.classList.add('active');
            state.taskFilter = btn.dataset.filter;
            renderTasks();
        });
    });
}

async function loadTasks() {
    try {
        const data = await api.get('/api/tasks');
        state.tasks = data.tasks || data || [];
        renderTasks();
    } catch (e) {
        document.getElementById('tasks-list').innerHTML = '<div style="padding: 20px; text-align: center; color: var(--text-tertiary);">Could not load tasks.</div>';
    }
}

function renderTasks() {
    const container = document.getElementById('tasks-list');
    let tasks = state.tasks;
    if (state.taskFilter !== 'all') {
        tasks = tasks.filter(t => t.status === state.taskFilter);
    }

    if (tasks.length === 0) {
        container.innerHTML = '<div style="padding: 20px; text-align: center; color: var(--text-tertiary);">No tasks to show.</div>';
        return;
    }

    container.innerHTML = tasks.map(t => `
        <div class="task-item ${t.status === 'done' ? 'done' : ''}" data-id="${t.id}">
            <div class="task-checkbox ${t.status === 'done' ? 'done' : ''}" data-id="${t.id}"></div>
            <div class="task-info">
                <div class="task-title">${escapeHtml(t.title || t.description)}</div>
                <div class="task-meta">${t.source || ''} ${t.due_date ? '· Due ' + t.due_date : ''}</div>
            </div>
            ${t.priority ? `<span class="task-priority ${t.priority}">${t.priority}</span>` : ''}
        </div>
    `).join('');

    container.querySelectorAll('.task-checkbox').forEach(cb => {
        cb.addEventListener('click', async () => {
            const id = cb.dataset.id;
            const task = state.tasks.find(t => String(t.id) === id);
            if (!task) return;
            const newStatus = task.status === 'done' ? 'pending' : 'done';
            try {
                await api.put(`/api/tasks/${id}`, { status: newStatus });
                task.status = newStatus;
                renderTasks();
            } catch (e) { /* silent */ }
        });
    });
}

// ── Settings ──────────────────────────────────────────────────
async function loadSettings() {
    try {
        const data = await api.get('/api/settings');
        state.settings = data.settings || data || {};

        // Apply to UI
        const tempSlider = document.getElementById('setting-temperature');
        if (state.settings.temperature) {
            tempSlider.value = state.settings.temperature;
            document.getElementById('temp-value').textContent = state.settings.temperature;
        }
        if (state.settings.max_tokens) {
            document.getElementById('setting-max-tokens').value = state.settings.max_tokens;
        }

        // Load cloud providers into selector
        loadProviders();
    } catch (e) { /* silent, defaults are fine */ }
}

async function loadProviders() {
    try {
        const data = await api.get('/api/providers');
        const providers = data.providers || data || [];
        const select = document.getElementById('provider-select');

        // Keep "Local Model" as first option
        providers.forEach(p => {
            if (!select.querySelector(`option[value="${p.id}"]`)) {
                const opt = document.createElement('option');
                opt.value = p.id;
                opt.textContent = p.name;
                select.appendChild(opt);
            }
        });
    } catch (e) { /* silent */ }
}

function setupSettings() {
    // Temperature
    const tempSlider = document.getElementById('setting-temperature');
    tempSlider.oninput = () => document.getElementById('temp-value').textContent = tempSlider.value;
    tempSlider.onchange = () => saveSetting('temperature', tempSlider.value);

    // Max tokens
    document.getElementById('setting-max-tokens').onchange = (e) => saveSetting('max_tokens', e.target.value);

    // API key handlers
    setupApiKey('gemini');
    setupApiKey('openai');
    setupApiKey('anthropic');

    // Logout
    document.getElementById('logout-btn').addEventListener('click', () => {
        state.token = null;
        state.user = null;
        localStorage.removeItem('lb_token');
        if (state.ws) state.ws.close();
        showAuth();
    });

    // Export data
    document.getElementById('export-data-btn')?.addEventListener('click', async () => {
        try {
            const data = await api.get('/api/export');
            const blob = new Blob([JSON.stringify(data, null, 2)], { type: 'application/json' });
            const url = URL.createObjectURL(blob);
            const a = document.createElement('a');
            a.href = url; a.download = 'libre-bird-export.json'; a.click();
            URL.revokeObjectURL(url);
        } catch (e) { console.error('Export failed:', e); }
    });
}

function setupApiKey(provider) {
    const saveBtn = document.getElementById(`save-${provider}-key`);
    const toggleBtn = document.getElementById(`toggle-${provider}-key`);
    const input = document.getElementById(`setting-${provider}-key`);
    const status = document.getElementById(`${provider}-key-status`);

    if (saveBtn) {
        saveBtn.onclick = async () => {
            const key = input.value.trim();
            if (!key) return;
            try {
                await api.put('/api/settings', { key: `${provider}_api_key`, value: key });
                status.innerHTML = '✅ API key saved!';
                status.style.color = 'var(--accent)';
                input.value = '';
                input.placeholder = 'Key saved — enter new key to update';
                loadProviders();
            } catch (e) {
                status.innerHTML = '❌ Failed to save';
                status.style.color = 'var(--error)';
            }
        };
    }

    if (toggleBtn) {
        toggleBtn.onclick = () => {
            const isPassword = input.type === 'password';
            input.type = isPassword ? 'text' : 'password';
            toggleBtn.textContent = isPassword ? '🙈' : '👁';
        };
    }
}

async function saveSetting(key, value) {
    try {
        await api.put('/api/settings', { key, value });
        state.settings[key] = value;
    } catch (e) {
        console.error('Failed to save setting:', e);
    }
}

// ── Markdown Formatting ───────────────────────────────────────
function formatMarkdown(text) {
    if (!text) return '';
    const lines = text.split('\n');
    let html = '';
    let inCode = false;
    let codeLang = '';
    let codeContent = '';
    let inList = false;
    let listType = '';

    function closeList() {
        if (inList) { html += listType === 'ol' ? '</ol>' : '</ul>'; inList = false; }
    }

    for (const line of lines) {
        // Code blocks
        if (line.startsWith('```')) {
            if (inCode) {
                html += `<pre><code class="language-${codeLang}">${escapeHtml(codeContent)}</code></pre>`;
                inCode = false;
                codeContent = '';
            } else {
                closeList();
                inCode = true;
                codeLang = line.slice(3).trim() || 'text';
                codeContent = '';
            }
            continue;
        }
        if (inCode) {
            codeContent += (codeContent ? '\n' : '') + line;
            continue;
        }

        // Headers
        const headerMatch = line.match(/^(#{1,6})\s+(.+)/);
        if (headerMatch) {
            closeList();
            const level = headerMatch[1].length;
            html += `<h${level}>${inlineFormat(headerMatch[2])}</h${level}>`;
            continue;
        }

        // Horizontal rule
        if (/^---+$/.test(line.trim())) {
            closeList();
            html += '<hr>';
            continue;
        }

        // Blockquote
        if (line.startsWith('> ')) {
            closeList();
            html += `<blockquote>${inlineFormat(line.slice(2))}</blockquote>`;
            continue;
        }

        // Unordered list
        if (/^\s*[-*]\s+/.test(line)) {
            if (!inList || listType !== 'ul') { closeList(); html += '<ul>'; inList = true; listType = 'ul'; }
            html += `<li>${inlineFormat(line.replace(/^\s*[-*]\s+/, ''))}</li>`;
            continue;
        }

        // Ordered list
        if (/^\s*\d+\.\s+/.test(line)) {
            if (!inList || listType !== 'ol') { closeList(); html += '<ol>'; inList = true; listType = 'ol'; }
            html += `<li>${inlineFormat(line.replace(/^\s*\d+\.\s+/, ''))}</li>`;
            continue;
        }

        closeList();

        // Empty line
        if (!line.trim()) {
            html += '<br>';
            continue;
        }

        // Paragraph
        html += `<p>${inlineFormat(line)}</p>`;
    }

    // Close open blocks
    if (inCode) html += `<pre><code>${escapeHtml(codeContent)}</code></pre>`;
    closeList();

    return html;
}

function inlineFormat(text) {
    return text
        .replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>')
        .replace(/~~(.+?)~~/g, '<del>$1</del>')
        .replace(/\*(.+?)\*/g, '<em>$1</em>')
        .replace(/_(.+?)_/g, '<em>$1</em>')
        .replace(/`([^`]+)`/g, '<code>$1</code>')
        .replace(/\[([^\]]+)\]\(([^)]+)\)/g, '<a href="$2" target="_blank" rel="noopener">$1</a>');
}

function escapeHtml(text) {
    if (!text) return '';
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
}

// ── Init ──────────────────────────────────────────────────────
document.addEventListener('DOMContentLoaded', () => {
    setupNavigation();
    setupChat();
    setupWorkspace();
    setupTasks();
    setupJournal();
    setupSettings();
    initAuth();
});
