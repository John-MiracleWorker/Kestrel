import { S } from '../constants';
import { Toggle } from '../Shared';

interface IntegrationsTabProps {
    telegramEnabled: boolean;
    setTelegramEnabled: (v: boolean) => void;
    telegramToken: string;
    setTelegramToken: (v: string) => void;
    discordEnabled: boolean;
    setDiscordEnabled: (v: boolean) => void;
    discordToken: string;
    setDiscordToken: (v: string) => void;
    webhookUrl: string;
    setWebhookUrl: (v: string) => void;
    webhookSecret: string;
    setWebhookSecret: (v: string) => void;
    webhookEvents: string[];
    handleWebhookEventToggle: (ev: string) => void;
}

const WEBHOOK_EVENTS = ['task.started', 'task.completed', 'task.failed', 'message.created'];

export function IntegrationsTab({
    telegramEnabled,
    setTelegramEnabled,
    telegramToken,
    setTelegramToken,
    discordEnabled,
    setDiscordEnabled,
    discordToken,
    setDiscordToken,
    webhookUrl,
    setWebhookUrl,
    webhookSecret,
    setWebhookSecret,
    webhookEvents,
    handleWebhookEventToggle
}: IntegrationsTabProps) {
    return (
        <div>
            <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '16px' }}>
                <div style={S.sectionTitle}>// TELEGRAM BOT</div>
                <Toggle value={telegramEnabled} onChange={setTelegramEnabled} />
            </div>
            {telegramEnabled && (
                <div style={{ padding: '16px', background: '#0d0d0d', border: '1px solid #1a1a1a', borderRadius: '4px', marginBottom: '24px' }}>
                    <div style={S.field}>
                        <label style={S.label}>Bot Token</label>
                        <input style={S.input} type="password" value={telegramToken}
                            onChange={e => setTelegramToken(e.target.value)}
                            placeholder="123456789:ABCdefGHIjklmNOPqrstUVwxyZ"
                            onFocus={e => { e.target.style.borderColor = '#00f3ff'; }}
                            onBlur={e => { e.target.style.borderColor = '#333'; }} />
                        <div style={{ fontSize: '0.65rem', color: '#666', marginTop: '6px' }}>
                            Talk to @BotFather on Telegram to create a bot and get a token.
                        </div>
                    </div>
                </div>
            )}

            <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '16px', marginTop: '32px' }}>
                <div style={S.sectionTitle}>// DISCORD BOT</div>
                <Toggle value={discordEnabled} onChange={setDiscordEnabled} />
            </div>
            {discordEnabled && (
                <div style={{ padding: '16px', background: '#0d0d0d', border: '1px solid #1a1a1a', borderRadius: '4px', marginBottom: '24px' }}>
                    <div style={S.field}>
                        <label style={S.label}>Bot Token</label>
                        <input style={S.input} type="password" value={discordToken}
                            onChange={e => setDiscordToken(e.target.value)}
                            placeholder="MTAxMjM0NTY3ODkw.GABCD_.1234567890abcdef"
                            onFocus={e => { e.target.style.borderColor = '#00f3ff'; }}
                            onBlur={e => { e.target.style.borderColor = '#333'; }} />
                        <div style={{ fontSize: '0.65rem', color: '#666', marginTop: '6px' }}>
                            Create a bot in the Discord Developer Portal to get a token.
                        </div>
                    </div>
                </div>
            )}

            <div style={{ ...S.sectionTitle, marginTop: '32px' }}>// WEBHOOKS</div>
            <p style={{ fontSize: '0.75rem', color: '#666', marginBottom: '20px', lineHeight: 1.5 }}>
                Send real-time event notifications to external services.
            </p>
            <div style={S.field}>
                <label style={S.label}>Endpoint URL</label>
                <input style={S.input} value={webhookUrl}
                    onChange={e => setWebhookUrl(e.target.value)}
                    placeholder="https://api.yourdomain.com/webhooks/kestrel"
                    onFocus={e => { e.target.style.borderColor = '#00f3ff'; }}
                    onBlur={e => { e.target.style.borderColor = '#333'; }} />
            </div>
            <div style={S.field}>
                <label style={S.label}>Secret (for signature verification)</label>
                <input style={S.input} type="password" value={webhookSecret}
                    onChange={e => setWebhookSecret(e.target.value)}
                    placeholder="Leave blank to generate automatically"
                    onFocus={e => { e.target.style.borderColor = '#00f3ff'; }}
                    onBlur={e => { e.target.style.borderColor = '#333'; }} />
            </div>
            <div style={S.field}>
                <label style={S.label}>Events to track</label>
                <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '8px' }}>
                    {WEBHOOK_EVENTS.map(ev => (
                        <label key={ev} style={{
                            display: 'flex', alignItems: 'center', gap: '8px', cursor: 'pointer',
                            fontSize: '0.75rem', color: '#aaa', padding: '8px 10px',
                            background: '#111', borderRadius: '4px', border: '1px solid #222'
                        }}>
                            <input type="checkbox" checked={webhookEvents.includes(ev)}
                                onChange={() => handleWebhookEventToggle(ev)}
                                style={{ accentColor: '#00f3ff' }} />
                            {ev}
                        </label>
                    ))}
                </div>
            </div>
        </div>
    );
}
