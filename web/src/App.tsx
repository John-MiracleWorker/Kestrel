import {
  Activity,
  Bell,
  BookOpen,
  Bot,
  Brain,
  Check,
  ClipboardList,
  Database,
  Feather,
  FileText,
  GitBranch,
  Home,
  Layers,
  LineChart,
  Network,
  PlugZap,
  RotateCw,
  ScrollText,
  Search,
  Send,
  ServerCog,
  Settings,
  ShieldCheck,
  Sparkles,
  Square,
  TerminalSquare,
  UserRound,
  Wrench,
  X
} from "lucide-react";
import { FormEvent, useEffect, useMemo, useState } from "react";

type Run = {
  run_id: string;
  status: string;
  message: string;
  session_id: string;
  assistant_message: string;
  tool_count: number;
  context_chars: number;
  stop_reason: string;
  error?: string | null;
  approvals?: Approval[];
};

type Session = {
  session_id: string;
  run_count: number;
  status_counts: Record<string, number>;
  latest_run_id: string;
  latest_status: string;
  latest_message: string;
  created_at: string;
  updated_at: string;
};

type Approval = {
  approval_id: string;
  run_id: string;
  tool_name: string;
  arguments: Record<string, unknown>;
  risk: string;
  status: string;
};

type Tool = {
  name: string;
  description: string;
  parameters?: Record<string, unknown>;
  risk: string;
  requires_approval: boolean;
  source: string;
  server_id?: string | null;
};

type McpTool = Tool & {
  remote_name?: string;
  capabilities?: string[];
};

type MemoryHit = {
  layer: string;
  kind: string;
  title: string;
  score: number;
  snippet: string;
  record_id?: string;
};

type ContextPackResult = {
  packed_prompt?: string;
  token_estimate?: number;
  selected_item_count?: number;
  selected_layers?: string[];
  conflict_warnings?: string[];
  evidence_refs?: string[];
  telemetry?: Record<string, unknown>;
};

type MemoryVerifyResult = Record<string, boolean>;

type TraceEvent = {
  id: number;
  run_id: string;
  type: string;
  payload: Record<string, unknown>;
  created_at: string;
};

type RunTrace = {
  run: Run;
  summary: {
    event_count: number;
    first_event_at: string | null;
    last_event_at: string | null;
    trace_counts: Record<string, number>;
  };
  timeline: TraceEvent[];
  traces: Record<string, TraceEvent[]>;
};

type AgentLogEvent = {
  id: string;
  type: string;
  payload: Record<string, unknown>;
  created_at: string;
};

type McpServer = {
  id: string;
  name: string;
  transport: string;
  status: string;
  session_state?: string;
  enabled: boolean;
  tools: McpTool[];
  tool_count?: number;
  last_seen_at?: string | null;
  last_call_at?: string | null;
  last_error_at?: string | null;
  failure_count?: number;
  last_latency_ms?: number | null;
  risk_policy?: string;
  error?: string | null;
};

type Skill = {
  id: string;
  name: string;
  description: string;
  enabled: boolean;
};

type EventRow = {
  id: number;
  type: string;
  payload: Record<string, unknown>;
};

type TaskGraph = {
  tasks: Array<{ task_id: string; title: string; goal: string; profile: string; status: string; approved: boolean }>;
  subagents: Array<{ subagent_id: string; profile: string; goal: string; status: string; result: string; error?: string | null }>;
};

const api = {
  async get<T>(path: string): Promise<T> {
    const response = await fetch(path);
    if (!response.ok) throw new Error(await response.text());
    return response.json();
  },
  async post<T>(path: string, body: unknown = {}): Promise<T> {
    const response = await fetch(path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body)
    });
    if (!response.ok) throw new Error(await response.text());
    return response.json();
  },
  async delete<T>(path: string): Promise<T> {
    const response = await fetch(path, { method: "DELETE" });
    if (!response.ok) throw new Error(await response.text());
    return response.json();
  }
};

function asCount(value: number): string {
  return new Intl.NumberFormat("en", { notation: value >= 1000 ? "compact" : "standard" }).format(value);
}

function percent(value: number, total: number): number {
  if (total <= 0) return 0;
  return Math.round((value / total) * 100);
}

function summarizePayload(payload: Record<string, unknown>): string {
  const direct = payload.message ?? payload.error ?? payload.tool_name ?? payload.run_id ?? payload.session_id;
  if (typeof direct === "string" && direct.trim()) return direct;
  return JSON.stringify(payload).slice(0, 96);
}

function relativeTime(value: string | null | undefined): string {
  if (!value) return "just now";
  const time = new Date(value).getTime();
  if (Number.isNaN(time)) return "just now";
  const seconds = Math.max(0, Math.floor((Date.now() - time) / 1000));
  if (seconds < 60) return `${seconds}s ago`;
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes}m ago`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours}h ago`;
  return `${Math.floor(hours / 24)}d ago`;
}

