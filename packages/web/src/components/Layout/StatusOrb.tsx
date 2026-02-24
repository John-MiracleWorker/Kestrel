/**
 * StatusOrb – Ambient animated status indicator.
 * Breathes/pulses with different colors based on system state.
 */
import { useMemo } from 'react';

export type OrbState = 'idle' | 'thinking' | 'executing' | 'escalated' | 'error' | 'disconnected';

interface StatusOrbProps {
    isConnected: boolean;
    isStreaming: boolean;
    toolStatus?: string;
    wasEscalated?: boolean;
}

function deriveState(props: StatusOrbProps): OrbState {
    if (!props.isConnected) return 'disconnected';
    if (props.wasEscalated) return 'escalated';
    if (props.toolStatus === 'calling') return 'executing';
    if (props.isStreaming) return 'thinking';
    return 'idle';
}

const STATE_CONFIG: Record<OrbState, { color: string; glow: string; animation: string; label: string }> = {
    idle: { color: '#00f3ff', glow: 'rgba(0, 243, 255, 0.4)', animation: 'orb-breathe 3s ease-in-out infinite', label: 'ONLINE' },
    thinking: { color: '#bd00ff', glow: 'rgba(189, 0, 255, 0.5)', animation: 'orb-pulse 1.2s ease-in-out infinite', label: 'THINKING' },
    executing: { color: '#f59e0b', glow: 'rgba(245, 158, 11, 0.5)', animation: 'orb-flash 0.6s ease-in-out infinite', label: 'EXECUTING' },
    escalated: { color: '#f97316', glow: 'rgba(249, 115, 22, 0.5)', animation: 'orb-escalated 1.5s ease-in-out infinite', label: 'CLOUD' },
    error: { color: '#ff0055', glow: 'rgba(255, 0, 85, 0.5)', animation: 'orb-error 0.8s ease-in-out infinite', label: 'ERROR' },
    disconnected: { color: '#6b7280', glow: 'rgba(107, 114, 128, 0.3)', animation: 'none', label: 'OFFLINE' },
};

export function StatusOrb(props: StatusOrbProps) {
    const state = useMemo(() => deriveState(props), [props.isConnected, props.isStreaming, props.toolStatus, props.wasEscalated]);
    const config = STATE_CONFIG[state];

    return (
        <div
            style={{
                display: 'flex',
                alignItems: 'center',
                gap: '8px',
            }}
            title={config.label}
        >
            {/* The orb itself */}
            <div
                style={{
                    position: 'relative',
                    width: '14px',
                    height: '14px',
                }}
            >
                {/* Outer glow ring */}
                <div
                    style={{
                        position: 'absolute',
                        inset: '-3px',
                        borderRadius: '50%',
                        background: `radial-gradient(circle, ${config.glow} 0%, transparent 70%)`,
                        animation: config.animation,
                    }}
                />
                {/* Core orb */}
                <div
                    style={{
                        position: 'absolute',
                        inset: '0',
                        borderRadius: '50%',
                        background: `radial-gradient(circle at 35% 35%, ${config.color}, ${config.color}aa)`,
                        boxShadow: `0 0 8px ${config.glow}, inset 0 0 3px rgba(255,255,255,0.3)`,
                        animation: config.animation,
                    }}
                />
            </div>

            {/* Label */}
            <span
                style={{
                    fontFamily: 'var(--font-mono)',
                    fontSize: '0.65rem',
                    color: config.color,
                    letterSpacing: '0.08em',
                    fontWeight: 600,
                    transition: 'color 0.3s',
                }}
            >
                {config.label}
            </span>

            <style>{`
                @keyframes orb-breathe {
                    0%, 100% { opacity: 0.6; transform: scale(1); }
                    50% { opacity: 1; transform: scale(1.1); }
                }
                @keyframes orb-pulse {
                    0%, 100% { opacity: 0.7; transform: scale(1); }
                    50% { opacity: 1; transform: scale(1.25); }
                }
                @keyframes orb-flash {
                    0%, 100% { opacity: 0.8; transform: scale(1); }
                    50% { opacity: 1; transform: scale(1.15); }
                }
                @keyframes orb-escalated {
                    0%, 100% { opacity: 0.7; transform: scale(1); box-shadow: 0 0 8px rgba(249, 115, 22, 0.3); }
                    50% { opacity: 1; transform: scale(1.2); box-shadow: 0 0 20px rgba(249, 115, 22, 0.6); }
                }
                @keyframes orb-error {
                    0%, 100% { opacity: 0.6; transform: scale(1); }
                    25% { opacity: 1; transform: scale(1.3); }
                    75% { opacity: 0.8; transform: scale(0.95); }
                }
            `}</style>
        </div>
    );
}
