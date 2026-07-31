import { CalendarClock, Pencil, Play, Plus, RefreshCw, ShieldCheck, Trash2 } from "lucide-react";
import { useCallback, useEffect, useRef, useState, type FormEvent } from "react";
import { ApiAuthError, ApiResponseError, deleteJson, getJson, postJson, putJson, queryString } from "../api";
import { ActionError, EmptyState, Field, InlineMeta, Metric, Panel, StatusBadge } from "../components";
import type { Routine, RoutineDelivery, RoutineOccurrence, RoutineRunNowResult, RoutineStatus } from "../types";
type RoutineDraft = {
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

type RoutineRunNowRequestRecord = {
  idempotencyKey: string;
  expectedRevision: number;
};

const ROUTINE_RUN_NOW_STORAGE_PREFIX = "kestrel.routine.run-now.v1:";
const ROUTINE_HISTORY_POLL_INTERVAL_MS = 1_500;
const ROUTINE_HISTORY_MAX_POLLS = 400;
const ROUTINE_NONTERMINAL_STATUSES = new Set(["claimed", "running"]);
const ROUTINE_RUN_NOW_DEFINITIVE_REJECTION_STATUSES = new Set([400, 401, 403, 404, 409, 422]);

type RoutineHistoryRequest = {
  routineId: string;
  controller: AbortController;
  promise: Promise<void>;
};

export function AutomateWorkspace({ onAuthRequired }: { onAuthRequired: () => void }) {
  const [status, setStatus] = useState<RoutineStatus | null>(null);
  const [routines, setRoutines] = useState<Routine[]>([]);
  const [selectedRoutineId, setSelectedRoutineId] = useState<string | null>(null);
  const selectedRoutineIdRef = useRef<string | null>(null);
  const [history, setHistory] = useState<RoutineOccurrence[]>([]);
  const [deliveries, setDeliveries] = useState<RoutineDelivery[]>([]);
  const [loading, setLoading] = useState(true);
  const [historyLoading, setHistoryLoading] = useState(false);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [historyError, setHistoryError] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [editorMode, setEditorMode] = useState<"create" | "edit" | null>(null);
  const [draft, setDraft] = useState<RoutineDraft>(() => emptyRoutineDraft());
  const [mutationPending, setMutationPending] = useState(false);
  const [runNowPendingId, setRunNowPendingId] = useState<string | null>(null);
  const [uncertainRoutineIds, setUncertainRoutineIds] = useState<Set<string>>(() => new Set());
  const [runNowResult, setRunNowResult] = useState<RoutineRunNowResult | null>(null);
  const runNowRequestRef = useRef(new Map<string, RoutineRunNowRequestRecord>());
  const historyRequestRef = useRef<RoutineHistoryRequest | null>(null);

  const selectedRoutine = routines.find((routine) => routine.routine_id === selectedRoutineId) ?? null;
  const selectedHistoryHasNonterminalOccurrence = history.some((occurrence) =>
    ROUTINE_NONTERMINAL_STATUSES.has(occurrence.status)
  );
  const localTimeZone = Intl.DateTimeFormat().resolvedOptions().timeZone || "local time";

  function selectRoutineId(routineId: string | null) {
    if (selectedRoutineIdRef.current !== routineId) {
      historyRequestRef.current?.controller.abort();
      historyRequestRef.current = null;
      setHistory([]);
      setDeliveries([]);
      setHistoryError(null);
      setHistoryLoading(false);
    }
    selectedRoutineIdRef.current = routineId;
    setSelectedRoutineId(routineId);
  }

  const handleError = useCallback((value: unknown, fallback: string) => {
    if (value instanceof ApiAuthError) {
      onAuthRequired();
      return "Kestrel API authentication is required for routine owner actions.";
    }
    return value instanceof Error ? value.message : fallback;
  }, [onAuthRequired]);

  const refreshHistory = useCallback(async (routineId: string, options: { showLoading?: boolean } = {}) => {
    const existingRequest = historyRequestRef.current;
    if (existingRequest?.routineId === routineId) {
      return existingRequest.promise;
    }

    existingRequest?.controller.abort();
    const controller = new AbortController();
    const request: RoutineHistoryRequest = {
      routineId,
      controller,
      promise: Promise.resolve()
    };
    historyRequestRef.current = request;

    if (options.showLoading !== false) setHistoryLoading(true);
    setHistoryError(null);
    request.promise = (async () => {
      try {
        const rows = await getJson<RoutineOccurrence[]>(
          `/api/routines/${encodeURIComponent(routineId)}/history${queryString({ limit: 50 })}`,
          { signal: controller.signal }
        );
        if (controller.signal.aborted || selectedRoutineIdRef.current !== routineId) return;
        setHistory(rows);
        try {
          const deliveryRows = await getJson<RoutineDelivery[]>(
            `/api/routines/${encodeURIComponent(routineId)}/deliveries${queryString({ limit: 50 })}`,
            { signal: controller.signal }
          );
          if (!controller.signal.aborted && selectedRoutineIdRef.current === routineId) {
            setDeliveries(Array.isArray(deliveryRows) ? deliveryRows : []);
          }
        } catch {
          if (!controller.signal.aborted && selectedRoutineIdRef.current === routineId) {
            setDeliveries([]);
          }
        }
        setRunNowResult((current) => {
          if (!current || current.occurrence.routine_id !== routineId) return current;
          const occurrence = rows.find(
            (row) => row.occurrence_id === current.occurrence.occurrence_id
          );
          if (!occurrence) return current;
          return {
            ...current,
            occurrence,
            dispatch: current.dispatch
              ? {
                  ...current.dispatch,
                  status: occurrence.status,
                  error: occurrence.error
                }
              : null
          };
        });
      } catch (value) {
        if (controller.signal.aborted || selectedRoutineIdRef.current !== routineId) return;
        setHistory([]);
        setDeliveries([]);
        setHistoryError(handleError(value, "Routine history is unavailable."));
      } finally {
        if (historyRequestRef.current === request) historyRequestRef.current = null;
        if (!controller.signal.aborted && selectedRoutineIdRef.current === routineId) {
          setHistoryLoading(false);
        }
      }
    })();
    return request.promise;
  }, [handleError]);

  async function refreshWorkbench(preferredRoutineId = selectedRoutineIdRef.current) {
    setLoading(true);
    setLoadError(null);
    const [statusResult, routinesResult] = await Promise.allSettled([
      getJson<RoutineStatus>("/api/routines/status"),
      getJson<Routine[]>("/api/routines")
    ]);

    if (statusResult.status === "fulfilled") {
      setStatus(statusResult.value);
    } else {
      setStatus(null);
      setLoadError(handleError(statusResult.reason, "Routine status is unavailable."));
    }

    if (routinesResult.status === "fulfilled") {
      const nextRoutines = routinesResult.value;
      setRoutines(nextRoutines);
      const recoveredUncertain = new Set<string>();
      nextRoutines.forEach((routine) => {
        const recovered = runNowRequestRef.current.get(routine.routine_id) ?? readStoredRunNowRequest(routine.routine_id);
        if (!recovered) return;
        runNowRequestRef.current.set(routine.routine_id, recovered);
        recoveredUncertain.add(routine.routine_id);
      });
      setUncertainRoutineIds(recoveredUncertain);
      const nextSelection =
        nextRoutines.find((routine) => routine.routine_id === preferredRoutineId)?.routine_id ??
        nextRoutines[0]?.routine_id ??
        null;
      selectRoutineId(nextSelection);
      if (nextSelection) {
        await refreshHistory(nextSelection);
      } else {
        setHistory([]);
        setDeliveries([]);
        setHistoryError(null);
      }
    } else {
      setRoutines([]);
      selectRoutineId(null);
      setHistory([]);
      setDeliveries([]);
      const message = handleError(routinesResult.reason, "Routine definitions are unavailable.");
      setLoadError((current) => current ? `${current} ${message}` : message);
    }
    setLoading(false);
  }

  useEffect(() => {
    void refreshWorkbench();
  }, []);

  useEffect(() => () => {
    selectedRoutineIdRef.current = null;
    historyRequestRef.current?.controller.abort();
    historyRequestRef.current = null;
  }, []);

  useEffect(() => {
    if (!selectedRoutineId || !selectedHistoryHasNonterminalOccurrence) return;

    let cancelled = false;
    let pollCount = 0;
    let timeoutId: number | null = null;

    const poll = async () => {
      if (cancelled) return;
      pollCount += 1;
      await refreshHistory(selectedRoutineId, { showLoading: false });
      if (!cancelled && pollCount < ROUTINE_HISTORY_MAX_POLLS) schedulePoll();
    };
    const schedulePoll = () => {
      timeoutId = window.setTimeout(() => {
        timeoutId = null;
        void poll();
      }, ROUTINE_HISTORY_POLL_INTERVAL_MS);
    };

    schedulePoll();
    return () => {
      cancelled = true;
      if (timeoutId !== null) window.clearTimeout(timeoutId);
    };
  }, [refreshHistory, selectedHistoryHasNonterminalOccurrence, selectedRoutineId]);

  async function chooseRoutine(routine: Routine) {
    selectRoutineId(routine.routine_id);
    setRunNowResult(null);
    await refreshHistory(routine.routine_id);
  }

  function openCreateEditor() {
    setDraft(emptyRoutineDraft());
    setEditorMode("create");
    setActionError(null);
  }

  function openEditEditor(routine: Routine) {
    setDraft(routineDraftFrom(routine));
    selectRoutineId(routine.routine_id);
    setEditorMode("edit");
    setActionError(null);
  }

  async function submitRoutine(event: FormEvent) {
    event.preventDefault();
    setActionError(null);
    setNotice(null);
    setMutationPending(true);
    try {
      const payload = routinePayload(draft);
      const saved = editorMode === "edit" && selectedRoutine
        ? await putJson<Routine>(`/api/routines/${encodeURIComponent(selectedRoutine.routine_id)}`, {
            expected_revision: selectedRoutine.revision,
            ...payload
          })
        : await postJson<Routine>("/api/routines", payload);
      setEditorMode(null);
      setNotice(editorMode === "edit" ? `${saved.name} updated.` : `${saved.name} created disabled; review it before enabling.`);
      await refreshWorkbench(saved.routine_id);
    } catch (value) {
      setActionError(handleError(value, "Routine could not be saved."));
    } finally {
      setMutationPending(false);
    }
  }

  async function toggleRoutine(routine: Routine) {
    setActionError(null);
    setNotice(null);
    setMutationPending(true);
    try {
      const saved = await putJson<Routine>(`/api/routines/${encodeURIComponent(routine.routine_id)}/enabled`, {
        expected_revision: routine.revision,
        enabled: !routine.enabled
      });
      setNotice(`${saved.name} ${saved.enabled ? "enabled" : "paused"}.`);
      await refreshWorkbench(saved.routine_id);
    } catch (value) {
      setActionError(handleError(value, "Routine state could not be changed."));
    } finally {
      setMutationPending(false);
    }
  }

  async function deleteRoutine(routine: Routine) {
    if (!window.confirm(`Delete ${routine.name}? Its occurrence history remains in the local audit store.`)) return;
    setActionError(null);
    setNotice(null);
    setMutationPending(true);
    try {
      await deleteJson<Routine>(
        `/api/routines/${encodeURIComponent(routine.routine_id)}${queryString({ expected_revision: routine.revision })}`
      );
      forgetRunNowRequest(runNowRequestRef.current, routine.routine_id);
      setUncertainRoutineIds((current) => withoutSetValue(current, routine.routine_id));
      setNotice(`${routine.name} deleted.`);
      await refreshWorkbench(null);
    } catch (value) {
      setActionError(handleError(value, "Routine could not be deleted."));
    } finally {
      setMutationPending(false);
    }
  }

  async function runRoutineNow(routine: Routine) {
    let request = runNowRequestRef.current.get(routine.routine_id) ?? readStoredRunNowRequest(routine.routine_id);
    if (!request) {
      request = {
        idempotencyKey: crypto.randomUUID(),
        expectedRevision: routine.revision
      };
      runNowRequestRef.current.set(routine.routine_id, request);
      storeRunNowRequest(routine.routine_id, request);
    }

    setRunNowPendingId(routine.routine_id);
    setActionError(null);
    setNotice(null);
    try {
      const result = await postJson<RoutineRunNowResult>(
        `/api/routines/${encodeURIComponent(routine.routine_id)}/actions/run-now`,
        {
          expected_revision: request.expectedRevision,
          idempotency_key: request.idempotencyKey
        }
      );
      forgetRunNowRequest(runNowRequestRef.current, routine.routine_id);
      setUncertainRoutineIds((current) => withoutSetValue(current, routine.routine_id));
      setRunNowResult(result);
      setNotice(
        result.idempotent_replay
          ? `${routine.name} request recovered without creating a duplicate run.`
          : `${routine.name} dispatched.`
      );
      await refreshHistory(routine.routine_id);
    } catch (value) {
      if (
        value instanceof ApiResponseError
        && ROUTINE_RUN_NOW_DEFINITIVE_REJECTION_STATUSES.has(value.status)
      ) {
        forgetRunNowRequest(runNowRequestRef.current, routine.routine_id);
        setUncertainRoutineIds((current) => withoutSetValue(current, routine.routine_id));
        setActionError(handleError(value, "Routine dispatch was rejected."));
      } else {
        setUncertainRoutineIds((current) => new Set(current).add(routine.routine_id));
        const reason = value instanceof ApiResponseError
          ? `The server returned ${value.status} before confirming the outcome for ${routine.name}.`
          : `No response was received for ${routine.name}.`;
        setActionError(`${reason} Retry run now to safely reuse the same request key.`);
      }
    } finally {
      setRunNowPendingId(null);
    }
  }

  async function reconcileRoutineDelivery(
    delivery: RoutineDelivery,
    resolution: "retry" | "delivered" | "failed"
  ) {
    setMutationPending(true);
    setActionError(null);
    setNotice(null);
    try {
      const updated = await postJson<RoutineDelivery>(
        `/api/routine-deliveries/${encodeURIComponent(delivery.delivery_id)}/actions/reconcile`,
        {
          expected_attempt_count: delivery.attempt_count,
          resolution,
          receipt: resolution === "delivered"
            ? { operator_confirmed: true }
            : null
        }
      );
      setNotice(`Delivery ${updated.delivery_id} reconciled as ${updated.status}.`);
      await refreshHistory(delivery.routine_id);
    } catch (value) {
      setActionError(handleError(value, "Routine delivery could not be reconciled."));
    } finally {
      setMutationPending(false);
    }
  }

  const enabledCount = routines.filter((routine) => routine.enabled).length;
  const selectedIsUncertain = selectedRoutine ? uncertainRoutineIds.has(selectedRoutine.routine_id) : false;

  return (
    <section id="routines" className="shell page-shell routines-page" data-section="routines" aria-label="Routine Workbench">
      <header className="page-head">
        <div>
          <p className="page-eyebrow">Personal automation</p>
          <h1 className="page-title">Routine Workbench<em>.</em></h1>
          <p className="page-subtitle">
            Schedule durable local turns, inspect their audit history, and dispatch one routine now without duplicate retries.
          </p>
        </div>
        <div className="page-actions">
          <button className="btn subtle" type="button" onClick={() => void refreshWorkbench()} disabled={loading}>
            <RefreshCw size={15} /> Refresh
          </button>
          <button className="btn primary" type="button" onClick={openCreateEditor}>
            <Plus size={15} /> New routine
          </button>
        </div>
      </header>

      <div className="announcer page-notice" aria-live="polite">{notice}</div>
      {loadError && <ActionError message={loadError} onDismiss={() => setLoadError(null)} />}
      {actionError && <ActionError message={actionError} onDismiss={() => setActionError(null)} />}

      <section className="routine-status-grid" aria-label="Routine service status">
        <Metric label="Definitions" value={routines.length} />
        <Metric label="Enabled" value={enabledCount} />
        <Metric label="Dispatcher" value={status?.enabled ? "enabled" : "disabled"} />
        <Metric label="Loop" value={status?.loop?.running ? "running" : status?.loop ? "stopped" : "unavailable"} />
      </section>

      {status && !status.enabled && (
        <section className="routine-disabled-callout" role="status">
          <ShieldCheck size={18} />
          <div>
            <strong>Proactive dispatch is disabled.</strong>
            <p>Definitions remain editable, but scheduled and manual runs stay fail-closed until proactive routines are enabled at launch.</p>
          </div>
        </section>
      )}
      {status?.loop?.last_error && (
        <section className="routine-disabled-callout danger" role="alert">
          <div>
            <strong>The routine loop reported an error.</strong>
            <p>{status.loop.last_error}</p>
          </div>
        </section>
      )}

      <div className="routine-workbench-grid">
        <Panel
          id="routine-definitions"
          title="Routines"
          icon={<CalendarClock size={19} />}
          actions={<StatusBadge value={loading ? "loading" : `${routines.length} total`} />}
        >
          <div className="routine-list">
            {routines.map((routine) => (
              <article
                className={`routine-card ${routine.routine_id === selectedRoutineId ? "selected" : ""}`}
                key={routine.routine_id}
              >
                <button type="button" className="routine-select" onClick={() => void chooseRoutine(routine)}>
                  <span>
                    <strong>{routine.name}</strong>
                    <small>{routineScheduleLabel(routine)}</small>
                  </span>
                  <StatusBadge value={routine.enabled ? "enabled" : "paused"} />
                </button>
                <div className="routine-card-actions">
                  <button
                    type="button"
                    aria-label={`${routine.enabled ? "Pause" : "Enable"} ${routine.name}`}
                    onClick={() => void toggleRoutine(routine)}
                    disabled={mutationPending || uncertainRoutineIds.has(routine.routine_id)}
                  >
                    {routine.enabled ? "Pause" : "Enable"}
                  </button>
                  <button
                    type="button"
                    aria-label={`Edit ${routine.name}`}
                    onClick={() => openEditEditor(routine)}
                    disabled={mutationPending || uncertainRoutineIds.has(routine.routine_id)}
                  >
                    <Pencil size={14} /> Edit
                  </button>
                  <button
                    type="button"
                    className="btn danger"
                    aria-label={`Delete ${routine.name}`}
                    onClick={() => void deleteRoutine(routine)}
                    disabled={mutationPending || uncertainRoutineIds.has(routine.routine_id)}
                  >
                    <Trash2 size={14} /> Delete
                  </button>
                </div>
              </article>
            ))}
            {!loading && routines.length === 0 && <EmptyState>No routines yet. Create one to start with a disabled, reviewable definition.</EmptyState>}
          </div>
        </Panel>

        <Panel
          id="routine-detail"
          title={selectedRoutine?.name ?? "Routine detail"}
          icon={<Play size={19} />}
          actions={selectedRoutine ? <StatusBadge value={`revision ${selectedRoutine.revision}`} /> : undefined}
        >
          {selectedRoutine ? (
            <div className="routine-detail">
              <p>{selectedRoutine.prompt}</p>
              <dl className="routine-facts">
                <div><dt>Schedule</dt><dd>{routineScheduleLabel(selectedRoutine)}</dd></div>
                <div><dt>Next run</dt><dd>{formatRoutineDate(selectedRoutine.next_run_at)}</dd></div>
                <div><dt>Workspace</dt><dd>{selectedRoutine.workspace || "Configured default"}</dd></div>
                <div><dt>Provider</dt><dd>{[selectedRoutine.provider, selectedRoutine.model].filter(Boolean).join(" / ") || "Configured default"}</dd></div>
                <div><dt>Autonomy</dt><dd>{selectedRoutine.autonomy_mode}</dd></div>
                <div><dt>Timezone</dt><dd>{selectedRoutine.timezone || "UTC"} schedule · {localTimeZone} display</dd></div>
                <div>
                  <dt>Delivery</dt>
                  <dd>
                    {selectedRoutine.delivery && "channel_id" in selectedRoutine.delivery
                      ? `${selectedRoutine.delivery.channel_id} / ${selectedRoutine.delivery.conversation_id}`
                      : "No external destination"}
                  </dd>
                </div>
              </dl>
              <button
                type="button"
                className="btn primary routine-run-now"
                aria-label={`${selectedIsUncertain ? "Retry" : "Run"} ${selectedRoutine.name} now`}
                disabled={!status?.enabled || !selectedRoutine.enabled || runNowPendingId !== null}
                onClick={() => void runRoutineNow(selectedRoutine)}
              >
                <Play size={14} />
                {runNowPendingId === selectedRoutine.routine_id
                  ? "Dispatching…"
                  : selectedIsUncertain
                    ? "Retry run now safely"
                    : "Run now"}
              </button>
              {!selectedRoutine.enabled && <p className="muted">Enable this definition before running it.</p>}
              {selectedIsUncertain && (
                <p className="routine-retry-note" role="status">
                  Retry will reuse the original idempotency key and revision until the server gives a definite response.
                </p>
              )}
              {runNowResult?.occurrence.routine_id === selectedRoutine.routine_id && (
                <div className="routine-run-result" aria-live="polite">
                  <strong>{runNowResult.idempotent_replay ? "Recovered dispatch" : "Dispatch accepted"}</strong>
                  <InlineMeta items={[runNowResult.occurrence.run_id, runNowResult.occurrence.status, runNowResult.occurrence.trigger_kind]} />
                </div>
              )}
              <section className="routine-history" aria-labelledby="routine-history-title">
                <div className="routine-history-head">
                  <h3 id="routine-history-title">Run history</h3>
                  <StatusBadge value={historyLoading ? "loading" : `${history.length} records`} />
                </div>
                {historyError && <p className="danger-text">History unavailable: {historyError}</p>}
                <div className="list compact-list">
                  {history.map((occurrence) => (
                    <article className="data-row" key={occurrence.occurrence_id}>
                      <div className="run-title">
                        <strong>{occurrence.trigger_kind === "manual" ? "Manual run" : "Scheduled run"}</strong>
                        <StatusBadge value={occurrence.status} />
                      </div>
                      <InlineMeta items={[occurrence.run_id, formatRoutineDate(occurrence.requested_at ?? occurrence.scheduled_for)]} />
                      {(occurrence.error || occurrence.skip_reason) && <p className="danger-text">{occurrence.error || occurrence.skip_reason}</p>}
                    </article>
                  ))}
                  {!historyLoading && !historyError && history.length === 0 && <EmptyState>No occurrences recorded for this routine.</EmptyState>}
                </div>
              </section>
              <section className="routine-history" aria-labelledby="routine-delivery-title">
                <div className="routine-history-head">
                  <h3 id="routine-delivery-title">Delivery history</h3>
                  <StatusBadge value={`${deliveries.length} records`} />
                </div>
                <div className="list compact-list">
                  {deliveries.map((delivery) => (
                    <article className="data-row" key={delivery.delivery_id}>
                      <div className="run-title">
                        <strong>{delivery.destination.channel_id} / {delivery.destination.conversation_id}</strong>
                        <StatusBadge value={delivery.status} />
                      </div>
                      <InlineMeta items={[
                        `attempt ${delivery.attempt_count}`,
                        delivery.idempotency_key,
                        formatRoutineDate(delivery.delivered_at ?? delivery.updated_at)
                      ]} />
                      {delivery.error ? <p className="danger-text">{delivery.error}</p> : null}
                      {["uncertain", "failed", "blocked"].includes(delivery.status) ? (
                        <div className="page-actions">
                          <button
                            type="button"
                            disabled={mutationPending || !status?.enabled}
                            onClick={() => void reconcileRoutineDelivery(delivery, "retry")}
                          >
                            Retry with same key
                          </button>
                          <button
                            type="button"
                            disabled={mutationPending || !status?.enabled}
                            onClick={() => void reconcileRoutineDelivery(delivery, "delivered")}
                          >
                            Mark delivered
                          </button>
                          <button
                            type="button"
                            disabled={mutationPending || !status?.enabled}
                            onClick={() => void reconcileRoutineDelivery(delivery, "failed")}
                          >
                            Mark failed
                          </button>
                        </div>
                      ) : null}
                    </article>
                  ))}
                  {!historyLoading && deliveries.length === 0 ? (
                    <EmptyState>No delivery attempts recorded for this routine.</EmptyState>
                  ) : null}
                </div>
              </section>
            </div>
          ) : (
            <EmptyState>Select a routine to inspect its schedule and run history.</EmptyState>
          )}
        </Panel>
      </div>

      {editorMode && (
        <Panel
          id="routine-editor"
          title={editorMode === "edit" ? `Edit ${selectedRoutine?.name ?? "routine"}` : "Create routine"}
          icon={editorMode === "edit" ? <Pencil size={19} /> : <Plus size={19} />}
        >
          <form className="routine-editor-form" aria-label={editorMode === "edit" ? "Edit routine" : "Create routine"} onSubmit={submitRoutine}>
            <Field label="Routine name">
              <input required maxLength={200} value={draft.name} onChange={(event) => setDraft((current) => ({ ...current, name: event.target.value }))} />
            </Field>
            <Field label="Prompt">
              <textarea required maxLength={20_000} rows={5} value={draft.prompt} onChange={(event) => setDraft((current) => ({ ...current, prompt: event.target.value }))} />
            </Field>
            <div className="field-row">
              <Field label="Schedule">
                <select value={draft.schedule_kind} onChange={(event) => setDraft((current) => ({ ...current, schedule_kind: event.target.value as "once" | "interval" | "cron" }))}>
                  <option value="once">Once</option>
                  <option value="interval">Fixed interval</option>
                  <option value="cron">Cron / calendar</option>
                </select>
              </Field>
              <Field label={`Start time (${localTimeZone})`} hint="Stored as UTC after submission.">
                <input type="datetime-local" required value={draft.start_at_local} onChange={(event) => setDraft((current) => ({ ...current, start_at_local: event.target.value }))} />
              </Field>
              {draft.schedule_kind === "interval" && (
                <Field label="Interval seconds" hint="Minimum 60 seconds.">
                  <input type="number" required min="60" max="31536000" step="1" value={draft.interval_seconds} onChange={(event) => setDraft((current) => ({ ...current, interval_seconds: event.target.value }))} />
                </Field>
              )}
              {draft.schedule_kind === "cron" && (
                <>
                  <Field label="Cron expression" hint="Five fields: minute hour day month weekday.">
                    <input required value={draft.cron_expression} onChange={(event) => setDraft((current) => ({ ...current, cron_expression: event.target.value }))} placeholder="0 9 * * 1-5" />
                  </Field>
                  <Field label="IANA timezone" hint="DST is evaluated in this named timezone.">
                    <input required maxLength={128} value={draft.timezone} onChange={(event) => setDraft((current) => ({ ...current, timezone: event.target.value }))} placeholder="America/Detroit" />
                  </Field>
                </>
              )}
              <Field label="Misfire grace seconds">
                <input type="number" required min="0" max="604800" step="1" value={draft.misfire_grace_seconds} onChange={(event) => setDraft((current) => ({ ...current, misfire_grace_seconds: event.target.value }))} />
              </Field>
            </div>
            <div className="field-row">
              <Field label="Workspace" hint="Blank uses the configured default.">
                <input maxLength={4096} value={draft.workspace} onChange={(event) => setDraft((current) => ({ ...current, workspace: event.target.value }))} />
              </Field>
              <Field label="Provider" hint="Blank uses the configured default.">
                <input maxLength={256} value={draft.provider} onChange={(event) => setDraft((current) => ({ ...current, provider: event.target.value }))} />
              </Field>
              <Field label="Model" hint="Blank uses the configured default.">
                <input maxLength={256} value={draft.model} onChange={(event) => setDraft((current) => ({ ...current, model: event.target.value }))} />
              </Field>
              <Field label="Autonomy">
                <select value={draft.autonomy_mode} onChange={(event) => setDraft((current) => ({ ...current, autonomy_mode: event.target.value }))}>
                  <option value="background">Safe Auto</option>
                  <option value="manual">Manual</option>
                  <option value="autonomous">Autopilot</option>
                </select>
              </Field>
            </div>
            <div className="field-row">
              <Field label="Delivery channel" hint="Optional configured channel id.">
                <input maxLength={128} value={draft.delivery_channel_id} onChange={(event) => setDraft((current) => ({ ...current, delivery_channel_id: event.target.value }))} placeholder="telegram" />
              </Field>
              <Field label="Delivery conversation" hint="Required when a channel is selected.">
                <input maxLength={512} value={draft.delivery_conversation_id} onChange={(event) => setDraft((current) => ({ ...current, delivery_conversation_id: event.target.value }))} placeholder="chat or webhook destination" />
              </Field>
              <Field label="Delivery template" hint="Supports {result}, {run_id}, and {run_status}.">
                <input maxLength={4000} value={draft.delivery_template} onChange={(event) => setDraft((current) => ({ ...current, delivery_template: event.target.value }))} />
              </Field>
            </div>
            <div className="page-actions">
              <button className="btn primary" type="submit" disabled={mutationPending}>{mutationPending ? "Saving…" : "Save routine"}</button>
              <button className="btn subtle" type="button" onClick={() => setEditorMode(null)} disabled={mutationPending}>Cancel</button>
            </div>
          </form>
        </Panel>
      )}
    </section>
  );
}

function emptyRoutineDraft(): RoutineDraft {
  const start = new Date(Date.now() + 60 * 60 * 1000);
  start.setSeconds(0, 0);
  return {
    name: "",
    prompt: "",
    schedule_kind: "once",
    start_at_local: localDateTimeInput(start),
    interval_seconds: "3600",
    cron_expression: "0 9 * * 1-5",
    timezone: Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC",
    delivery_channel_id: "",
    delivery_conversation_id: "",
    delivery_template: "{result}",
    workspace: "",
    provider: "",
    model: "",
    autonomy_mode: "background",
    misfire_grace_seconds: "60"
  };
}

function routineDraftFrom(routine: Routine): RoutineDraft {
  return {
    name: routine.name,
    prompt: routine.prompt,
    schedule_kind: routine.schedule_kind,
    start_at_local: localDateTimeInput(new Date(routine.start_at)),
    interval_seconds: String(routine.interval_seconds ?? 3600),
    cron_expression: routine.cron_expression ?? "0 9 * * 1-5",
    timezone: routine.timezone ?? "UTC",
    delivery_channel_id: routine.delivery && "channel_id" in routine.delivery
      ? routine.delivery.channel_id
      : "",
    delivery_conversation_id: routine.delivery && "conversation_id" in routine.delivery
      ? routine.delivery.conversation_id
      : "",
    delivery_template: routine.delivery && "template" in routine.delivery
      ? routine.delivery.template
      : "{result}",
    workspace: routine.workspace ?? "",
    provider: routine.provider ?? "",
    model: routine.model ?? "",
    autonomy_mode: routine.autonomy_mode,
    misfire_grace_seconds: String(routine.misfire_grace_seconds)
  };
}

function routinePayload(draft: RoutineDraft): Record<string, unknown> {
  const start = new Date(draft.start_at_local);
  if (Number.isNaN(start.valueOf())) throw new Error("Start time must be a valid local date and time.");
  const intervalSeconds = Number(draft.interval_seconds);
  const misfireGraceSeconds = Number(draft.misfire_grace_seconds);
  if (
    draft.schedule_kind === "interval" &&
    (!Number.isInteger(intervalSeconds) || intervalSeconds < 60 || intervalSeconds > 31_536_000)
  ) {
    throw new Error("Interval seconds must be an integer between 60 and 31536000.");
  }
  if (!Number.isInteger(misfireGraceSeconds) || misfireGraceSeconds < 0 || misfireGraceSeconds > 604_800) {
    throw new Error("Misfire grace seconds must be an integer between 0 and 604800.");
  }
  if (draft.schedule_kind === "cron" && draft.cron_expression.trim().split(/\s+/).length !== 5) {
    throw new Error("Cron expression must contain five fields.");
  }
  const deliveryChannel = draft.delivery_channel_id.trim();
  const deliveryConversation = draft.delivery_conversation_id.trim();
  if (Boolean(deliveryChannel) !== Boolean(deliveryConversation)) {
    throw new Error("Delivery channel and conversation must be configured together.");
  }
  return {
    name: draft.name.trim(),
    prompt: draft.prompt.trim(),
    schedule_kind: draft.schedule_kind,
    start_at: start.toISOString(),
    interval_seconds: draft.schedule_kind === "interval" ? intervalSeconds : null,
    cron_expression: draft.schedule_kind === "cron" ? draft.cron_expression.trim() : null,
    timezone: draft.timezone.trim() || "UTC",
    delivery: deliveryChannel
      ? {
          channel_id: deliveryChannel,
          conversation_id: deliveryConversation,
          template: draft.delivery_template.trim() || "{result}"
        }
      : null,
    workspace: draft.workspace.trim() || null,
    provider: draft.provider.trim() || null,
    model: draft.model.trim() || null,
    autonomy_mode: draft.autonomy_mode,
    misfire_grace_seconds: misfireGraceSeconds
  };
}

function localDateTimeInput(date: Date): string {
  const offset = date.getTimezoneOffset() * 60_000;
  return new Date(date.getTime() - offset).toISOString().slice(0, 16);
}

function routineScheduleLabel(routine: Routine): string {
  if (routine.schedule_kind === "cron") {
    return `${routine.cron_expression ?? "Cron"} · ${routine.timezone ?? "UTC"}`;
  }
  if (routine.schedule_kind === "interval") {
    return `Every ${formatDuration(routine.interval_seconds ?? 0)} from ${formatRoutineDate(routine.start_at)}`;
  }
  return `Once at ${formatRoutineDate(routine.start_at)}`;
}

function formatDuration(seconds: number): string {
  if (seconds > 0 && seconds % 86_400 === 0) return `${seconds / 86_400}d`;
  if (seconds > 0 && seconds % 3_600 === 0) return `${seconds / 3_600}h`;
  if (seconds > 0 && seconds % 60 === 0) return `${seconds / 60}m`;
  return `${seconds}s`;
}

function formatRoutineDate(value: string | null | undefined): string {
  if (!value) return "Not scheduled";
  const date = new Date(value);
  return Number.isNaN(date.valueOf()) ? value : date.toLocaleString();
}

function storeRunNowRequest(routineId: string, request: RoutineRunNowRequestRecord) {
  try {
    sessionStorage.setItem(`${ROUTINE_RUN_NOW_STORAGE_PREFIX}${encodeURIComponent(routineId)}`, JSON.stringify(request));
  } catch {
    // The in-memory request map still preserves retry safety when browser storage is unavailable.
  }
}

function readStoredRunNowRequest(routineId: string): RoutineRunNowRequestRecord | null {
  const key = `${ROUTINE_RUN_NOW_STORAGE_PREFIX}${encodeURIComponent(routineId)}`;
  try {
    const raw = sessionStorage.getItem(key);
    if (!raw) return null;
    const parsed = JSON.parse(raw) as Partial<RoutineRunNowRequestRecord>;
    if (
      typeof parsed.idempotencyKey !== "string" ||
      !/^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i.test(parsed.idempotencyKey) ||
      !Number.isInteger(parsed.expectedRevision) ||
      Number(parsed.expectedRevision) < 1
    ) {
      sessionStorage.removeItem(key);
      return null;
    }
    return {
      idempotencyKey: parsed.idempotencyKey,
      expectedRevision: Number(parsed.expectedRevision)
    };
  } catch {
    return null;
  }
}

function forgetRunNowRequest(requests: Map<string, RoutineRunNowRequestRecord>, routineId: string) {
  requests.delete(routineId);
  try {
    sessionStorage.removeItem(`${ROUTINE_RUN_NOW_STORAGE_PREFIX}${encodeURIComponent(routineId)}`);
  } catch {
    // The request is already removed from the active in-memory retry boundary.
  }
}

function withoutSetValue(values: Set<string>, value: string): Set<string> {
  const next = new Set(values);
  next.delete(value);
  return next;
}
