import React, { useState } from 'react';
import './WorkflowPicker.css';

/**
 * WorkflowPicker — grid of one-click workflow templates.
 *
 * Displays available workflows as cards. When a user clicks one,
 * it expands to show variable input fields, then launches the task.
 */

interface WorkflowVariable {
    name: string;
    label: string;
    description?: string;
    type: 'text' | 'textarea' | 'select' | 'number';
    required: boolean;
    default: string;
    options?: string[];
}

interface Workflow {
    id: string;
    name: string;
    description: string;
    icon: string;
    category: string;
    goal_template: string;
    variables: WorkflowVariable[];
    specialist?: string;
    tags?: string[];
}

interface WorkflowPickerProps {
    workflows: Workflow[];
    onLaunch: (workflowId: string, variables: Record<string, string>) => void;
    isLoading?: boolean;
}

export const WorkflowPicker: React.FC<WorkflowPickerProps> = ({
    workflows,
    onLaunch,
    isLoading = false,
}) => {
    const [selectedId, setSelectedId] = useState<string | null>(null);
    const [variables, setVariables] = useState<Record<string, string>>({});
    const [filter, setFilter] = useState<string>('all');

    const selected = workflows.find(w => w.id === selectedId);
    const categories = ['all', ...Array.from(new Set(workflows.map(w => w.category)))];

    const filtered = filter === 'all'
        ? workflows
        : workflows.filter(w => w.category === filter);

    const handleSelect = (workflow: Workflow) => {
        if (selectedId === workflow.id) {
            setSelectedId(null);
            setVariables({});
            return;
        }
        setSelectedId(workflow.id);
        // Pre-fill defaults
        const defaults: Record<string, string> = {};
        workflow.variables.forEach(v => {
            defaults[v.name] = v.default || '';
        });
        setVariables(defaults);
    };

    const handleLaunch = () => {
        if (!selectedId) return;
        onLaunch(selectedId, variables);
        setSelectedId(null);
        setVariables({});
    };

    const canLaunch = selected?.variables.every(
        v => !v.required || variables[v.name]?.trim()
    );

    return (
        <div className="workflow-picker">
            <div className="workflow-picker__header">
                <h3 className="workflow-picker__title">Quick Actions</h3>
                <div className="workflow-picker__filters">
                    {categories.map(cat => (
                        <button
                            key={cat}
                            className={`workflow-picker__filter ${filter === cat ? 'active' : ''}`}
                            onClick={() => setFilter(cat)}
                        >
                            {cat === 'all' ? '✨ All' : cat.charAt(0).toUpperCase() + cat.slice(1)}
                        </button>
                    ))}
                </div>
            </div>

            <div className="workflow-picker__grid">
                {filtered.map(wf => (
                    <div
                        key={wf.id}
                        className={`workflow-card ${selectedId === wf.id ? 'workflow-card--selected' : ''}`}
                        onClick={() => handleSelect(wf)}
                    >
                        <div className="workflow-card__icon">{wf.icon}</div>
                        <div className="workflow-card__info">
                            <div className="workflow-card__name">{wf.name}</div>
                            <div className="workflow-card__desc">{wf.description}</div>
                        </div>
                        {wf.tags && wf.tags.length > 0 && (
                            <div className="workflow-card__tags">
                                {wf.tags.slice(0, 3).map(tag => (
                                    <span key={tag} className="workflow-card__tag">{tag}</span>
                                ))}
                            </div>
                        )}
                    </div>
                ))}
            </div>

            {selected && (
                <div className="workflow-picker__config">
                    <div className="workflow-picker__config-header">
                        <span className="workflow-picker__config-icon">{selected.icon}</span>
                        <span className="workflow-picker__config-title">{selected.name}</span>
                    </div>

                    <div className="workflow-picker__fields">
                        {selected.variables.map(v => (
                            <div key={v.name} className="workflow-field">
                                <label className="workflow-field__label">
                                    {v.label}
                                    {v.required && <span className="workflow-field__required">*</span>}
                                </label>
                                {v.description && (
                                    <span className="workflow-field__hint">{v.description}</span>
                                )}

                                {v.type === 'select' ? (
                                    <select
                                        className="workflow-field__select"
                                        value={variables[v.name] || ''}
                                        onChange={e => setVariables({ ...variables, [v.name]: e.target.value })}
                                    >
                                        {v.options?.map(opt => (
                                            <option key={opt} value={opt}>{opt}</option>
                                        ))}
                                    </select>
                                ) : v.type === 'textarea' ? (
                                    <textarea
                                        className="workflow-field__textarea"
                                        value={variables[v.name] || ''}
                                        onChange={e => setVariables({ ...variables, [v.name]: e.target.value })}
                                        placeholder={v.description || v.label}
                                        rows={3}
                                    />
                                ) : (
                                    <input
                                        className="workflow-field__input"
                                        type={v.type === 'number' ? 'number' : 'text'}
                                        value={variables[v.name] || ''}
                                        onChange={e => setVariables({ ...variables, [v.name]: e.target.value })}
                                        placeholder={v.description || v.label}
                                    />
                                )}
                            </div>
                        ))}
                    </div>

                    <button
                        className="workflow-picker__launch"
                        onClick={handleLaunch}
                        disabled={!canLaunch || isLoading}
                    >
                        {isLoading ? (
                            <>
                                <span className="workflow-picker__spinner" />
                                Launching...
                            </>
                        ) : (
                            <>🚀 Launch {selected.name}</>
                        )}
                    </button>
                </div>
            )}
        </div>
    );
};

export default WorkflowPicker;
