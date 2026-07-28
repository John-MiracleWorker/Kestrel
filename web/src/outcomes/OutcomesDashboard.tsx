import {
  BarChart3,
  Download,
  FlaskConical,
  LoaderCircle,
  Play,
  Plus,
  RefreshCw,
  TrendingUp
} from "lucide-react";
import { useCallback, useEffect, useMemo, useState } from "react";
import { getJson, postJson, queryString } from "../api";
import { StatusBadge } from "../components";
import "./outcomes.css";

type Metric = {
  value: number | null;
  sample_count: number;
  population: number;
  coverage: number;
  missing: boolean;
};

type Coverage = {
  covered: number;
  total: number;
  rate: number | null;
  missing: boolean;
};

type OutcomeReport = {
  schema: string;
  generated_at: string;
  filters: Record<string, unknown>;
  summary: Record<string, Metric>;
  groups: OutcomeGroup[];
  baselines: Baseline[];
  evidence_coverage: Record<string, Coverage>;
};

type OutcomeGroup = {
  project_id: string | null;
  task_family: string;
  risk: string;
  provider: string;
  model: string;
  policy_id: string;
  attempt_count: number;
  validated_completion_rate: Metric;
  actual_cost_usd: Metric;
  median_latency_seconds: Metric;
  retry_count: number;
  route_regret_usd: Metric;
  estimated_savings_usd: Metric;
};

type Baseline = {
  baseline: string;
  available: boolean;
  sample_count: number;
  validated_completion_rate: Metric;
  validated_success_per_dollar: Metric;
  inference: string;
};

type Benchmark = {
  case_id: string;
  project_id: string;
  name: string;
  task_family: string;
  risk: string;
  acceptance_criteria: string[];
  case_digest: string;
  status: string;
};

type ProjectChoice = {
  project_id: string;
  display_name: string;
};

const SUMMARY_METRICS: Array<{
  key: string;
  label: string;
  format: "number" | "percent" | "currency" | "seconds";
}> = [
  { key: "run_count", label: "Runs", format: "number" },
  { key: "validated_completion_rate", label: "Validated completion", format: "percent" },
  { key: "validated_success_per_dollar", label: "Validated success / $", format: "number" },
  { key: "validated_success_per_minute", label: "Validated success / min", format: "number" },
  { key: "actual_cost_usd", label: "Attributed cost", format: "currency" },
  { key: "median_run_seconds", label: "Median run", format: "seconds" },
  { key: "human_interventions", label: "Human interventions", format: "number" },
  { key: "median_approval_wait_seconds", label: "Approval wait", format: "seconds" },
  { key: "patch_acceptance_count", label: "Accepted patches", format: "number" },
  { key: "rollback_count", label: "Rollbacks", format: "number" },
  { key: "browser_validation_pass_rate", label: "Browser proof pass", format: "percent" }
];

