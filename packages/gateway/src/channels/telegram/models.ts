import type { TelegramAdapter } from './index';

const OLLAMA_PORT = 11434;

interface OllamaModelInfo {
    name: string;
    parameterSize: string;
    family: string;
    host: string;
}

function getOllamaHosts(): string[] {
    const hosts: string[] = [];
    const seen = new Set<string>();

    const add = (host: string) => {
        const normalized = host.replace(/\/$/, '');
        const url = normalized.startsWith('http')
            ? normalized
            : `http://${normalized}:${OLLAMA_PORT}`;
        if (!seen.has(url)) {
            seen.add(url);
            hosts.push(url);
        }
    };

    if (process.env.OLLAMA_HOST) {
        add(process.env.OLLAMA_HOST);
    }
    if (process.env.OLLAMA_REMOTE_HOSTS) {
        for (const host of process.env.OLLAMA_REMOTE_HOSTS.split(',')) {
            const trimmed = host.trim();
            if (trimmed) {
                add(trimmed);
            }
        }
    }

    add('host.docker.internal');
    add('172.17.0.1');
    add('127.0.0.1');
    return hosts;
}

async function probeOllamaHost(baseUrl: string): Promise<OllamaModelInfo[]> {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 5000);
    try {
        const response = await fetch(`${baseUrl}/api/tags`, { signal: controller.signal });
        clearTimeout(timer);
        if (!response.ok) {
            return [];
        }
        const data = (await response.json()) as { models?: any[] };
        return (data.models || []).map((model: any) => ({
            name: model.name as string,
            parameterSize: (model.details?.parameter_size || '') as string,
            family: (model.details?.family || '') as string,
            host: baseUrl,
        }));
    } catch {
        clearTimeout(timer);
        return [];
    }
}

async function fetchAllOllamaModels(): Promise<OllamaModelInfo[]> {
    const results = await Promise.allSettled(getOllamaHosts().map((host) => probeOllamaHost(host)));

    const seen = new Set<string>();
    const models: OllamaModelInfo[] = [];
    for (const result of results) {
        if (result.status !== 'fulfilled') {
            continue;
        }
        for (const model of result.value) {
            if (!seen.has(model.name)) {
                seen.add(model.name);
                models.push(model);
            }
        }
    }
    return models;
}

export async function fetchAndShowOllamaModels(
    adapter: TelegramAdapter,
    chatId: number,
    threadId: number | undefined,
    withThread: (params: Record<string, any>) => Record<string, any>,
): Promise<void> {
    const models = await fetchAllOllamaModels();
    if (models.length === 0) {
        await adapter.api(
            'sendMessage',
            withThread({
                chat_id: chatId,
                text: '⚠️ No Ollama models found. Make sure Ollama is running and has models installed.\n\nTip: Set OLLAMA_HOST or OLLAMA_REMOTE_HOSTS to connect to remote servers.',
            }),
        );
        return;
    }

    const uniqueHosts = new Set(models.map((model) => model.host));
    const multiHost = uniqueHosts.size > 1;
    const buttons = models.map((model) => {
        let label = model.parameterSize ? `${model.name} (${model.parameterSize})` : model.name;
        if (multiHost) {
            try {
                const hostname = new URL(model.host).hostname;
                if (
                    !['host.docker.internal', '172.17.0.1', '127.0.0.1', 'localhost'].includes(
                        hostname,
                    )
                ) {
                    label += ` @${hostname}`;
                }
            } catch {
                // Ignore malformed hosts and show the model label only.
            }
        }
        return { text: label, callback_data: `model:${model.name}` };
    });

    const keyboard: { text: string; callback_data: string }[][] = [];
    for (let index = 0; index < buttons.length; index += 2) {
        keyboard.push(buttons.slice(index, index + 2));
    }

    const headerParts = [`🤖 *Available Ollama Models* (${models.length})`];
    if (multiHost) {
        headerParts.push(`from ${uniqueHosts.size} servers`);
    }
    headerParts.push('\nTap a model to switch:');

    await adapter.api(
        'sendMessage',
        withThread({
            chat_id: chatId,
            text: headerParts.join(' '),
            parse_mode: 'Markdown',
            reply_markup: JSON.stringify({ inline_keyboard: keyboard }),
        }),
    );
}
