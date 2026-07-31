import {
  Brain,
  FileText,
  TestTube2,
} from "lucide-react";
import {
  type FormEvent,
  useCallback,
  useEffect,
  useRef,
  useState,
} from "react";
import { ApiAuthError, getJson, postJson, queryString } from "../api";
import {
  EmptyState,
  Field,
  InlineMeta,
  JsonBlock,
  Metric,
  Panel,
  StatusBadge,
} from "../components";
import type {
  BehaviorDeltaReport,
  ContextPackResult,
  LearningDashboard,
  MemoryHit,
  MemoryLayerStatus,
} from "../types";
import { BehaviorDeltaWorkspace } from "./BehaviorDeltaWorkspace";
import { MemoryHealth } from "./MemoryHealth";
import { MemorySearch } from "./MemorySearch";
import { PromotionHistory } from "./PromotionHistory";
import "./memory.css";

type MemoryWorkspaceOptions = {
  enabled: boolean;
  activeRunId: string | null;
  onAuthRequired: () => void;
  onError: (message: string | null) => void;
  onNotice: (message: string) => void;
};

type OptionalRead<T> = {
  data: T | null;
  error: string | null;
};

export function useMemoryWorkspace({
  enabled,
  activeRunId,
  onAuthRequired,
  onError,
  onNotice,
}: MemoryWorkspaceOptions) {
  const callbacksRef = useRef({
    onAuthRequired,
    onError,
    onNotice,
  });
  const activeRunIdRef = useRef(activeRunId);
  const refreshControllerRef = useRef<AbortController | null>(null);
  callbacksRef.current = {
    onAuthRequired,
    onError,
    onNotice,
  };
  activeRunIdRef.current = activeRunId;

  const [memoryLayers, setMemoryLayers] = useState<MemoryLayerStatus[]>([]);
  const [behaviorDeltaReport, setBehaviorDeltaReport] =
    useState<BehaviorDeltaReport | null>(null);
  const [behaviorDeltaError, setBehaviorDeltaError] = useState<string | null>(
    null,
  );
  const [learningDashboard, setLearningDashboard] =
    useState<LearningDashboard | null>(null);
  const [learningDashboardError, setLearningDashboardError] = useState<
    string | null
  >(null);
  const [lessons, setLessons] = useState<Array<Record<string, unknown>>>([]);
  const [failures, setFailures] = useState<Array<Record<string, unknown>>>([]);

  const [memoryQuery, setMemoryQuery] = useState("");
  const [memoryHits, setMemoryHits] = useState<MemoryHit[]>([]);
  const [memoryInspect, setMemoryInspect] = useState<Record<
    string,
    unknown
  > | null>(null);
  const [contextQuery, setContextQuery] = useState("");
  const [contextLayers, setContextLayers] = useState(
    "policy,self,procedural,semantic,episodic,working",
  );
  const [contextBudget, setContextBudget] = useState("6000");
  const [contextExpandRaw, setContextExpandRaw] = useState(false);
  const [contextResult, setContextResult] =
    useState<ContextPackResult | null>(null);
  const [learningTitle, setLearningTitle] = useState("");
  const [learningContent, setLearningContent] = useState("");
  const [learningKind, setLearningKind] = useState("observation");
  const [learningValidation, setLearningValidation] = useState("0.78");
  const [learningRepeat, setLearningRepeat] = useState("1");
  const [learningExplicit, setLearningExplicit] = useState(false);
  const [learningResult, setLearningResult] = useState<Record<
    string,
    unknown
  > | null>(null);
  const [capsuleResult, setCapsuleResult] = useState<Record<
    string,
    unknown
  > | null>(null);
  const [conflictResult, setConflictResult] = useState<Record<
    string,
    unknown
  > | null>(null);
  const [diagnosisText, setDiagnosisText] = useState("");
  const [diagnosisResult, setDiagnosisResult] = useState<Record<
    string,
    unknown
  > | null>(null);

  const reportError = useCallback((value: unknown) => {
    if (value instanceof ApiAuthError) {
      callbacksRef.current.onAuthRequired();
      return;
    }
    callbacksRef.current.onError(errorMessage(value));
  }, []);

  const refresh = useCallback(
    async () => {
      refreshControllerRef.current?.abort();
      const controller = new AbortController();
      refreshControllerRef.current = controller;
      const { signal } = controller;
      try {
        const [
          layerList,
          lessonList,
          failureList,
          deltaRead,
          dashboardRead,
        ] = await Promise.all([
          getJson<MemoryLayerStatus[]>("/api/memory/layers", { signal }),
          getJson<{ items: Array<Record<string, unknown>> }>(
            "/api/cognition/lessons?k=20",
            { signal },
          ),
          getJson<{ items: Array<Record<string, unknown>> }>(
            "/api/cognition/failures?k=20",
            { signal },
          ),
          optionalRead<BehaviorDeltaReport>(
            "/api/memory/deltas?since=all",
            signal,
          ),
          optionalRead<LearningDashboard>(
            "/api/learning/dashboard?since=all",
            signal,
          ),
        ]);
        if (signal?.aborted) return;
        setMemoryLayers(layerList);
        setLessons(lessonList.items);
        setFailures(failureList.items);
        setBehaviorDeltaError(deltaRead.error);
        setLearningDashboardError(dashboardRead.error);
        if (deltaRead.data) setBehaviorDeltaReport(deltaRead.data);
        if (dashboardRead.data) setLearningDashboard(dashboardRead.data);
      } catch (value) {
        if (signal?.aborted) return;
        reportError(value);
      } finally {
        if (refreshControllerRef.current === controller) {
          refreshControllerRef.current = null;
        }
      }
    },
    [reportError],
  );

  useEffect(() => {
    if (!enabled) {
      refreshControllerRef.current?.abort();
      return;
    }
    void refresh();
    const timer = window.setInterval(() => {
      void refresh();
    }, 3_500);
    return () => {
      window.clearInterval(timer);
      refreshControllerRef.current?.abort();
    };
  }, [enabled, refresh]);

  const guarded = useCallback(
    async (action: () => Promise<void>, success?: string) => {
      callbacksRef.current.onError(null);
      try {
        await action();
        if (success) callbacksRef.current.onNotice(success);
      } catch (value) {
        reportError(value);
      }
    },
    [reportError],
  );

  async function searchMemory(event?: FormEvent) {
    event?.preventDefault();
    await guarded(async () => {
      if (!memoryQuery.trim()) return;
      const params = queryString({ query: memoryQuery, k: 12 });
      const hits = await getJson<MemoryHit[]>(`/api/memory/search${params}`);
      const inspected = await getJson<Record<string, unknown>>(
        `/api/memory/inspect${params}`,
      );
      setMemoryHits(hits);
      setMemoryInspect(inspected);
    });
  }

  async function packContext(event: FormEvent) {
    event.preventDefault();
    await guarded(async () => {
      const query = contextQuery.trim() || memoryQuery.trim();
      if (!query) return;
      const params = queryString({
        query,
        token_budget: contextBudget,
        layers: contextLayers,
        expand_raw: contextExpandRaw,
        include_telemetry: true,
      });
      setContextResult(
        await getJson<ContextPackResult>(`/api/context${params}`),
      );
    });
  }

  async function findConflicts() {
    await guarded(async () => {
      const query = contextQuery.trim() || memoryQuery.trim();
      if (!query) return;
      setConflictResult(
        await getJson<Record<string, unknown>>(
          `/api/memory/conflicts${queryString({ query, k: 8 })}`,
        ),
      );
    });
  }

  async function submitLearning(event: FormEvent) {
    event.preventDefault();
    await guarded(async () => {
      const result = await postJson<Record<string, unknown>>(
        "/api/memory/learn",
        {
          title: learningTitle,
          content: learningContent,
          kind: learningKind,
          validation_score: Number(learningValidation),
          repeat_count: Number(learningRepeat),
          explicit_instruction: learningExplicit,
        },
      );
      setLearningResult(result);
      await refresh();
    }, "Learning signal reviewed.");
  }

  async function capsule(action: "summarize" | "apply") {
    const runId = activeRunIdRef.current;
    if (!runId) return;
    await guarded(async () => {
      const result = await postJson<Record<string, unknown>>(
        `/api/capsules/${runId}/${action}`,
        {
          dry_run: action === "summarize",
          include_policy: false,
        },
      );
      setCapsuleResult(result);
      await refresh();
    });
  }

  async function diagnose(event: FormEvent) {
    event.preventDefault();
    await guarded(async () => {
      const result = await postJson<Record<string, unknown>>(
        "/api/diagnosis/recall",
        {
          failure_text: diagnosisText,
          source: "web-ui",
          k: 5,
        },
      );
      setDiagnosisResult(result);
    });
  }

  return {
    activeDeltaCount: behaviorDeltaReport?.summary.active_deltas ?? 0,
    totalDeltaCount: behaviorDeltaReport?.summary.total_deltas ?? 0,
    behaviorDeltaError,
    behaviorDeltaReport,
    capsule,
    capsuleResult,
    conflictResult,
    contextBudget,
    contextExpandRaw,
    contextLayers,
    contextQuery,
    contextResult,
    diagnose,
    diagnosisResult,
    diagnosisText,
    failures,
    findConflicts,
    hasActiveRun: Boolean(activeRunId),
    learningContent,
    learningDashboard,
    learningDashboardError,
    learningExplicit,
    learningKind,
    learningRepeat,
    learningResult,
    learningTitle,
    learningValidation,
    lessons,
    memoryHits,
    memoryInspect,
    memoryLayers,
    memoryQuery,
    packContext,
    refresh,
    searchMemory,
    setContextBudget,
    setContextExpandRaw,
    setContextLayers,
    setContextQuery,
    setDiagnosisText,
    setLearningContent,
    setLearningExplicit,
    setLearningKind,
    setLearningRepeat,
    setLearningTitle,
    setLearningValidation,
    setMemoryQuery,
    submitLearning,
  };
}

