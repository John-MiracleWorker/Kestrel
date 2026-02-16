import React from 'react';
import './PlanView.css';

interface PlanStep {
    id: string;
    name: string;
    description: string;
    depends_on: string[];
    status?: string;
}

interface PlanViewProps {
    planJson: string;
    progress: Record<string, string>;
}

/**
 * Visual representation of an agent's task plan as a step graph.
 */
export function PlanView({ planJson, progress }: PlanViewProps) {
    let steps: PlanStep[] = [];

    try {
        const plan = JSON.parse(planJson);
        steps = plan.steps || [];
    } catch {
        return (
            <div className="plan-view plan-view--error">
                Could not parse plan data.
            </div>
        );
    }

    const completedStep = parseInt(progress.current_step || '0');

    return (
        <div className="plan-view">
            <div className="plan-view__header">
                <span>📋</span>
                <span>Execution Plan ({steps.length} steps)</span>
            </div>

            <div className="plan-view__steps">
                {steps.map((step, index) => {
                    const stepStatus =
                        index < completedStep
                            ? 'complete'
                            : index === completedStep
                                ? 'active'
                                : 'pending';

                    return (
                        <div
                            key={step.id}
                            className={`plan-step plan-step--${stepStatus}`}
                        >
                            <div className="plan-step__indicator">
                                {stepStatus === 'complete' && '✅'}
                                {stepStatus === 'active' && '🔄'}
                                {stepStatus === 'pending' && '○'}
                            </div>
                            <div className="plan-step__content">
                                <div className="plan-step__name">{step.name}</div>
                                <div className="plan-step__desc">
                                    {step.description}
                                </div>
                                {step.depends_on.length > 0 && (
                                    <div className="plan-step__deps">
                                        Depends on: {step.depends_on.join(', ')}
                                    </div>
                                )}
                            </div>
                        </div>
                    );
                })}
            </div>
        </div>
    );
}

export default PlanView;
