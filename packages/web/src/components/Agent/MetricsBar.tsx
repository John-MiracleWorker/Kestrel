import React from 'react';
import './MetricsBar.css';

/**
 * MetricsBar — real-time token/cost/performance display.
 * 
 * Shows compact metrics during agent execution:
 * - Total tokens used
 * - Estimated cost in USD  
 * - Elapsed time
 * - Tool call count
 * - LLM call count
 */

interface MetricsBarProps {
    metrics: {
        tokens?: number;
        cost_usd?: number;
        elapsed_ms?: number;
        tools?: number;
        llm_calls?: number;
        prompt_tokens?: number;
        completion_tokens?: number;
        context_compactions?: number;
        model_failovers?: number;
    } | null;
    isRunning?: boolean;
}

function formatTokens(n: number): string {
    if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`;
    if (n >= 1_000) return `${(n / 1_000).toFixed(1)}K`;
    return n.toString();
}

function formatCost(usd: number): string {
    if (usd === 0) return 'Free';
    if (usd < 0.001) return `<$0.001`;
    if (usd < 1) return `$${usd.toFixed(3)}`;
    return `$${usd.toFixed(2)}`;
}

function formatTime(ms: number): string {
    if (ms < 1000) return `${ms}ms`;
    const seconds = ms / 1000;
    if (seconds < 60) return `${seconds.toFixed(1)}s`;
    const minutes = Math.floor(seconds / 60);
    const remaining = Math.floor(seconds % 60);
    return `${minutes}m ${remaining}s`;
}

export const MetricsBar: React.FC<MetricsBarProps> = ({ metrics, isRunning }) => {
    if (!metrics) return null;

    const tokens = metrics.tokens ?? 0;
    const cost = metrics.cost_usd ?? 0;
    const elapsed = metrics.elapsed_ms ?? 0;
    const tools = metrics.tools ?? 0;
    const llmCalls = metrics.llm_calls ?? 0;

    return (
        <div className={`metrics-bar ${isRunning ? 'metrics-bar--active' : ''}`}>
            <div className="metrics-bar__items">
                <div className="metrics-bar__item" title="Total tokens (prompt + completion)">
                    <span className="metrics-bar__icon">🔤</span>
                    <span className="metrics-bar__value">{formatTokens(tokens)}</span>
                    <span className="metrics-bar__label">tokens</span>
                </div>

                <div className="metrics-bar__divider" />

                <div className="metrics-bar__item" title="Estimated cost">
                    <span className="metrics-bar__icon">💰</span>
                    <span className="metrics-bar__value">{formatCost(cost)}</span>
                </div>

                <div className="metrics-bar__divider" />

                <div className="metrics-bar__item" title="Elapsed time">
                    <span className="metrics-bar__icon">⏱️</span>
                    <span className="metrics-bar__value">{formatTime(elapsed)}</span>
                </div>

                <div className="metrics-bar__divider" />

                <div className="metrics-bar__item" title="Tool executions">
                    <span className="metrics-bar__icon">🔧</span>
                    <span className="metrics-bar__value">{tools}</span>
                    <span className="metrics-bar__label">tools</span>
                </div>

                <div className="metrics-bar__divider" />

                <div className="metrics-bar__item" title="LLM API calls">
                    <span className="metrics-bar__icon">🧠</span>
                    <span className="metrics-bar__value">{llmCalls}</span>
                    <span className="metrics-bar__label">calls</span>
                </div>

                {(metrics.context_compactions ?? 0) > 0 && (
                    <>
                        <div className="metrics-bar__divider" />
                        <div className="metrics-bar__item metrics-bar__item--warning" title="Context compactions">
                            <span className="metrics-bar__icon">📦</span>
                            <span className="metrics-bar__value">{metrics.context_compactions}</span>
                            <span className="metrics-bar__label">compact</span>
                        </div>
                    </>
                )}

                {(metrics.model_failovers ?? 0) > 0 && (
                    <>
                        <div className="metrics-bar__divider" />
                        <div className="metrics-bar__item metrics-bar__item--danger" title="Model failovers">
                            <span className="metrics-bar__icon">🔄</span>
                            <span className="metrics-bar__value">{metrics.model_failovers}</span>
                            <span className="metrics-bar__label">failover</span>
                        </div>
                    </>
                )}
            </div>

            {isRunning && <div className="metrics-bar__pulse" />}
        </div>
    );
};

export default MetricsBar;