export type MemoryWorkspaceController = ReturnType<typeof useMemoryWorkspace>;

export function MemoryWorkspace({
  controller,
}: {
  controller: MemoryWorkspaceController;
}) {
  const {
    behaviorDeltaError,
    behaviorDeltaReport,
    capsule,
    capsuleResult,
    conflictResult,
    contextBudget,
    contextExpandRaw,
    contextLayers,
    contextQuery,
    contextResult,
    diagnose,
    diagnosisResult,
    diagnosisText,
    failures,
    findConflicts,
    hasActiveRun,
    learningContent,
    learningDashboard,
    learningDashboardError,
    learningExplicit,
    learningKind,
    learningRepeat,
    learningResult,
    learningTitle,
    learningValidation,
    lessons,
    memoryHits,
    memoryInspect,
    memoryLayers,
    memoryQuery,
    packContext,
    searchMemory,
    setContextBudget,
    setContextExpandRaw,
    setContextLayers,
    setContextQuery,
    setDiagnosisText,
    setLearningContent,
    setLearningExplicit,
    setLearningKind,
    setLearningRepeat,
    setLearningTitle,
    setLearningValidation,
    setMemoryQuery,
    submitLearning,
  } = controller;

  return (
    <section id="memory" className="content-grid wide-left">
      <MemoryHealth layers={memoryLayers} />
      <MemorySearch
        memoryQuery={memoryQuery}
        memoryHits={memoryHits}
        memoryInspect={memoryInspect}
        onQueryChange={setMemoryQuery}
        onSearch={searchMemory}
      />

      <Panel title="Context Pack" icon={<FileText size={19} />}>
        <form onSubmit={packContext} className="stack-form">
          <Field label="Objective or claim">
            <input
              value={contextQuery}
              onChange={(event) => setContextQuery(event.target.value)}
            />
          </Field>
          <Field label="Layers CSV">
            <input
              value={contextLayers}
              onChange={(event) => setContextLayers(event.target.value)}
            />
          </Field>
          <Field label="Token budget">
            <input
              value={contextBudget}
              onChange={(event) => setContextBudget(event.target.value)}
              inputMode="numeric"
            />
          </Field>
          <label className="check-row">
            <input
              type="checkbox"
              checked={contextExpandRaw}
              onChange={(event) => setContextExpandRaw(event.target.checked)}
            />
            <span>Expand raw evidence</span>
          </label>
          <div className="page-actions">
            <button type="submit">Pack</button>
            <button type="button" onClick={findConflicts}>
              Find Conflicts
            </button>
            <button
              type="button"
              disabled={!hasActiveRun}
              onClick={() => capsule("summarize")}
            >
              Capsule Preview
            </button>
            <button
              type="button"
              disabled={!hasActiveRun}
              onClick={() => capsule("apply")}
            >
              Request Capsule Apply
            </button>
          </div>
        </form>
        {contextResult && (
          <JsonBlock
            value={contextResult.packed_prompt || contextResult}
            maxHeight="360px"
          />
        )}
        {conflictResult && <JsonBlock value={conflictResult} />}
        {capsuleResult && <JsonBlock value={capsuleResult} />}
      </Panel>

      <Panel title="Learning Review" icon={<Brain size={19} />}>
        <form onSubmit={submitLearning} className="stack-form">
          <Field label="Title">
            <input
              value={learningTitle}
              onChange={(event) => setLearningTitle(event.target.value)}
            />
          </Field>
          <Field label="Validated content">
            <textarea
              value={learningContent}
              onChange={(event) => setLearningContent(event.target.value)}
              rows={4}
            />
          </Field>
          <div className="field-row">
            <Field label="Kind">
              <select
                value={learningKind}
                onChange={(event) => setLearningKind(event.target.value)}
              >
                {[
                  "observation",
                  "fact",
                  "event",
                  "failure",
                  "procedure",
                  "policy",
                ].map((kind) => (
                  <option key={kind} value={kind}>
                    {kind}
                  </option>
                ))}
              </select>
            </Field>
            <Field label="Validation score">
              <input
                value={learningValidation}
                onChange={(event) => setLearningValidation(event.target.value)}
                inputMode="decimal"
              />
            </Field>
            <Field label="Repeat count">
              <input
                value={learningRepeat}
                onChange={(event) => setLearningRepeat(event.target.value)}
                inputMode="numeric"
              />
            </Field>
          </div>
          <label className="check-row">
            <input
              type="checkbox"
              checked={learningExplicit}
              onChange={(event) => setLearningExplicit(event.target.checked)}
            />
            <span>Explicit instruction</span>
          </label>
          <button type="submit">Review Learning Signal</button>
        </form>
        {learningResult && <JsonBlock value={learningResult} />}
      </Panel>

      <BehaviorDeltaWorkspace
        report={behaviorDeltaReport}
        error={behaviorDeltaError}
      />
      <PromotionHistory
        dashboard={learningDashboard}
        error={learningDashboardError}
      />

      <Panel title="Lessons & Failures" icon={<TestTube2 size={19} />}>
        <h3>Lessons</h3>
        <RecordList records={lessons} />
        <h3>Failure Episodes</h3>
        <RecordList records={failures} />
        <form onSubmit={diagnose} className="stack-form">
          <Field label="Diagnose failure text">
            <textarea
              value={diagnosisText}
              onChange={(event) => setDiagnosisText(event.target.value)}
              rows={4}
            />
          </Field>
          <button type="submit">Classify & Recall Lessons</button>
        </form>
        {diagnosisResult && <JsonBlock value={diagnosisResult} />}
      </Panel>
    </section>
  );
}