export function App() {
  const [message, setMessage] = useState("");
  const [runs, setRuns] = useState<Run[]>([]);
  const [sessions, setSessions] = useState<Session[]>([]);
  const [activeRunId, setActiveRunId] = useState<string | null>(null);
  const [events, setEvents] = useState<EventRow[]>([]);
  const [tools, setTools] = useState<Tool[]>([]);
  const [approvals, setApprovals] = useState<Approval[]>([]);
  const [memoryQuery, setMemoryQuery] = useState("");
  const [memoryHits, setMemoryHits] = useState<MemoryHit[]>([]);
  const [memoryVerify, setMemoryVerify] = useState<MemoryVerifyResult | null>(null);
  const [learningTitle, setLearningTitle] = useState("");
  const [learningContent, setLearningContent] = useState("");
  const [learningKind, setLearningKind] = useState("observation");
  const [learningValidation, setLearningValidation] = useState("0.78");
  const [learningRepeat, setLearningRepeat] = useState("1");
  const [learningExplicit, setLearningExplicit] = useState(false);
  const [learningResult, setLearningResult] = useState<Record<string, unknown> | null>(null);
  const [contextQuery, setContextQuery] = useState("");
  const [contextPackResult, setContextPackResult] = useState<ContextPackResult | null>(null);
  const [contextLayers, setContextLayers] = useState("policy,procedural,semantic,episodic,working");
  const [contextBudget, setContextBudget] = useState("6000");
  const [contextExpandRaw, setContextExpandRaw] = useState(false);
  const [conflictResult, setConflictResult] = useState<Record<string, unknown> | null>(null);
  const [capsuleResult, setCapsuleResult] = useState<Record<string, unknown> | null>(null);
  const [mcpServers, setMcpServers] = useState<McpServer[]>([]);
  const [mcpId, setMcpId] = useState("");
  const [mcpCommand, setMcpCommand] = useState("");
  const [mcpTransport, setMcpTransport] = useState("stdio");
  const [mcpToolSelection, setMcpToolSelection] = useState("");
  const [mcpArguments, setMcpArguments] = useState("{}");
  const [mcpInvokeResult, setMcpInvokeResult] = useState<Record<string, unknown> | null>(null);
  const [skills, setSkills] = useState<Skill[]>([]);
  const [taskGraph, setTaskGraph] = useState<TaskGraph | null>(null);
  const [subagentProfile, setSubagentProfile] = useState("worker");
  const [subagentGoal, setSubagentGoal] = useState("");
  const [runTrace, setRunTrace] = useState<RunTrace | null>(null);
  const [logs, setLogs] = useState<AgentLogEvent[]>([]);
  const activeRun = useMemo(() => runs.find((run) => run.run_id === activeRunId) ?? runs[0], [runs, activeRunId]);
  const streamedAssistant = useMemo(
    () =>
      events
        .filter((event) => event.type === "assistant.token")
        .map((event) => String(event.payload.content ?? ""))
        .join(""),
    [events]
  );
  const mcpToolOptions = useMemo(
    () =>
      mcpServers.flatMap((server) =>
        server.tools.map((tool) => ({
          server,
          tool,
          value: `${server.id}::${tool.remote_name ?? tool.name}`
        }))
      ),
    [mcpServers]
  );
  const activeAgents = Math.max(skills.filter((skill) => skill.enabled).length, taskGraph?.subagents.length ?? 0, 1);
  const completedRuns = runs.filter((run) => run.status === "completed").length;
  const runningRuns = runs.filter((run) => run.status === "running").length;
  const failedRuns = runs.filter((run) => run.status === "failed").length;
  const blockedRuns = runs.filter((run) => run.status === "blocked").length;
  const queuedTasks = approvals.length + blockedRuns;
  const connectedServers = mcpServers.filter(
    (server) => server.enabled && (server.session_state === "connected" || server.status === "connected")
  ).length;
  const systemHealthy = failedRuns === 0 && mcpServers.every((server) => !server.error);
  const statusTotal = runs.length + approvals.length;
  const statusSlices = useMemo(() => {
    if (statusTotal === 0) {
      return {
        completed: 67,
        running: 22,
        queued: 8,
        failed: 3
      };
    }
    return {
      completed: percent(completedRuns, statusTotal),
      running: percent(runningRuns, statusTotal),
      queued: percent(queuedTasks, statusTotal),
      failed: percent(failedRuns, statusTotal)
    };
  }, [completedRuns, failedRuns, queuedTasks, runningRuns, statusTotal]);
  const statusDonutStyle = {
    background: `conic-gradient(#34d5b2 0 ${statusSlices.completed}%, #6575ff ${statusSlices.completed}% ${
      statusSlices.completed + statusSlices.running
    }%, #9a72f8 ${statusSlices.completed + statusSlices.running}% ${
      statusSlices.completed + statusSlices.running + statusSlices.queued
    }%, #f36f98 ${statusSlices.completed + statusSlices.running + statusSlices.queued}% 100%)`
  };
  const recentActivity = useMemo(() => {
    const fromLogs = logs.slice(0, 7).map((event) => ({
      key: event.id,
      title: event.type.replaceAll(".", " "),
      detail: summarizePayload(event.payload),
      time: relativeTime(event.created_at),
      tone: event.type.includes("error") || event.type.includes("failed") ? "danger" : "accent"
    }));
    if (fromLogs.length > 0) return fromLogs;
    return runs.slice(0, 7).map((run) => ({
      key: run.run_id,
      title: `Run ${run.status}`,
      detail: run.message || run.stop_reason || run.run_id,
      time: run.session_id,
      tone: run.status === "failed" || run.status === "blocked" ? "danger" : "accent"
    }));
  }, [logs, runs]);
  const sparkValues = useMemo(() => {
    const values = runs
      .slice(0, 7)
      .reverse()
      .map((run, index) => Math.max(run.context_chars || run.tool_count * 420 || (index + 1) * 360, 160));
    return values.length > 1 ? values : [820, 1980, 1420, 1040, 1660, 1220, 2140, 1360];
  }, [runs]);
  const sparkMax = Math.max(...sparkValues, 1);
  const sparkPoints = sparkValues
    .map((value, index) => {
      const x = (index / Math.max(sparkValues.length - 1, 1)) * 320;
      const y = 92 - (value / sparkMax) * 68;
      return `${x.toFixed(1)},${y.toFixed(1)}`;
    })
    .join(" ");

  async function refresh() {
    const [runList, sessionList, toolList, approvalList, mcpList, skillList] = await Promise.all([
      api.get<Run[]>("/api/runs"),
      api.get<Session[]>("/api/sessions"),
      api.get<Tool[]>("/api/tools"),
      api.get<Approval[]>("/api/approvals?status=pending"),
      api.get<McpServer[]>("/api/mcp/servers"),
      api.get<Skill[]>("/api/skills")
    ]);
    setRuns(runList);
    setSessions(sessionList);
    setTools(toolList);
    setApprovals(approvalList);
    setMcpServers(mcpList);
    setSkills(skillList);
    if (!activeRunId && runList.length > 0) setActiveRunId(runList[0].run_id);
  }

  useEffect(() => {
    refresh().catch(console.error);
    refreshLogs().catch(console.error);
    const timer = window.setInterval(() => refresh().catch(console.error), 3000);
    return () => window.clearInterval(timer);
  }, []);

  useEffect(() => {
    refreshTrace(activeRun?.run_id).catch(console.error);
  }, [activeRun?.run_id]);

  useEffect(() => {
    if (!activeRun?.run_id) return;
    setEvents([]);
    const source = new EventSource(`/api/runs/${activeRun.run_id}/events`);
    source.onmessage = (event) => {
      const parsed = JSON.parse(event.data);
      setEvents((rows) => [...rows.slice(-80), parsed]);
      refresh().catch(console.error);
    };
    [
      "run.started",
      "run.completed",
      "run.blocked",
      "run.failed",
      "approval.requested",
      "tool.executed",
      "tool.started",
      "tool.completed",
      "tool.failed",
      "assistant.token",
      "assistant.tool_call",
      "assistant.usage",
      "assistant.provider_error",
      "context.compile",
      "memory.write",
      "runtime.error",
      "capsule.completed",
      "capsule.failed",
      "task.approved",
      "subagent.queued",
      "subagent.started",
      "subagent.completed",
      "subagent.failed"
    ].forEach((type) => {
      source.addEventListener(type, (event) => {
        const parsed = JSON.parse((event as MessageEvent).data);
        setEvents((rows) => [...rows.slice(-80), parsed]);
        if (type !== "assistant.token") {
          refresh().catch(console.error);
          refreshTrace(activeRun.run_id).catch(console.error);
          refreshLogs().catch(console.error);
        }
      });
    });
    return () => source.close();
  }, [activeRun?.run_id]);

  async function refreshTrace(runId: string | null | undefined = activeRun?.run_id) {
    if (!runId) {
      setRunTrace(null);
      return;
    }
    setRunTrace(await api.get<RunTrace>(`/api/runs/${runId}/trace?limit=500`));
  }

  async function refreshLogs() {
    setLogs(await api.get<AgentLogEvent[]>("/api/logs?limit=80"));
  }

  async function submitRun(event: FormEvent) {
    event.preventDefault();
    if (!message.trim()) return;
    const run = await api.post<Run>("/api/runs", { message });
    setMessage("");
    setActiveRunId(run.run_id);
    await refresh();
    const graph = await api.get<TaskGraph>(`/api/runs/${run.run_id}/task-graph`);
    setTaskGraph(graph);
  }

  async function decide(approval: Approval, approved: boolean) {
    await api.post(`/api/approvals/${approval.approval_id}/decision`, {
      approved,
      arguments: approval.arguments
    });
    await refresh();
  }

  async function searchMemory(event: FormEvent) {
    event.preventDefault();
    if (!memoryQuery.trim()) return;
    const params = new URLSearchParams({ query: memoryQuery, k: "8" });
    const hits = await api.get<MemoryHit[]>(`/api/memory/search?${params.toString()}`);
    setMemoryHits(hits);
  }

  async function verifyMemory() {
    setMemoryVerify(await api.get<MemoryVerifyResult>("/api/memory/verify"));
  }

  async function submitLearning(event: FormEvent) {
    event.preventDefault();
    if (!learningTitle.trim() || !learningContent.trim()) return;
    const result = await api.post<Record<string, unknown>>("/api/memory/learn", {
      title: learningTitle,
      content: learningContent,
      kind: learningKind,
      validation_score: Number(learningValidation),
      repeat_count: Number(learningRepeat),
      explicit_instruction: learningExplicit
    });
    setLearningResult(result);
    await refresh();
  }

  async function packContext(event: FormEvent) {
    event.preventDefault();
    const query = contextQuery.trim() || memoryQuery.trim();
    if (!query) return;
    const params = new URLSearchParams({
      query,
      token_budget: contextBudget.trim() || "6000",
      expand_raw: contextExpandRaw ? "true" : "false",
      include_telemetry: "true"
    });
    if (contextLayers.trim()) params.set("layers", contextLayers);
    const result = await api.get<ContextPackResult>(`/api/context?${params.toString()}`);
    setContextPackResult(result);
  }

  async function cancelActiveRun() {
    if (!activeRun?.run_id) return;
    await api.post(`/api/runs/${activeRun.run_id}/cancel`);
    await refresh();
  }

  async function selectSession(session: Session) {
    setActiveRunId(session.latest_run_id);
    const [graph] = await Promise.all([
      api.get<TaskGraph>(`/api/runs/${session.latest_run_id}/task-graph`),
      refreshTrace(session.latest_run_id)
    ]);
    setTaskGraph(graph);
  }

  async function findConflicts() {
    const query = contextQuery.trim() || memoryQuery.trim();
    if (!query) return;
    const params = new URLSearchParams({ query, k: "8" });
    const result = await api.get<Record<string, unknown>>(`/api/memory/conflicts?${params.toString()}`);
    setConflictResult(result);
  }

  async function summarizeActiveCapsule() {
    if (!activeRun?.run_id) return;
    const result = await api.post<Record<string, unknown>>(`/api/capsules/${activeRun.run_id}/summarize`, { dry_run: true });
    setCapsuleResult(result);
  }

  async function applyActiveCapsule() {
    if (!activeRun?.run_id) return;
    const result = await api.post<Record<string, unknown>>(`/api/capsules/${activeRun.run_id}/apply`, {
      dry_run: false,
      include_policy: false
    });
    setCapsuleResult(result);
    await refresh();
  }

  async function discoverSkills() {
    await api.post("/api/skills/discover");
    await refresh();
  }

  async function refreshTaskGraph() {
    if (!activeRun?.run_id) return;
    const graph = await api.get<TaskGraph>(`/api/runs/${activeRun.run_id}/task-graph`);
    setTaskGraph(graph);
  }

  async function submitSubagent(event: FormEvent) {
    event.preventDefault();
    if (!activeRun?.run_id || !subagentGoal.trim()) return;
    await api.post("/api/subagents", {
      run_id: activeRun.run_id,
      profile: subagentProfile,
      goal: subagentGoal
    });
    setSubagentGoal("");
    await refreshTaskGraph();
  }

  async function submitMcp(event: FormEvent) {
    event.preventDefault();
    if (!mcpId.trim()) return;
    await api.post("/api/mcp/servers", {
      id: mcpId.trim(),
      name: mcpId.trim(),
      transport: mcpTransport,
      command: mcpTransport === "stdio" ? mcpCommand.trim() || null : null,
      url: mcpTransport === "stdio" ? null : mcpCommand.trim() || null,
      args: [],
      tools: [],
      risk_policy: "approval_by_default"
    });
    setMcpId("");
    setMcpCommand("");
    await refresh();
  }

  async function controlMcp(server: McpServer, action: "connect" | "disconnect" | "restart") {
    await api.post(`/api/mcp/servers/${server.id}/${action}`);
    await refresh();
  }

  async function syncMcp(server: McpServer) {
    await api.post(`/api/mcp/servers/${server.id}/sync`);
    await refresh();
  }

  async function testMcp(server: McpServer) {
    await api.post(`/api/mcp/servers/${server.id}/test`);
    await refresh();
  }

  async function healthMcp(server: McpServer) {
    await api.get(`/api/mcp/servers/${server.id}/health`);
    await refresh();
  }

  async function deleteMcp(server: McpServer) {
    await api.delete(`/api/mcp/servers/${server.id}`);
    await refresh();
  }

  async function invokeMcp(event: FormEvent) {
    event.preventDefault();
    if (!mcpToolSelection) return;
    const [serverId, toolName] = mcpToolSelection.split("::");
    try {
      const parsed = JSON.parse(mcpArguments || "{}");
      const result = await api.post<Record<string, unknown>>(
        `/api/mcp/servers/${serverId}/tools/${encodeURIComponent(toolName)}/invoke`,
        { arguments: parsed }
      );
      setMcpInvokeResult(result);
    } catch (error) {
      setMcpInvokeResult({ success: false, error: error instanceof Error ? error.message : String(error) });
    }
    await refresh();
  }

  async function toggleSkill(skill: Skill) {
    await api.post(`/api/skills/${skill.id}/${skill.enabled ? "disable" : "enable"}`);
    await refresh();
  }

  return (
    <div className="shell">
      <aside className="sidebar">
        <div className="brand">
          <div className="brand-mark">
            <Feather size={27} />
          </div>
          <div>
            <strong>Kestrel</strong>
            <span>Nested MV2 runtime</span>
          </div>
        </div>
        <nav aria-label="Primary">
          <a className="active" href="#overview"><Home size={18} /> Overview</a>
          <a href="#chat"><TerminalSquare size={18} /> Agent Console</a>
          <a href="#sessions"><ServerCog size={18} /> Sessions</a>
          <a href="#subagents"><Bot size={18} /> Agents</a>
          <a href="#approvals"><ShieldCheck size={18} /> Approvals</a>
          <a href="#context"><FileText size={18} /> Context</a>
          <a href="#memory"><Database size={18} /> Knowledge</a>
          <a href="#mcp"><PlugZap size={18} /> Integrations</a>
          <a href="#observability"><LineChart size={18} /> Analytics</a>
          <a href="#tools"><Settings size={18} /> Tools</a>
        </nav>
        <div className="sidebar-footer">
          <a href="https://github.com" aria-label="GitHub"><GitBranch size={18} /></a>
          <a href="#observability" aria-label="Logs"><ScrollText size={18} /></a>
          <a href="#overview" aria-label="Collapse sidebar">&lt;</a>
        </div>
      </aside>

      <main className="workspace">
        <header className="topbar">
          <form
            className="global-search"
            onSubmit={async (event) => {
              event.preventDefault();
              await searchMemory(event);
              document.getElementById("memory")?.scrollIntoView({ behavior: "smooth" });
            }}
          >
            <Search size={17} />
            <input
              value={memoryQuery}
              onChange={(event) => setMemoryQuery(event.target.value)}
              placeholder="Search agents, memories, tools..."
            />
            <kbd>Ctrl K</kbd>
          </form>
          <div className="topbar-actions">
            <button type="button" className="icon-button" aria-label="Notifications">
              <Bell size={18} />
              {approvals.length > 0 && <span className="notification-dot" />}
            </button>
            <a className="icon-button" href="#context" aria-label="Context"><BookOpen size={18} /></a>
            <div className="profile-chip">
              <span>KD</span>
              <div>
                <strong>Kestrel Dev</strong>
                <small>{systemHealthy ? "Healthy" : "Needs review"}</small>
              </div>
            </div>
          </div>
        </header>

        <section id="overview" className="dashboard">
          <div className="hero-copy">
            <h1>Good morning, Kestrel.</h1>
            <p>Open-source agents. Built for builders.</p>
          </div>

          <div className="metric-grid">
            <article className="metric-card">
              <Bot size={27} />
              <span>Agents</span>
              <strong>{asCount(activeAgents)}</strong>
              <small>{taskGraph?.subagents.length ?? 0} delegated</small>
            </article>
            <article className="metric-card">
              <GitBranch size={27} />
              <span>Workflows</span>
              <strong>{asCount(sessions.length + mcpServers.length)}</strong>
              <small>{connectedServers} connected</small>
            </article>
            <article className="metric-card">
              <ClipboardList size={27} />
              <span>Tasks</span>
              <strong>{asCount(runs.length + approvals.length)}</strong>
              <small>{queuedTasks} queued</small>
            </article>
            <article className="metric-card">
              <Activity size={27} />
              <span>System</span>
              <strong>{systemHealthy ? "Healthy" : "Review"}</strong>
              <small>{failedRuns} failed runs</small>
            </article>
          </div>

          <div className="dashboard-grid">
            <section className="panel workflow-panel">
              <div className="panel-head">
                <div>
                  <h2>Workflow canvas</h2>
                  <p>Orchestrate agents and automate local runtime processes.</p>
                </div>
                <div className="canvas-controls">
                  <button type="button" aria-label="Zoom out">-</button>
                  <span>100%</span>
                  <button type="button" aria-label="Zoom in">+</button>
                  <button type="button" aria-label="Fit canvas"><Square size={14} /></button>
                </div>
              </div>
              <div className="workflow-canvas">
                <div className="canvas-toolbar" aria-hidden="true">
                  <TerminalSquare size={17} />
                  <Network size={17} />
                  <Layers size={17} />
                  <FileText size={17} />
                  <Sparkles size={17} />
                </div>
                <svg className="workflow-lines" viewBox="0 0 720 240" role="presentation">
                  <path d="M145 118 C205 118 205 118 268 118" />
                  <path d="M412 118 C462 118 450 50 510 50" />
                  <path d="M412 118 C466 118 466 118 514 118" />
                  <path d="M412 118 C462 118 450 186 510 186" />
                </svg>
                <div className="flow-node node-input">
                  <Network size={18} />
                  <div>
                    <strong>Webhook</strong>
                    <span>Trigger</span>
                  </div>
                  <i />
                </div>
                <div className="flow-node node-agent">
                  <Bot size={18} />
                  <div>
                    <strong>Enrich Lead</strong>
                    <span>Agent</span>
                  </div>
                  <i />
                </div>
                <div className="flow-node node-score">
                  <LineChart size={18} />
                  <div>
                    <strong>Score Lead</strong>
                    <span>Agent</span>
                  </div>
                  <i />
                </div>
                <div className="flow-node node-memory">
                  <Database size={18} />
                  <div>
                    <strong>Write Memory</strong>
                    <span>Validated</span>
                  </div>
                  <i />
                </div>
                <div className="flow-node node-team">
                  <Send size={18} />
                  <div>
                    <strong>Notify Team</strong>
                    <span>Action</span>
                  </div>
                  <i />
                </div>
              </div>
            </section>

            <aside className="panel activity-panel">
              <div className="panel-head">
                <h2>Recent activity</h2>
              </div>
              <div className="activity-list">
                {recentActivity.length === 0 && <p className="muted">No recent runtime activity.</p>}
                {recentActivity.map((item, index) => (
                  <div className="activity-row" key={item.key}>
                    <span className={`activity-icon ${item.tone}`}>
                      {index % 3 === 0 ? <Check size={15} /> : index % 3 === 1 ? <UserRound size={15} /> : <PlugZap size={15} />}
                    </span>
                    <div>
                      <strong>{item.title}</strong>
                      <small>{item.detail}</small>
                    </div>
                    <time>{item.time}</time>
                  </div>
                ))}
              </div>
            </aside>

            <section className="panel chart-panel">
              <div className="panel-head">
                <h2>Runs over time</h2>
                <span>7 runs</span>
              </div>
              <svg className="sparkline" viewBox="0 0 330 112" role="img" aria-label="Run trend">
                <polyline points={sparkPoints} />
                {sparkValues.map((value, index) => {
                  const x = (index / Math.max(sparkValues.length - 1, 1)) * 320;
                  const y = 92 - (value / sparkMax) * 68;
                  return <circle key={`${value}-${index}`} cx={x} cy={y} r="3.5" />;
                })}
              </svg>
              <div className="chart-axis">
                <span>oldest</span>
                <span>latest</span>
              </div>
            </section>

            <section className="panel status-panel">
              <div className="panel-head">
                <h2>Tasks by status</h2>
              </div>
              <div className="status-content">
                <div className="donut" style={statusDonutStyle} />
                <div className="legend">
                  <span><i className="completed" /> Completed <b>{statusSlices.completed}%</b></span>
                  <span><i className="running" /> In progress <b>{statusSlices.running}%</b></span>
                  <span><i className="queued" /> Queued <b>{statusSlices.queued}%</b></span>
                  <span><i className="failed" /> Failed <b>{statusSlices.failed}%</b></span>
                </div>
              </div>
            </section>
          </div>
        </section>

        <section id="chat" className="band command-center">
          <div className="section-head">
            <div>
              <div className="section-title"><TerminalSquare size={18} /> Agent Console</div>
              <p>Run the conversational CLI-backed agent and inspect streamed execution state.</p>
            </div>
            <button type="button" onClick={() => refresh()}><RotateCw size={15} /> Refresh</button>
          </div>
          <div className="command-grid">
            <div className="run-list">
              <div className="section-title"><Activity size={18} /> Runs</div>
              {runs.length === 0 && <p className="muted">No runs yet.</p>}
              {runs.map((run) => (
                <button
                  type="button"
                  key={run.run_id}
                  className={run.run_id === activeRun?.run_id ? "run selected" : "run"}
                  onClick={() => setActiveRunId(run.run_id)}
                >
                  <span>{run.message || run.run_id}</span>
                  <small>{run.status}</small>
                </button>
              ))}
            </div>

            <div className="conversation">
              <div className="run-header">
                <div>
                  <div className="status-line">
                    <h2>{activeRun?.status ?? "Ready"}</h2>
                    {activeRun && <span className={`status-pill ${activeRun.status}`}>{activeRun.stop_reason || activeRun.status}</span>}
                  </div>
                  <p>{activeRun?.session_id ?? "Start a background run"}</p>
                </div>
                <div className="run-header-actions">
                  {activeRun?.status === "running" && <Square className="pulse" size={20} />}
                  {activeRun && activeRun.status === "running" && (
                    <button type="button" className="danger" onClick={cancelActiveRun}>
                      <Square size={14} /> Cancel
                    </button>
                  )}
                </div>
              </div>

              <div className="transcript">
                {activeRun ? (
                  <>
                    <div className="bubble user">{activeRun.message}</div>
                    <div className="bubble agent">{activeRun.assistant_message || streamedAssistant || activeRun.stop_reason || "Working..."}</div>
                    {(activeRun.error || activeRun.status === "failed" || activeRun.status === "blocked") && (
                      <div className="run-alert">
                        <strong>{activeRun.status}</strong>
                        <span>{activeRun.error || activeRun.stop_reason || "Run needs attention."}</span>
                      </div>
                    )}
                  </>
                ) : (
                  <div className="empty">No run selected.</div>
                )}
              </div>

              <form className="composer" onSubmit={submitRun}>
                <input value={message} onChange={(event) => setMessage(event.target.value)} placeholder="Ask the agent to do real work..." />
                <button type="submit"><Send size={15} /> Run</button>
              </form>
            </div>

            <div className="timeline">
              <div className="section-title"><Activity size={18} /> Run Replay</div>
              {activeRun && (
                <div className="trace-summary">
                  <span>{activeRun.run_id}</span>
                  <span>{activeRun.tool_count} tools</span>
                  <span>{activeRun.context_chars} chars</span>
                </div>
              )}
              {events.map((event) => (
                <div className="event" key={event.id}>
                  <span>{event.type}</span>
                  <code>{JSON.stringify(event.payload).slice(0, 220)}</code>
                </div>
              ))}
            </div>
          </div>
        </section>

        <section id="sessions" className="band">
          <div className="section-head">
            <div className="section-title"><ServerCog size={18} /> Sessions</div>
            <button type="button" onClick={() => refresh()}><RotateCw size={15} /> Refresh</button>
          </div>
          <div className="session-grid">
            {sessions.map((session) => (
              <button
                type="button"
                className={session.latest_run_id === activeRun?.run_id ? "session selected" : "session"}
                key={session.session_id}
                onClick={() => selectSession(session)}
              >
                <strong>{session.session_id}</strong>
                <span>{session.run_count} runs / {session.latest_status}</span>
                <small>{session.latest_message}</small>
                <code>{JSON.stringify(session.status_counts)}</code>
              </button>
            ))}
          </div>
        </section>

        <section id="subagents" className="band two-col">
          <form onSubmit={submitSubagent} className="memory-search">
            <div className="section-title"><Bot size={18} /> Subagents</div>
            <select value={subagentProfile} onChange={(event) => setSubagentProfile(event.target.value)}>
              <option value="worker">Worker</option>
              <option value="planner">Planner</option>
              <option value="reviewer">Reviewer</option>
            </select>
            <textarea value={subagentGoal} onChange={(event) => setSubagentGoal(event.target.value)} placeholder="Bounded subagent goal for the active run" />
            <div className="actions">
              <button type="submit" disabled={!activeRun}>Queue Subagent</button>
              <button type="button" onClick={refreshTaskGraph} disabled={!activeRun}>Refresh Graph</button>
            </div>
          </form>
          <div>
            <div className="section-title"><Activity size={18} /> Task Graph</div>
            {taskGraph?.tasks.map((task) => (
              <div className="row" key={task.task_id}>
                <strong>{task.title}</strong>
                <span>{task.profile} / {task.status} / {task.approved ? "approved" : "needs review"}</span>
                <p>{task.goal}</p>
              </div>
            ))}
            {taskGraph?.subagents.map((subagent) => (
              <div className="row" key={subagent.subagent_id}>
                <strong>{subagent.profile}</strong>
                <span>{subagent.status}</span>
                <p>{subagent.result || subagent.error || subagent.goal}</p>
              </div>
            ))}
          </div>
        </section>

        <section id="approvals" className="band two-col">
          <div>
            <div className="section-title"><ShieldCheck size={18} /> Pending Approvals</div>
            {approvals.length === 0 && <p className="muted">No blocked actions.</p>}
            {approvals.map((approval) => (
              <div className="approval" key={approval.approval_id}>
                <div>
                  <strong>{approval.tool_name}</strong>
                  <span>{approval.risk}</span>
                  <code>{JSON.stringify(approval.arguments)}</code>
                </div>
                <div className="actions">
                  <button type="button" onClick={() => decide(approval, true)}><Check size={16} /> Approve</button>
                  <button type="button" className="danger" onClick={() => decide(approval, false)}><X size={16} /> Deny</button>
                </div>
              </div>
            ))}
          </div>
          <div id="tools">
            <div className="section-title"><Wrench size={18} /> Tool Inventory</div>
            <div className="tool-grid">
              {tools.map((tool) => (
                <div className="tool" key={tool.name}>
                  <strong>{tool.name}</strong>
                  <span>{tool.source} / {tool.risk}</span>
                  <p>{tool.description}</p>
                </div>
              ))}
            </div>
          </div>
        </section>

        <section id="context" className="band two-col">
          <form onSubmit={packContext} className="memory-search">
            <div className="section-title"><FileText size={18} /> Compiled Context Viewer</div>
            <input value={contextQuery} onChange={(event) => setContextQuery(event.target.value)} placeholder="Objective or claim..." />
            <div className="context-controls">
              <input value={contextLayers} onChange={(event) => setContextLayers(event.target.value)} placeholder="layers CSV" />
              <input value={contextBudget} onChange={(event) => setContextBudget(event.target.value)} inputMode="numeric" />
              <label className="check-row">
                <input type="checkbox" checked={contextExpandRaw} onChange={(event) => setContextExpandRaw(event.target.checked)} />
                <span>Expand raw</span>
              </label>
            </div>
            <div className="actions">
              <button type="submit">Pack Context</button>
              <button type="button" onClick={findConflicts}>Find Conflicts</button>
              <button type="button" onClick={summarizeActiveCapsule} disabled={!activeRun}>Capsule Summary</button>
              <button type="button" onClick={applyActiveCapsule} disabled={!activeRun}>Request Apply</button>
            </div>
          </form>
          <div className="context-metadata">
            <div className="section-title"><Activity size={18} /> Context Trace</div>
            {contextPackResult ? (
              <>
                <div className="trace-summary">
                  <span>{contextPackResult.token_estimate ?? 0} tokens</span>
                  <span>{contextPackResult.selected_item_count ?? 0} items</span>
                  <span>{contextPackResult.selected_layers?.join(", ") || "no layers"}</span>
                </div>
                {contextPackResult.conflict_warnings?.length ? (
                  <div className="run-alert">
                    <strong>conflicts</strong>
                    <span>{contextPackResult.conflict_warnings.join(" | ")}</span>
                  </div>
                ) : (
                  <p className="muted">No conflict warnings in the compiled context.</p>
                )}
                <code>{JSON.stringify(contextPackResult.telemetry ?? {}, null, 2)}</code>
              </>
            ) : (
              <p className="muted">No compiled context loaded.</p>
            )}
          </div>
          {contextPackResult?.packed_prompt && (
            <pre className="context-viewer">{contextPackResult.packed_prompt}</pre>
          )}
        </section>

        <section id="memory" className="band two-col">
          <form onSubmit={searchMemory} className="memory-search">
            <div className="section-title"><Search size={18} /> Memory Browser</div>
            <input value={memoryQuery} onChange={(event) => setMemoryQuery(event.target.value)} placeholder="Search nested memory..." />
            <div className="actions">
              <button type="submit">Search</button>
              <button type="button" onClick={verifyMemory}>Verify Memory</button>
            </div>
            {memoryVerify && (
              <div className="verify-grid">
                {Object.entries(memoryVerify).map(([layer, ok]) => (
                  <span className={ok ? "verify ok" : "verify failed"} key={layer}>{layer}: {ok ? "ok" : "failed"}</span>
                ))}
              </div>
            )}
          </form>
          <div className="learning-panel">
            <form onSubmit={submitLearning} className="memory-search">
              <div className="section-title"><Brain size={18} /> Learning Signal</div>
              <input value={learningTitle} onChange={(event) => setLearningTitle(event.target.value)} placeholder="Title" />
              <textarea value={learningContent} onChange={(event) => setLearningContent(event.target.value)} placeholder="Validated memory content" />
              <div className="learning-controls">
                <select value={learningKind} onChange={(event) => setLearningKind(event.target.value)}>
                  <option value="observation">Observation</option>
                  <option value="fact">Fact</option>
                  <option value="event">Event</option>
                  <option value="failure">Failure</option>
                  <option value="procedure">Procedure</option>
                  <option value="policy">Policy</option>
                </select>
                <input value={learningValidation} onChange={(event) => setLearningValidation(event.target.value)} inputMode="decimal" />
                <input value={learningRepeat} onChange={(event) => setLearningRepeat(event.target.value)} inputMode="numeric" />
              </div>
              <label className="check-row">
                <input type="checkbox" checked={learningExplicit} onChange={(event) => setLearningExplicit(event.target.checked)} />
                <span>Explicit instruction</span>
              </label>
              <button type="submit">Learn</button>
            </form>
            {learningResult && <code>{JSON.stringify(learningResult).slice(0, 420)}</code>}
          </div>
          <div className="learning-panel">
            <div className="section-title"><ShieldCheck size={18} /> Conflict Warnings</div>
            {conflictResult ? <code>{JSON.stringify(conflictResult).slice(0, 720)}</code> : <p className="muted">No conflict query run.</p>}
            <div className="section-title"><Activity size={18} /> Consolidation Decisions</div>
            {capsuleResult ? <code>{JSON.stringify(capsuleResult).slice(0, 720)}</code> : <p className="muted">No capsule summary loaded.</p>}
          </div>
          <div className="hits wide">
            {memoryHits.map((hit, index) => (
              <div className="hit" key={`${hit.title}-${index}`}>
                <strong>{hit.title}</strong>
                <span>{hit.layer} / {hit.kind} / {hit.score.toFixed(2)}</span>
                <p>{hit.snippet}</p>
              </div>
            ))}
          </div>
        </section>

        <section id="observability" className="band two-col">
          <div className="observability-panel">
            <div className="section-head">
              <div className="section-title"><ScrollText size={18} /> Run Trace</div>
              <button type="button" onClick={() => refreshTrace()} disabled={!activeRun}><RotateCw size={15} /> Refresh</button>
            </div>
            {runTrace ? (
              <>
                <div className="trace-summary">
                  <span>{runTrace.summary.event_count} events</span>
                  {Object.entries(runTrace.summary.trace_counts).map(([name, count]) => (
                    <span key={name}>{name}: {count}</span>
                  ))}
                </div>
                <div className="trace-buckets">
                  {Object.entries(runTrace.traces).map(([name, traceEvents]) => (
                    <div className="trace-bucket" key={name}>
                      <strong>{name}</strong>
                      {traceEvents.slice(-5).map((event) => (
                        <code key={event.id}>{event.type}: {JSON.stringify(event.payload).slice(0, 260)}</code>
                      ))}
                    </div>
                  ))}
                </div>
              </>
            ) : (
              <p className="muted">No run trace selected.</p>
            )}
          </div>
          <div className="observability-panel">
            <div className="section-head">
              <div className="section-title"><Activity size={18} /> JSONL Logs</div>
              <button type="button" onClick={refreshLogs}><RotateCw size={15} /> Refresh</button>
            </div>
            <div className="log-viewer">
              {logs.map((event) => (
                <div className="log-row" key={event.id}>
                  <span>{event.type}</span>
                  <small>{event.created_at}</small>
                  <code>{JSON.stringify(event.payload).slice(0, 300)}</code>
                </div>
              ))}
            </div>
          </div>
        </section>

        <section id="mcp" className="band two-col">
          <div>
            <div className="section-title"><PlugZap size={18} /> MCP Servers</div>
            <form onSubmit={submitMcp} className="memory-search">
              <input value={mcpId} onChange={(event) => setMcpId(event.target.value)} placeholder="Server id" />
              <select value={mcpTransport} onChange={(event) => setMcpTransport(event.target.value)}>
                <option value="stdio">stdio</option>
                <option value="streamable_http">streamable http</option>
                <option value="sse">sse</option>
              </select>
              <input value={mcpCommand} onChange={(event) => setMcpCommand(event.target.value)} placeholder="Command or URL" />
              <button type="submit">Add Server</button>
            </form>
            {mcpServers.map((server) => (
              <div className="row" key={server.id}>
                <strong>{server.name}</strong>
                <span>{server.transport} / {server.status} / {server.session_state ?? "disconnected"} / {server.tool_count ?? server.tools.length} tools</span>
                <div className="mcp-health">
                  <span>{server.risk_policy ?? "approval_by_default"}</span>
                  <span>{server.failure_count ?? 0} failures</span>
                  <span>{server.last_latency_ms ?? 0} ms</span>
                  {server.last_seen_at && <span>seen {new Date(server.last_seen_at).toLocaleTimeString()}</span>}
                </div>
                {server.error && <p>{server.error}</p>}
                <div className="actions">
                  <button type="button" onClick={() => controlMcp(server, "connect")}>Connect</button>
                  <button type="button" onClick={() => healthMcp(server)}>Health</button>
                  <button type="button" onClick={() => controlMcp(server, "restart")}>Restart</button>
                  <button type="button" onClick={() => controlMcp(server, "disconnect")}>Disconnect</button>
                  <button type="button" onClick={() => testMcp(server)}>Test</button>
                  <button type="button" onClick={() => syncMcp(server)}>Sync</button>
                  <button type="button" className="danger" onClick={() => deleteMcp(server)}>Delete</button>
                </div>
                {server.tools.length > 0 && (
                  <div className="tool-list">
                    {server.tools.map((tool) => (
                      <button
                        type="button"
                        key={tool.name}
                        onClick={() => {
                          setMcpToolSelection(`${server.id}::${tool.remote_name ?? tool.name}`);
                          setMcpArguments(JSON.stringify(tool.parameters ?? {}, null, 2));
                        }}
                      >
                        {tool.remote_name ?? tool.name} / {tool.risk}
                      </button>
                    ))}
                  </div>
                )}
              </div>
            ))}
            <form onSubmit={invokeMcp} className="mcp-invoke">
              <div className="section-title"><Wrench size={18} /> Manual MCP Invoke</div>
              <select value={mcpToolSelection} onChange={(event) => setMcpToolSelection(event.target.value)}>
                <option value="">Select tool</option>
                {mcpToolOptions.map(({ server, tool, value }) => (
                  <option key={value} value={value}>
                    {server.id} / {tool.remote_name ?? tool.name}
                  </option>
                ))}
              </select>
              <textarea value={mcpArguments} onChange={(event) => setMcpArguments(event.target.value)} />
              <button type="submit" disabled={!mcpToolSelection}>Invoke Tool</button>
              {mcpInvokeResult && <code>{JSON.stringify(mcpInvokeResult).slice(0, 680)}</code>}
            </form>
          </div>
          <div>
            <div className="section-title"><Sparkles size={18} /> Skills</div>
            <button type="button" onClick={discoverSkills}>Discover Skills</button>
            {skills.map((skill) => (
              <div className="row" key={skill.id}>
                <strong>{skill.name}</strong>
                <span>{skill.enabled ? "enabled" : "disabled"}</span>
                <p>{skill.description}</p>
                <button type="button" onClick={() => toggleSkill(skill)}>{skill.enabled ? "Disable" : "Enable"}</button>
              </div>
            ))}
          </div>
        </section>
      </main>
    </div>
  );
}