export function OutcomesDashboard({
  onBack
}: {
  onBack: () => void;
}) {
  const [projects, setProjects] = useState<ProjectChoice[]>([]);
  const [projectId, setProjectId] = useState("");
  const [windowName, setWindowName] = useState("30d");
  const [report, setReport] = useState<OutcomeReport | null>(null);
  const [benchmarks, setBenchmarks] = useState<Benchmark[]>([]);
  const [pending, setPending] = useState(true);
  const [actionPending, setActionPending] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [newBenchmarkOpen, setNewBenchmarkOpen] = useState(false);
  const [benchmarkName, setBenchmarkName] = useState("");
  const [benchmarkFamily, setBenchmarkFamily] = useState("engineering");
  const [benchmarkRisk, setBenchmarkRisk] = useState("low");
  const [benchmarkObjective, setBenchmarkObjective] = useState("");
  const [benchmarkCriteria, setBenchmarkCriteria] = useState("");

  const since = useMemo(() => sinceForWindow(windowName), [windowName]);

  const refresh = useCallback(async (signal?: AbortSignal) => {
    setPending(true);
    setError(null);
    try {
      const projectQuery = queryString({ project_id: projectId || null });
      const outcomeQuery = queryString({
        project_id: projectId || null,
        since
      });
      const [projectResponse, outcomeResponse, benchmarkResponse] = await Promise.all([
        getJson<{ items: ProjectChoice[] }>("/api/projects", { signal }),
        getJson<OutcomeReport>(`/api/outcomes${outcomeQuery}`, { signal }),
        getJson<{ items: Benchmark[] }>(`/api/benchmarks${projectQuery}`, { signal })
      ]);
      setProjects(Array.isArray(projectResponse.items) ? projectResponse.items : []);
      setReport(outcomeResponse);
      setBenchmarks(Array.isArray(benchmarkResponse.items) ? benchmarkResponse.items : []);
    } catch (value) {
      if (!signal?.aborted) setError(messageFor(value));
    } finally {
      if (!signal?.aborted) setPending(false);
    }
  }, [projectId, since]);

  useEffect(() => {
    const controller = new AbortController();
    void refresh(controller.signal);
    return () => controller.abort();
  }, [refresh]);

  async function createBenchmark() {
    if (!projectId || !benchmarkName.trim() || !benchmarkObjective.trim()) return;
    const criteria = benchmarkCriteria
      .split("\n")
      .map((item) => item.trim())
      .filter(Boolean);
    if (criteria.length === 0) return;
    setActionPending("create-benchmark");
    setError(null);
    try {
      const created = await postJson<Benchmark>("/api/benchmarks", {
        case_id: `benchmark_${crypto.randomUUID().replaceAll("-", "")}`,
        project_id: projectId,
        name: benchmarkName.trim(),
        task_family: benchmarkFamily.trim(),
        risk: benchmarkRisk,
        fixture: { objective: benchmarkObjective.trim(), redacted: true },
        acceptance_criteria: criteria
      });
      setBenchmarks((current) => [...current, created]);
      setNewBenchmarkOpen(false);
      setBenchmarkName("");
      setBenchmarkObjective("");
      setBenchmarkCriteria("");
    } catch (value) {
      setError(messageFor(value));
    } finally {
      setActionPending("");
    }
  }

  async function replayBenchmark(benchmark: Benchmark) {
    setActionPending(`replay:${benchmark.case_id}`);
    setError(null);
    try {
      await postJson(`/api/benchmarks/${encodeURIComponent(benchmark.case_id)}/replays`, {
        replay_id: `replay_${crypto.randomUUID().replaceAll("-", "")}`,
        launch: true,
        route_policy_id: null,
        context_strategy: "project_default",
        baseline: "live",
        existing_run_id: null
      });
      await refresh();
    } catch (value) {
      setError(messageFor(value));
    } finally {
      setActionPending("");
    }
  }

  async function exportReport() {
    setActionPending("export");
    setError(null);
    try {
      const payload = await getJson<Record<string, unknown>>(
        `/api/outcomes/export${queryString({ project_id: projectId || null, since })}`
      );
      const blob = new Blob([JSON.stringify(payload, null, 2)], {
        type: "application/json"
      });
      const url = URL.createObjectURL(blob);
      const anchor = document.createElement("a");
      anchor.href = url;
      anchor.download = `kestrel-outcomes-${new Date().toISOString().slice(0, 10)}.json`;
      anchor.click();
      URL.revokeObjectURL(url);
    } catch (value) {
      setError(messageFor(value));
    } finally {
      setActionPending("");
    }
  }

  return (
    <section className="outcomes-page" id="outcomes" aria-label="Outcome analytics and private benchmarks">
      <header className="outcomes-header">
        <div>
          <span className="eyebrow">Measured usefulness</span>
          <h1>Outcomes and benchmarks</h1>
          <p>Compare validated completion, cost, time, intervention, and routing evidence without changing live policy.</p>
        </div>
        <div className="outcomes-header-actions">
          <button type="button" onClick={onBack}>Back to mission</button>
          <button type="button" onClick={() => void exportReport()} disabled={actionPending === "export"}>
            <Download size={15} /> Export redacted report
          </button>
        </div>
      </header>

      <section className="outcomes-filters" aria-label="Outcome filters">
        <label>
          Project
          <select value={projectId} onChange={(event) => setProjectId(event.target.value)}>
            <option value="">All projects</option>
            {projects.map((project) => (
              <option key={project.project_id} value={project.project_id}>{project.display_name}</option>
            ))}
          </select>
        </label>
        <label>
          Time window
          <select value={windowName} onChange={(event) => setWindowName(event.target.value)}>
            <option value="7d">Last 7 days</option>
            <option value="30d">Last 30 days</option>
            <option value="90d">Last 90 days</option>
            <option value="all">All evidence</option>
          </select>
        </label>
        <button type="button" onClick={() => void refresh()} disabled={pending}>
          <RefreshCw className={pending ? "spin" : ""} size={15} /> Refresh
        </button>
      </section>

      {error ? <div className="outcomes-error" role="alert">{error}</div> : null}
      {pending && !report ? (
        <div className="outcomes-loading"><LoaderCircle className="spin" /> Loading evidence…</div>
      ) : null}

      {report ? (
        <>
          <section className="outcomes-metrics" aria-label="Outcome summary">
            {SUMMARY_METRICS.map((definition) => (
              <MetricCard
                key={definition.key}
                label={definition.label}
                metric={report.summary[definition.key]}
                format={definition.format}
              />
            ))}
          </section>

          <section className="outcomes-grid">
            <article className="outcomes-panel">
              <header><BarChart3 size={17} /><h2>Evidence coverage</h2></header>
              <div className="coverage-list">
                {Object.entries(report.evidence_coverage).map(([name, coverage]) => (
                  <div key={name}>
                    <span>{title(name)}</span>
                    <strong>{coverage.missing ? "Missing" : percent(coverage.rate)}</strong>
                    <small>{coverage.covered} / {coverage.total}</small>
                  </div>
                ))}
              </div>
            </article>

            <article className="outcomes-panel">
              <header><TrendingUp size={17} /><h2>Observed baselines</h2></header>
              <div className="baseline-list">
                {report.baselines.map((baseline) => (
                  <div key={baseline.baseline}>
                    <span>{title(baseline.baseline)}</span>
                    <StatusBadge value={baseline.available ? "available" : "sparse evidence"} />
                    <strong>{metricValue(baseline.validated_completion_rate, "percent")}</strong>
                    <small>{baseline.sample_count} samples · historical shadow comparison</small>
                  </div>
                ))}
              </div>
            </article>
          </section>

          <section className="outcomes-panel outcomes-groups">
            <header><BarChart3 size={17} /><h2>Task-family and route results</h2></header>
            {report.groups.length === 0 ? (
              <p>No routed outcomes match these filters.</p>
            ) : (
              <div className="outcomes-table-wrap">
                <table>
                  <thead>
                    <tr>
                      <th>Family</th>
                      <th>Target</th>
                      <th>Policy</th>
                      <th>Attempts</th>
                      <th>Validated</th>
                      <th>Cost</th>
                      <th>Latency</th>
                      <th>Regret</th>
                      <th>Savings</th>
                    </tr>
                  </thead>
                  <tbody>
                    {report.groups.map((group) => (
                      <tr key={`${group.task_family}:${group.provider}:${group.model}:${group.policy_id}`}>
                        <td><strong>{group.task_family}</strong><small>{group.risk}</small></td>
                        <td>{group.provider}<small>{group.model}</small></td>
                        <td>{group.policy_id}</td>
                        <td>{group.attempt_count}</td>
                        <td>{metricValue(group.validated_completion_rate, "percent")}</td>
                        <td>{metricValue(group.actual_cost_usd, "currency")}</td>
                        <td>{metricValue(group.median_latency_seconds, "seconds")}</td>
                        <td>{metricValue(group.route_regret_usd, "currency")}</td>
                        <td>{metricValue(group.estimated_savings_usd, "currency")}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </section>
        </>
      ) : null}

      <section className="outcomes-panel benchmarks-panel">
        <header>
          <FlaskConical size={17} />
          <h2>Private benchmark cases</h2>
          <button
            type="button"
            onClick={() => setNewBenchmarkOpen((current) => !current)}
            disabled={!projectId}
            title={projectId ? "Create a redacted private benchmark" : "Choose one project first"}
          >
            <Plus size={14} /> New case
          </button>
        </header>
        {newBenchmarkOpen ? (
          <div className="benchmark-form">
            <label>Name<input value={benchmarkName} onChange={(event) => setBenchmarkName(event.target.value)} /></label>
            <label>Task family<input value={benchmarkFamily} onChange={(event) => setBenchmarkFamily(event.target.value)} /></label>
            <label>
              Risk
              <select value={benchmarkRisk} onChange={(event) => setBenchmarkRisk(event.target.value)}>
                <option value="low">Low</option>
                <option value="medium">Medium</option>
                <option value="high">High</option>
              </select>
            </label>
            <label className="benchmark-wide">
              Redacted objective
              <textarea rows={3} value={benchmarkObjective} onChange={(event) => setBenchmarkObjective(event.target.value)} />
            </label>
            <label className="benchmark-wide">
              Acceptance criteria, one per line
              <textarea rows={4} value={benchmarkCriteria} onChange={(event) => setBenchmarkCriteria(event.target.value)} />
            </label>
            <button
              type="button"
              onClick={() => void createBenchmark()}
              disabled={actionPending === "create-benchmark"}
            >
              Save private case
            </button>
          </div>
        ) : null}
        <div className="benchmark-list">
          {benchmarks.map((benchmark) => (
            <article key={benchmark.case_id}>
              <div>
                <strong>{benchmark.name}</strong>
                <StatusBadge value={benchmark.status} />
              </div>
              <p>{benchmark.task_family} · {benchmark.risk} risk · {benchmark.acceptance_criteria.length} criteria</p>
              <small>Digest {benchmark.case_digest.slice(0, 12)} · fixture remains project scoped and redacted</small>
              <button
                type="button"
                onClick={() => void replayBenchmark(benchmark)}
                disabled={actionPending === `replay:${benchmark.case_id}`}
              >
                <Play size={14} /> Replay
              </button>
            </article>
          ))}
          {benchmarks.length === 0 ? <p>No private benchmark cases match this project.</p> : null}
        </div>
      </section>
    </section>
  );
}

function MetricCard({
  label,
  metric,
  format
}: {
  label: string;
  metric: Metric | undefined;
  format: "number" | "percent" | "currency" | "seconds";
}) {
  const available = metric && !metric.missing;
  return (
    <article className={available ? "outcome-metric" : "outcome-metric missing"}>
      <span>{label}</span>
      <strong>{metric ? metricValue(metric, format) : "Missing"}</strong>
      <small>
        {metric
          ? `${metric.sample_count}/${metric.population} evidence · ${Math.round(metric.coverage * 100)}% coverage`
          : "No evidence field returned"}
      </small>
    </article>
  );
}

function metricValue(
  metric: Metric,
  format: "number" | "percent" | "currency" | "seconds"
): string {
  if (metric.missing || metric.value === null) return "Missing";
  if (format === "percent") return percent(metric.value);
  if (format === "currency") return `$${metric.value.toFixed(4)}`;
  if (format === "seconds") return duration(metric.value);
  return Number.isInteger(metric.value) ? String(metric.value) : metric.value.toFixed(3);
}

function percent(value: number | null): string {
  return value === null ? "Missing" : `${(value * 100).toFixed(1)}%`;
}

function duration(seconds: number): string {
  if (seconds < 60) return `${seconds.toFixed(1)}s`;
  return `${(seconds / 60).toFixed(1)}m`;
}

function sinceForWindow(value: string): string | null {
  if (value === "all") return null;
  const days = Number.parseInt(value, 10);
  return new Date(Date.now() - days * 86_400_000).toISOString();
}

function title(value: string): string {
  return value.replaceAll("_", " ").replace(/\b\w/g, (character) => character.toUpperCase());
}

function messageFor(value: unknown): string {
  return value instanceof Error ? value.message : String(value);
}
