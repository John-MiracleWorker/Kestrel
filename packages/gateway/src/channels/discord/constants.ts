// ── Slash Command Definitions ──────────────────────────────────────

export const SLASH_COMMANDS = [
    {
        name: 'chat',
        description: 'Chat with Kestrel AI',
        options: [{
            name: 'message',
            description: 'Your message',
            type: 3, // STRING
            required: true,
        }],
    },
    {
        name: 'task',
        description: 'Launch an autonomous agent task',
        options: [{
            name: 'goal',
            description: 'What should the agent accomplish?',
            type: 3,
            required: true,
        }],
    },
    {
        name: 'tasks',
        description: 'List your active tasks',
    },
    {
        name: 'status',
        description: 'Show Kestrel system status',
    },
    {
        name: 'cancel',
        description: 'Cancel a running task',
        options: [{
            name: 'task_id',
            description: 'ID of the task to cancel',
            type: 3,
            required: true,
        }],
    },
    {
        name: 'model',
        description: 'Switch AI model',
        options: [{
            name: 'name',
            description: 'Model name (e.g. gpt-4o, claude-sonnet-4-20250514)',
            type: 3,
            required: true,
        }],
    },
    {
        name: 'workspace',
        description: 'Show current workspace info',
    },
    {
        name: 'help',
        description: 'Show Kestrel bot help',
    },
];

// ── Embed Colors ───────────────────────────────────────────────────

export const COLORS = {
    primary: 0x6366f1,   // Indigo
    success: 0x22c55e,   // Green
    warning: 0xf59e0b,   // Amber
    error: 0xef4444,   // Red
    info: 0x3b82f6,   // Blue
    task: 0x8b5cf6,   // Purple
    progress: 0x64748b,   // Slate
};