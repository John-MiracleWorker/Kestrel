# Auto

## Configuration
- **Artifacts Path**: {@artifacts_path} → `.zenflow/tasks/{task_id}`

---

## Agent Instructions

Ask the user questions when anything is unclear or needs their input. This includes:
- Ambiguous or incomplete requirements
- Technical decisions that affect architecture or user experience
- Trade-offs that require business context

Do not make assumptions on important decisions — get clarification first.

---

## Workflow Steps

### [ ] Step: Implementation
- If task is small and clear -> jump directly to implementation, without any additional steps or planning
- If the task is complex enough, plan how you want to address it. Plan can include requirements, tech specification and key implementation steps.
- Break down the work into concrete tasks (incrementable, testable milestones). Rule of thumb for step size: each step should represent a coherent unit of work (e.g., implement a component, add an API endpoint, write tests for a module). Avoid steps that are too granular (single function).
- To reflect actual purpose of the first step, you can update step Implementation, renaming it to something more relevant, like Planning. Do NOT remove meta information, like comments for any step.
- Update `{@artifacts_path}/plan.md`.