async function optionalRead<T>(
  path: string,
  signal?: AbortSignal,
): Promise<OptionalRead<T>> {
  try {
    return {
      data: await getJson<T>(path, { signal }),
      error: null,
    };
  } catch (value) {
    if (value instanceof ApiAuthError || signal?.aborted) throw value;
    return {
      data: null,
      error: errorMessage(value),
    };
  }
}

function RecordList({
  records,
}: {
  records: Array<Record<string, unknown>>;
}) {
  if (records.length === 0) {
    return <EmptyState>No records found.</EmptyState>;
  }
  return (
    <div className="list">
      {records.slice(0, 8).map((item, index) => {
        const record = item.record as Record<string, unknown> | undefined;
        return (
          <div
            className="data-row"
            key={`${String(record?.id ?? "record")}-${index}`}
          >
            <strong>{String(record?.title ?? item.title ?? "Record")}</strong>
            <InlineMeta
              items={[
                String(record?.layer ?? ""),
                String(record?.kind ?? ""),
                scoreLabel(item.score),
              ]}
            />
            <p>
              {String(record?.content ?? item.snippet ?? "").slice(0, 360)}
            </p>
          </div>
        );
      })}
    </div>
  );
}

function errorMessage(value: unknown): string {
  return value instanceof Error ? value.message : String(value);
}

function scoreLabel(value: unknown): string {
  return typeof value === "number" ? value.toFixed(2) : "";
}

function formatPercent(value: number): string {
  if (!Number.isFinite(value)) return "0%";
  return `${Math.round(value * 100)}%`;
}
