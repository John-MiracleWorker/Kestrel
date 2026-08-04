import { Pencil, Plus } from "lucide-react";
import type { FormEvent } from "react";
import { Field, Panel } from "../components";

export type RoutineDraft = {
  name: string;
  prompt: string;
  schedule_kind: "once" | "interval" | "cron";
  start_at_local: string;
  interval_seconds: string;
  cron_expression: string;
  timezone: string;
  delivery_channel_id: string;
  delivery_conversation_id: string;
  delivery_template: string;
  workspace: string;
  provider: string;
  model: string;
  autonomy_mode: string;
  misfire_grace_seconds: string;
};

export type RoutineEditorProps = {
  mode: "create" | "edit";
  routineName: string | null;
  draft: RoutineDraft;
  localTimeZone: string;
  mutationPending: boolean;
  onDraftChange: (next: (current: RoutineDraft) => RoutineDraft) => void;
  onSubmit: (event: FormEvent) => void;
  onCancel: () => void;
};

export function RoutineEditor({
  mode,
  routineName,
  draft,
  localTimeZone,
  mutationPending,
  onDraftChange,
  onSubmit,
  onCancel
}: RoutineEditorProps) {
  return (
    <Panel
      id="routine-editor"
      title={mode === "edit" ? `Edit ${routineName ?? "routine"}` : "Create routine"}
      icon={mode === "edit" ? <Pencil size={19} /> : <Plus size={19} />}
    >
      <form
        className="routine-editor-form"
        aria-label={mode === "edit" ? "Edit routine" : "Create routine"}
        onSubmit={onSubmit}
      >
        <Field label="Routine name">
          <input required maxLength={200} value={draft.name} onChange={(event) => onDraftChange((current) => ({ ...current, name: event.target.value }))} />
        </Field>
        <Field label="Prompt">
          <textarea required maxLength={20_000} rows={5} value={draft.prompt} onChange={(event) => onDraftChange((current) => ({ ...current, prompt: event.target.value }))} />
        </Field>
        <div className="field-row">
          <Field label="Schedule">
            <select value={draft.schedule_kind} onChange={(event) => onDraftChange((current) => ({ ...current, schedule_kind: event.target.value as "once" | "interval" | "cron" }))}>
              <option value="once">Once</option>
              <option value="interval">Fixed interval</option>
              <option value="cron">Cron / calendar</option>
            </select>
          </Field>
          <Field label={`Start time (${localTimeZone})`} hint="Stored as UTC after submission.">
            <input type="datetime-local" required value={draft.start_at_local} onChange={(event) => onDraftChange((current) => ({ ...current, start_at_local: event.target.value }))} />
          </Field>
          {draft.schedule_kind === "interval" && (
            <Field label="Interval seconds" hint="Minimum 60 seconds.">
              <input type="number" required min="60" max="31536000" step="1" value={draft.interval_seconds} onChange={(event) => onDraftChange((current) => ({ ...current, interval_seconds: event.target.value }))} />
            </Field>
          )}
          {draft.schedule_kind === "cron" && (
            <>
              <Field label="Cron expression" hint="Five fields: minute hour day month weekday.">
                <input required value={draft.cron_expression} onChange={(event) => onDraftChange((current) => ({ ...current, cron_expression: event.target.value }))} placeholder="0 9 * * 1-5" />
              </Field>
              <Field label="IANA timezone" hint="DST is evaluated in this named timezone.">
                <input required maxLength={128} value={draft.timezone} onChange={(event) => onDraftChange((current) => ({ ...current, timezone: event.target.value }))} placeholder="America/Detroit" />
              </Field>
            </>
          )}
          <Field label="Misfire grace seconds">
            <input type="number" required min="0" max="604800" step="1" value={draft.misfire_grace_seconds} onChange={(event) => onDraftChange((current) => ({ ...current, misfire_grace_seconds: event.target.value }))} />
          </Field>
        </div>
        <div className="field-row">
          <Field label="Workspace" hint="Blank uses the configured default.">
            <input maxLength={4096} value={draft.workspace} onChange={(event) => onDraftChange((current) => ({ ...current, workspace: event.target.value }))} />
          </Field>
          <Field label="Provider" hint="Blank uses the configured default.">
            <input maxLength={256} value={draft.provider} onChange={(event) => onDraftChange((current) => ({ ...current, provider: event.target.value }))} />
          </Field>
          <Field label="Model" hint="Blank uses the configured default.">
            <input maxLength={256} value={draft.model} onChange={(event) => onDraftChange((current) => ({ ...current, model: event.target.value }))} />
          </Field>
          <Field label="Autonomy">
            <select value={draft.autonomy_mode} onChange={(event) => onDraftChange((current) => ({ ...current, autonomy_mode: event.target.value }))}>
              <option value="background">Safe Auto</option>
              <option value="manual">Manual</option>
              <option value="autonomous">Autopilot</option>
            </select>
          </Field>
        </div>
        <div className="field-row">
          <Field label="Delivery channel" hint="Optional configured channel id.">
            <input maxLength={128} value={draft.delivery_channel_id} onChange={(event) => onDraftChange((current) => ({ ...current, delivery_channel_id: event.target.value }))} placeholder="telegram" />
          </Field>
          <Field label="Delivery conversation" hint="Required when a channel is selected.">
            <input maxLength={512} value={draft.delivery_conversation_id} onChange={(event) => onDraftChange((current) => ({ ...current, delivery_conversation_id: event.target.value }))} placeholder="chat or webhook destination" />
          </Field>
          <Field label="Delivery template" hint="Supports {result}, {run_id}, and {run_status}.">
            <input maxLength={4000} value={draft.delivery_template} onChange={(event) => onDraftChange((current) => ({ ...current, delivery_template: event.target.value }))} />
          </Field>
        </div>
        <div className="page-actions">
          <button className="btn primary" type="submit" disabled={mutationPending}>{mutationPending ? "Saving…" : "Save routine"}</button>
          <button className="btn subtle" type="button" onClick={onCancel} disabled={mutationPending}>Cancel</button>
        </div>
      </form>
    </Panel>
  );
}
