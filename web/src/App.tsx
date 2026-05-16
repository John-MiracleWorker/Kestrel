import {
  Activity,
  Bell,
  Bot,
  Brain,
  Check,
  ClipboardCheck,
  Database,
  FileText,
  GitBranch,
  Home,
  Layers,
  LineChart,
  PlugZap,
  RefreshCw,
  Route,
  Search,
  Send,
  ServerCog,
  Settings,
  ShieldCheck,
  Sparkles,
  Square,
  TerminalSquare,
  TestTube2,
  Wrench,
  X
} from "lucide-react";
import { FormEvent, useEffect, useMemo, useState } from "react";
import { deleteJson, getJson, postJson, putJson, queryString } from "./api";
import { EmptyState, Field, InlineMeta, JsonBlock, Panel, StatusBadge } from "./components";
import type {
  AgentLogEvent,
  ApiResult,
  Approval,
  Channel,
  ContextPackResult,
  McpServer,
  MemoryHit,
  MemoryLayerStatus,
  Plugin,
  Run,
  RunTrace,
  RuntimeConfig,
  Session,
  Skill,
  TaskGraph,
  TaskNode,
  Tool,
  TraceEvent
} from "./types";

const providerOptions = ["mock", "openai", "openai-compatible", "openrouter", "ollama", "anthropic", "gemini", "codex-cli"];
const autonomyOptions = [
  { value: "background", label: "Background run" },
  { value: "manual", label: "Manual review" },
  { value: "autonomous", label: "Autonomous scheduler" }
];

export function App() {
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [runtime, setRuntime] = useState<Record<string, unknown> | null>(null);
  const [runs, setRuns] = useState<Run[]>([]);
  const [sessions, setSessions] = useState<Session[]>([]);
  const [tools, setTools] = useState<Tool[]>([]);
  const [approvals, setApprovals] = useState<Approval[]>([]);
  const [allApprovals, setAllApprovals] = useState<Approval[]>([]);
  const [mcpServers, setMcpServers] = useState<McpServer[]>([]);
  const [skills, setSkills] = useState<Skill[]>([]);
  const [plugins, setPlugins] = useState<Plugin[]>([]);
  const [channels, setChannels] = useState<Channel[]>([]);
  const [memoryLayers, setMemoryLayers] = useState<MemoryLayerStatus[]>([]);
  const [lessons, setLessons] = useState<Array<Record<string, unknown>>>([]);
  const [failures, setFailures] = useState<Array<Record<string, unknown>>>([]);
  const [logs, setLogs] = useState<AgentLogEvent[]>([]);
  const [events, setEvents] = useState<TraceEvent[]>([]);
  const [runTrace, setRunTrace] = useState<RunTrace | null>(null);
  const [taskGraph, setTaskGraph] = useState<TaskGraph | null>(null);
  const [activeRunId, setActiveRunId] = useState<string | null>(null);

  const [message, setMessage] = useState("");
  const [sessionId, setSessionId] = useState("");
  const [workspace, setWorkspace] = useState("");
  const [provider, setProvider] = useState("mock");
  const [model, setModel] = useState("mock");
  const [autonomyMode, setAutonomyMode] = useState("background");

  const [subagentProfile, setSubagentProfile] = useState("worker");
  const [subagentGoal, setSubagentGoal] = useState("");
  const [schedulerTasks, setSchedulerTasks] = useState("3");
  const [schedulerCycles, setSchedulerCycles] = useState("5");
  const [schedulerResult, setSchedulerResult] = useState<Record<string, unknown> | null>(null);

  const [memoryQuery, setMemoryQuery] = useState("");
  const [memoryHits, setMemoryHits] = useState<MemoryHit[]>([]);
  const [memoryInspect, setMemoryInspect] = useState<Record<string, unknown> | null>(null);
  const [contextQuery, setContextQuery] = useState("");
  const [contextLayers, setContextLayers] = useState("policy,procedural,semantic,episodic,working");
  const [contextBudget, setContextBudget] = useState("6000");
  const [contextExpandRaw, setContextExpandRaw] = useState(false);
  const [contextResult, setContextResult] = useState<ContextPackResult | null>(null);
  const [learningTitle, setLearningTitle] = useState("");
  const [learningContent, setLearningContent] = useState("");
  const [learningKind, setLearningKind] = useState("observation");
  const [learningValidation, setLearningValidation] = useState("0.78");
  const [learningRepeat, setLearningRepeat] = useState("1");
  const [learningExplicit, setLearningExplicit] = useState(false);
  const [learningResult, setLearningResult] = useState<Record<string, unknown> | null>(null);
  const [capsuleResult, setCapsuleResult] = useState<Record<string, unknown> | null>(null);
  const [conflictResult, setConflictResult] = useState<Record<string, unknown> | null>(null);

  const [toolName, setToolName] = useState("");
  const [toolArgs, setToolArgs] = useState("{}");
  const [toolResult, setToolResult] = useState<Record<string, unknown> | null>(null);

  const [mcpId, setMcpId] = useState("");
  const [mcpName, setMcpName] = useState("");
  const [mcpTransport, setMcpTransport] = useState("stdio");
  const [mcpEndpoint, setMcpEndpoint] = useState("");
  const [mcpArgs, setMcpArgs] = useState("[]");
  const [mcpEnv, setMcpEnv] = useState("{}");
  const [mcpSecretEnv, setMcpSecretEnv] = useState("{}");
  const [mcpRiskPolicy, setMcpRiskPolicy] = useState("approval_by_default");
  const [mcpEnabled, setMcpEnabled] = useState(true);
  const [mcpToolSelection, setMcpToolSelection] = useState("");
  const [mcpToolArgs, setMcpToolArgs] = useState("{}");
  const [mcpResult, setMcpResult] = useState<Record<string, unknown> | null>(null);

  const [skillTask, setSkillTask] = useState("");
  const [skillSelection, setSkillSelection] = useState("");
  const [skillManifest, setSkillManifest] = useState('{\n  "id": "local-skill",\n  "name": "Local Skill",\n  "description": "Describe what this skill does.",\n  "risk": "medium"\n}');
  const [skillInstructions, setSkillInstructions] = useState("");
  const [skillResult, setSkillResult] = useState<Record<string, unknown> | null>(null);
  const [pluginSource, setPluginSource] = useState("");
  const [pluginRef, setPluginRef] = useState("");
  const [pluginEnable, setPluginEnable] = useState(false);
  const [pluginResult, setPluginResult] = useState<Record<string, unknown> | null>(null);

  const [channelId, setChannelId] = useState("webhook");
  const [channelProvider, setChannelProvider] = useState("webhook");
  const [channelTokenEnv, setChannelTokenEnv] = useState("");
  const [channelWebhookEnv, setChannelWebhookEnv] = useState("NEST_AGENT_CHANNEL_WEBHOOK_URL");
  const [channelEnabled, setChannelEnabled] = useState(true);
  const [channelSendEnabled, setChannelSendEnabled] = useState(false);
  const [channelAutoReply, setChannelAutoReply] = useState(false);
  const [channelSettings, setChannelSettings] = useState("{}");
  const [channelPayload, setChannelPayload] = useState('{\n  "conversation_id": "local-thread",\n  "text": "hello from the UI"\n}');
  const [channelResult, setChannelResult] = useState<Record<string, unknown> | null>(null);

  const [diagnosisText, setDiagnosisText] = useState("");
  const [diagnosisResult, setDiagnosisResult] = useState<Record<string, unknown> | null>(null);

  const activeRun = useMemo(() => runs.find((run) => run.run_id === activeRunId) ?? runs[0] ?? null, [runs, activeRunId]);
  const streamedAssistant = useMemo(
    () =>
      events
        .filter((event) => event.type === "assistant.token")
        .map((event) => String(event.payload.content ?? ""))
        .join(""),
    [events]
  );
  const proofOfWork = useMemo(() => extractProofOfWork(runTrace), [runTrace]);
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

  useEffect(() => {
    refreshAll().catch(reportError);
    const timer = window.setInterval(() => refreshSummary().catch(reportError), 3500);
    return () => window.clearInterval(timer);
  }, []);

  useEffect(() => {
    if (!activeRun?.run_id) return;
    setEvents([]);
    refreshRunDetails(activeRun.run_id).catch(reportError);
    const source = new EventSource(`/api/runs/${activeRun.run_id}/events`);
    const appendEvent = (event: MessageEvent) => {
      const parsed = JSON.parse(event.data) as TraceEvent;
      setEvents((rows) => [...rows.slice(-120), parsed]);
      if (parsed.type !== "assistant.token") {
        refreshSummary().catch(reportError);
        refreshRunDetails(activeRun.run_id).catch(reportError);
      }
    };
    source.onmessage = appendEvent;
    [
      "run.started",
      "run.completed",
      "run.blocked",
      "run.failed",
      "run.cancelled",
      "approval.requested",
      "approval.approved",
      "approval.denied",
      "tool.started",
      "tool.completed",
      "tool.failed",
      "assistant.token",
      "assistant.tool_call",
      "context.compile",
      "memory.write",
      "diagnosis.classified",
      "scheduler.step",
      "scheduler.run",
      "task.approved",
      "subagent.started",
      "subagent.completed",
      "subagent.failed"
    ].forEach((type) => source.addEventListener(type, appendEvent));
    return () => source.close();
  }, [activeRun?.run_id]);

  async function refreshSummary() {
    const [runList, sessionList, toolList, pendingApprovalList, approvalList, mcpList, skillList, pluginList, channelList, layerList] =
      await Promise.all([
        getJson<Run[]>("/api/runs"),
        getJson<Session[]>("/api/sessions"),
        getJson<Tool[]>("/api/tools"),
        getJson<Approval[]>("/api/approvals?status=pending"),
        getJson<Approval[]>("/api/approvals"),
        getJson<McpServer[]>("/api/mcp/servers"),
        getJson<Skill[]>("/api/skills"),
        getJson<Plugin[]>("/api/plugins"),
        getJson<Channel[]>("/api/channels"),
        getJson<MemoryLayerStatus[]>("/api/memory/layers")
      ]);
    setRuns(runList);
    setSessions(sessionList);
    setTools(toolList);
    setApprovals(pendingApprovalList);
    setAllApprovals(approvalList);
    setMcpServers(mcpList);
    setSkills(skillList);
    setPlugins(pluginList);
    setChannels(channelList);
    setMemoryLayers(layerList);
    if (!activeRunId && runList.length > 0) setActiveRunId(runList[0].run_id);
  }

  async function refreshAll() {
    await refreshSummary();
    const [runtimeConfig, logList, lessonList, failureList] = await Promise.all([
      getJson<RuntimeConfig>("/api/runtime/config"),
      getJson<AgentLogEvent[]>("/api/logs?limit=120"),
      getJson<{ items: Array<Record<string, unknown>> }>("/api/cognition/lessons?k=20"),
      getJson<{ items: Array<Record<string, unknown>> }>("/api/cognition/failures?k=20")
    ]);
    setRuntime(runtimeConfig);
    setProvider(String(runtimeConfig.provider?.name ?? "mock"));
    setModel(String(runtimeConfig.provider?.model ?? "mock"));
    setLogs(logList);
    setLessons(lessonList.items);
    setFailures(failureList.items);
  }

  async function refreshRunDetails(runId: string) {
    const [graph, trace] = await Promise.all([
      getJson<TaskGraph>(`/api/runs/${runId}/task-graph`),
      getJson<RunTrace>(`/api/runs/${runId}/trace?limit=700`)
    ]);
    setTaskGraph(graph);
    setRunTrace(trace);
  }

  function reportError(value: unknown) {
    setError(value instanceof Error ? value.message : String(value));
  }

  async function guarded(action: () => Promise<void>, success?: string) {
    setError(null);
    try {
      await action();
      if (success) setNotice(success);
    } catch (value) {
      reportError(value);
    }
  }

  async function submitRun(event: FormEvent) {
    event.preventDefault();
    await guarded(async () => {
      if (!message.trim()) return;
      const run = await postJson<Run>("/api/runs", {
        message,
        session_id: sessionId.trim() || null,
        workspace: workspace.trim() || null,
        provider: provider.trim() || null,
        model: model.trim() || null,
        autonomy_mode: autonomyMode
      });
      setMessage("");
      setActiveRunId(run.run_id);
      await refreshSummary();
      await refreshRunDetails(run.run_id);
    }, "Run queued.");
  }

  async function selectRun(runId: string) {
    setActiveRunId(runId);
    await guarded(async () => refreshRunDetails(runId));
  }

  async function decideApproval(approval: Approval, approved: boolean) {
    await guarded(async () => {
      await postJson(`/api/approvals/${approval.approval_id}/decision`, {
        approved,
        arguments: approval.arguments
      });
      await refreshSummary();
      if (activeRun) await refreshRunDetails(activeRun.run_id);
    }, approved ? "Approval accepted." : "Approval denied.");
  }

  async function approveTask(task: TaskNode) {
    if (!activeRun) return;
    await guarded(async () => {
      await postJson(`/api/runs/${activeRun.run_id}/approve-task`, { task_id: task.task_id });
      await refreshSummary();
      await refreshRunDetails(activeRun.run_id);
    }, "Task approved.");
  }

  async function runScheduler(mode: "step" | "run") {
    if (!activeRun) return;
    await guarded(async () => {
      const payload =
        mode === "step"
          ? { max_tasks: Number(schedulerTasks) || null }
          : { max_tasks: Number(schedulerTasks) || null, max_cycles: Number(schedulerCycles) || null };
      const result = await postJson<Record<string, unknown>>(`/api/runs/${activeRun.run_id}/scheduler/${mode}`, payload);
      setSchedulerResult(result);
      await refreshSummary();
      await refreshRunDetails(activeRun.run_id);
    }, mode === "step" ? "Scheduler step complete." : "Scheduler drain complete.");
  }

  async function submitSubagent(event: FormEvent) {
    event.preventDefault();
    if (!activeRun) return;
    await guarded(async () => {
      await postJson("/api/subagents", {
        run_id: activeRun.run_id,
        profile: subagentProfile,
        goal: subagentGoal
      });
      setSubagentGoal("");
      await refreshRunDetails(activeRun.run_id);
    }, "Subagent queued.");
  }

  async function searchMemory(event?: FormEvent) {
    event?.preventDefault();
    await guarded(async () => {
      if (!memoryQuery.trim()) return;
      const params = queryString({ query: memoryQuery, k: 12 });
      const hits = await getJson<MemoryHit[]>(`/api/memory/search${params}`);
      const inspected = await getJson<Record<string, unknown>>(`/api/memory/inspect${params}`);
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
        include_telemetry: true
      });
      setContextResult(await getJson<ContextPackResult>(`/api/context${params}`));
    });
  }

  async function findConflicts() {
    await guarded(async () => {
      const query = contextQuery.trim() || memoryQuery.trim();
      if (!query) return;
      setConflictResult(await getJson<Record<string, unknown>>(`/api/memory/conflicts${queryString({ query, k: 8 })}`));
    });
  }

  async function submitLearning(event: FormEvent) {
    event.preventDefault();
    await guarded(async () => {
      const result = await postJson<Record<string, unknown>>("/api/memory/learn", {
        title: learningTitle,
        content: learningContent,
        kind: learningKind,
        validation_score: Number(learningValidation),
        repeat_count: Number(learningRepeat),
        explicit_instruction: learningExplicit
      });
      setLearningResult(result);
      await refreshAll();
    }, "Learning signal reviewed.");
  }

  async function capsule(action: "summarize" | "apply") {
    if (!activeRun) return;
    await guarded(async () => {
      const result = await postJson<Record<string, unknown>>(`/api/capsules/${activeRun.run_id}/${action}`, {
        dry_run: action === "summarize",
        include_policy: false
      });
      setCapsuleResult(result);
      await refreshAll();
    });
  }

  async function invokeTool(event: FormEvent) {
    event.preventDefault();
    await guarded(async () => {
      const args = readJson<Record<string, unknown>>(toolArgs, {});
      const result = await postJson<Record<string, unknown>>(`/api/tools/${encodeURIComponent(toolName)}/invoke`, {
        arguments: args,
        session_id: activeRun?.session_id ?? "manual",
        run_id: activeRun?.run_id ?? null
      });
      setToolResult(result);
      await refreshSummary();
    });
  }

  function loadMcp(server: McpServer) {
    setMcpId(server.id);
    setMcpName(server.name);
    setMcpTransport(server.transport);
    setMcpEndpoint(server.transport === "stdio" ? server.command ?? "" : server.url ?? "");
    setMcpArgs(JSON.stringify(server.args ?? [], null, 2));
    setMcpEnv(JSON.stringify(server.env ?? {}, null, 2));
    setMcpSecretEnv(JSON.stringify(server.secret_env ?? {}, null, 2));
    setMcpRiskPolicy(server.risk_policy ?? "approval_by_default");
    setMcpEnabled(server.enabled);
  }

  async function saveMcp(event: FormEvent) {
    event.preventDefault();
    await guarded(async () => {
      const payload = {
        id: mcpId,
        name: mcpName || mcpId,
        transport: mcpTransport,
        command: mcpTransport === "stdio" ? mcpEndpoint || null : null,
        url: mcpTransport === "stdio" ? null : mcpEndpoint || null,
        args: readJson<string[]>(mcpArgs, []),
        env: readJson<Record<string, string>>(mcpEnv, {}),
        secret_env: readJson<Record<string, string>>(mcpSecretEnv, {}),
        enabled: mcpEnabled,
        tools: [],
        risk_policy: mcpRiskPolicy
      };
      const path = mcpServers.some((server) => server.id === mcpId) ? `/api/mcp/servers/${encodeURIComponent(mcpId)}` : "/api/mcp/servers";
      const saved = path === "/api/mcp/servers" ? await postJson<McpServer>(path, payload) : await putJson<McpServer>(path, payload);
      setMcpId(saved.id);
      await refreshSummary();
    }, "MCP server saved.");
  }

  async function controlMcp(server: McpServer, action: "connect" | "disconnect" | "restart" | "sync" | "test") {
    await guarded(async () => {
      const result = await postJson<Record<string, unknown>>(`/api/mcp/servers/${encodeURIComponent(server.id)}/${action}`);
      setMcpResult(result);
      await refreshSummary();
    });
  }

  async function deleteMcp(server: McpServer) {
    await guarded(async () => {
      await deleteJson(`/api/mcp/servers/${encodeURIComponent(server.id)}`);
      await refreshSummary();
    }, "MCP server removed.");
  }

  async function invokeMcp(event: FormEvent) {
    event.preventDefault();
    await guarded(async () => {
      const [serverId, remoteName] = mcpToolSelection.split("::");
      const result = await postJson<Record<string, unknown>>(
        `/api/mcp/servers/${encodeURIComponent(serverId)}/tools/${encodeURIComponent(remoteName)}/invoke`,
        { arguments: readJson<Record<string, unknown>>(mcpToolArgs, {}) }
      );
      setMcpResult(result);
      await refreshSummary();
    });
  }

  async function toggleSkill(skill: Skill) {
    await guarded(async () => {
      await postJson(`/api/skills/${encodeURIComponent(skill.id)}/${skill.enabled ? "disable" : "enable"}`);
      await refreshSummary();
    });
  }

  async function discoverSkills() {
    await guarded(async () => {
      await postJson("/api/skills/discover");
      await refreshSummary();
    }, "Skills discovered.");
  }

  async function installSkill(event: FormEvent) {
    event.preventDefault();
    await guarded(async () => {
      const result = await postJson<Record<string, unknown>>("/api/skills/install", {
        manifest: readJson<Record<string, unknown>>(skillManifest, {}),
        instructions: skillInstructions,
        overwrite: true,
        dry_run: false
      });
      setSkillResult(result);
      await refreshSummary();
    });
  }

  async function runSkill(event: FormEvent) {
    event.preventDefault();
    await guarded(async () => {
      const result = await postJson<Record<string, unknown>>(`/api/skills/${encodeURIComponent(skillSelection)}/run`, {
        arguments: { task: skillTask, context: { active_run_id: activeRun?.run_id ?? null } },
        session_id: activeRun?.session_id ?? "manual",
        run_id: activeRun?.run_id ?? null
      });
      setSkillResult(result);
      await refreshSummary();
    });
  }

  async function installPlugin(event: FormEvent) {
    event.preventDefault();
    await guarded(async () => {
      const result = await postJson<Record<string, unknown>>("/api/plugins/install", {
        source: pluginSource,
        ref: pluginRef || null,
        enable: pluginEnable,
        overwrite: true
      });
      setPluginResult(result);
      await refreshSummary();
    });
  }

  async function pluginAction(plugin: Plugin, action: "enable" | "disable" | "update" | "remove") {
    await guarded(async () => {
      const path = `/api/plugins/${encodeURIComponent(plugin.id)}`;
      const result =
        action === "remove"
          ? await deleteJson<Record<string, unknown>>(path)
          : await postJson<Record<string, unknown>>(`${path}/${action}`, action === "update" ? { ref: plugin.source_ref } : {});
      setPluginResult(result);
      await refreshSummary();
    });
  }

  function loadChannel(channel: Channel) {
    setChannelId(channel.id);
    setChannelProvider(channel.provider);
    setChannelTokenEnv(channel.token_env ?? "");
    setChannelWebhookEnv(channel.webhook_url_env ?? "");
    setChannelEnabled(channel.enabled);
    setChannelSendEnabled(channel.send_enabled);
    setChannelAutoReply(channel.auto_reply);
    setChannelSettings(JSON.stringify(channel.settings ?? {}, null, 2));
  }

  async function saveChannel(event: FormEvent) {
    event.preventDefault();
    await guarded(async () => {
      const payload = {
        id: channelId,
        provider: channelProvider,
        enabled: channelEnabled,
        send_enabled: channelSendEnabled,
        auto_reply: channelAutoReply,
        token_env: channelTokenEnv || null,
        webhook_url_env: channelWebhookEnv || null,
        settings: readJson<Record<string, unknown>>(channelSettings, {})
      };
      const path = channels.some((channel) => channel.id === channelId) ? `/api/channels/${encodeURIComponent(channelId)}` : "/api/channels";
      const saved = path === "/api/channels" ? await postJson<Channel>(path, payload) : await putJson<Channel>(path, payload);
      setChannelId(saved.id);
      await refreshSummary();
    }, "Channel saved.");
  }

  async function deleteChannel(channel: Channel) {
    await guarded(async () => {
      await deleteJson(`/api/channels/${encodeURIComponent(channel.id)}`);
      await refreshSummary();
    }, "Channel removed.");
  }

  async function ingestChannel(event: FormEvent) {
    event.preventDefault();
    await guarded(async () => {
      const result = await postJson<Record<string, unknown>>("/api/channels/ingest", {
        provider: channelProvider,
        channel_id: channelId,
        payload: readJson<Record<string, unknown>>(channelPayload, {}),
        send: false
      });
      setChannelResult(result);
      await refreshAll();
    });
  }

  async function diagnose(event: FormEvent) {
    event.preventDefault();
    await guarded(async () => {
      const result = await postJson<Record<string, unknown>>("/api/diagnosis/recall", {
        failure_text: diagnosisText,
        source: "web-ui",
        k: 5
      });
      setDiagnosisResult(result);
    });
  }

  return (
    <div className="app-shell">
      <a className="skip-link" href="#workspace">Skip to workspace</a>
      <aside className="sidebar" aria-label="Kestrel sections">
        <div className="brand">
          <span className="brand-mark" aria-hidden="true">
            <Bot size={24} />
          </span>
          <div>
            <strong>Kestrel</strong>
            <span>Local operator console</span>
          </div>
        </div>
        <nav>
          <a href="#workspace"><Home size={17} /> Workspace</a>
          <a href="#runs"><Route size={17} /> Runs</a>
          <a href="#approvals"><ShieldCheck size={17} /> Approvals</a>
          <a href="#memory"><Database size={17} /> Memory</a>
          <a href="#tools"><Wrench size={17} /> Tools</a>
          <a href="#mcp"><PlugZap size={17} /> MCP</a>
          <a href="#skills"><Sparkles size={17} /> Skills</a>
          <a href="#channels"><Bell size={17} /> Channels</a>
          <a href="#observability"><LineChart size={17} /> Traces</a>
          <a href="#settings"><Settings size={17} /> Settings</a>
        </nav>
      </aside>

      <main className="workspace" id="workspace">
        <header className="topbar">
          <div>
            <p className="eyebrow">Local-first runtime</p>
            <h1>Agent Workspace</h1>
          </div>
          <div className="topbar-actions">
            <StatusBadge value={activeRun?.status ?? "ready"} />
            <button type="button" onClick={() => refreshAll().catch(reportError)}>
              <RefreshCw size={15} /> Refresh
            </button>
          </div>
        </header>

        <div className="announcer" aria-live="polite">
          {notice}
        </div>
        {error && (
          <div className="alert" role="alert">
            <strong>Action failed</strong>
            <span>{error}</span>
            <button type="button" onClick={() => setError(null)} aria-label="Dismiss error">
              <X size={15} />
            </button>
          </div>
        )}

        <section className="workspace-grid">
          <Panel
            title="Run Agent"
            icon={<TerminalSquare size={19} />}
            actions={<StatusBadge value={runtime ? "runtime loaded" : "loading"} />}
            className="primary-panel"
          >
            <form className="run-form" onSubmit={submitRun}>
              <Field label="Objective">
                <textarea value={message} onChange={(event) => setMessage(event.target.value)} rows={5} />
              </Field>
              <div className="form-grid">
                <Field label="Session ID" hint="Leave blank to create a new session.">
                  <input value={sessionId} onChange={(event) => setSessionId(event.target.value)} />
                </Field>
                <Field label="Workspace" hint="Leave blank for configured workspace.">
                  <input value={workspace} onChange={(event) => setWorkspace(event.target.value)} />
                </Field>
                <Field label="Provider">
                  <input list="providers" value={provider} onChange={(event) => setProvider(event.target.value)} />
                </Field>
                <Field label="Model">
                  <input value={model} onChange={(event) => setModel(event.target.value)} />
                </Field>
                <Field label="Autonomy">
                  <select value={autonomyMode} onChange={(event) => setAutonomyMode(event.target.value)}>
                    {autonomyOptions.map((option) => (
                      <option key={option.value} value={option.value}>
                        {option.label}
                      </option>
                    ))}
                  </select>
                </Field>
              </div>
              <datalist id="providers">
                {providerOptions.map((item) => (
                  <option key={item} value={item} />
                ))}
              </datalist>
              <div className="button-row">
                <button type="submit" disabled={!message.trim()}>
                  <Send size={15} /> Queue Run
                </button>
                {activeRun?.status === "running" && (
                  <button
                    type="button"
                    className="danger"
                    onClick={() => guarded(async () => {
                      await postJson(`/api/runs/${activeRun.run_id}/cancel`);
                      await refreshSummary();
                    })}
                  >
                    <Square size={14} /> Cancel
                  </button>
                )}
              </div>
            </form>
          </Panel>

          <Panel title="Active Run" icon={<Activity size={19} />} className="primary-panel">
            {activeRun ? (
              <div className="run-detail">
                <div className="run-title">
                  <h3>{activeRun.message}</h3>
                  <StatusBadge value={activeRun.stop_reason || activeRun.status} />
                </div>
                <InlineMeta
                  items={[
                    activeRun.run_id,
                    activeRun.session_id,
                    activeRun.provider ?? "provider",
                    activeRun.model,
                    `${activeRun.tool_count} tools`,
                    `${activeRun.context_chars} chars`
                  ]}
                />
                <div className="transcript" aria-live="polite">
                  <article className="bubble user">
                    <strong>User</strong>
                    <p>{activeRun.message}</p>
                  </article>
                  <article className="bubble assistant">
                    <strong>Kestrel</strong>
                    <p>{activeRun.assistant_message || streamedAssistant || activeRun.stop_reason || "Working..."}</p>
                  </article>
                </div>
                {proofOfWork && (
                  <div className="proof-grid">
                    <SummaryList title="Completed" values={asStringArray(proofOfWork.completed_steps)} />
                    <SummaryList title="Validation" values={asStringArray(proofOfWork.validation_evidence)} />
                    <SummaryList title="Remaining Risks" values={asStringArray(proofOfWork.remaining_risks)} />
                  </div>
                )}
              </div>
            ) : (
              <EmptyState>No run selected.</EmptyState>
            )}
          </Panel>
        </section>

        <section id="runs" className="content-grid">
          <Panel title="Runs" icon={<Route size={19} />}>
            <div className="list compact-list">
              {runs.map((run) => (
                <button type="button" className="row-button" key={run.run_id} onClick={() => selectRun(run.run_id)}>
                  <span>
                    <strong>{run.message || run.run_id}</strong>
                    <small>{run.session_id} / {run.model}</small>
                  </span>
                  <StatusBadge value={run.status} />
                </button>
              ))}
              {runs.length === 0 && <EmptyState>No runs yet.</EmptyState>}
            </div>
          </Panel>

          <Panel title="Task Graph & Scheduler" icon={<ClipboardCheck size={19} />}>
            <div className="scheduler-controls">
              <Field label="Max tasks">
                <input value={schedulerTasks} onChange={(event) => setSchedulerTasks(event.target.value)} inputMode="numeric" />
              </Field>
              <Field label="Max cycles">
                <input value={schedulerCycles} onChange={(event) => setSchedulerCycles(event.target.value)} inputMode="numeric" />
              </Field>
              <button type="button" disabled={!activeRun} onClick={() => runScheduler("step")}>Step</button>
              <button type="button" disabled={!activeRun} onClick={() => runScheduler("run")}>Run Until Idle</button>
            </div>
            <TaskList title="Approval blocked" tasks={taskGraph?.approval_blocked_tasks ?? []} onApprove={approveTask} />
            <TaskList title="Ready" tasks={taskGraph?.ready_tasks ?? []} onApprove={approveTask} />
            <TaskList title="All tasks" tasks={taskGraph?.tasks ?? []} onApprove={approveTask} />
            {schedulerResult && <JsonBlock value={schedulerResult} />}
          </Panel>

          <Panel title="Subagents" icon={<Bot size={19} />}>
            <form onSubmit={submitSubagent} className="stack-form">
              <Field label="Profile">
                <select value={subagentProfile} onChange={(event) => setSubagentProfile(event.target.value)}>
                  <option value="worker">Worker</option>
                  <option value="planner">Planner</option>
                  <option value="reviewer">Reviewer</option>
                </select>
              </Field>
              <Field label="Bounded goal">
                <textarea value={subagentGoal} onChange={(event) => setSubagentGoal(event.target.value)} rows={4} />
              </Field>
              <button type="submit" disabled={!activeRun || !subagentGoal.trim()}>Queue Subagent</button>
            </form>
            {(taskGraph?.subagents ?? []).map((subagent) => (
              <div className="data-row" key={subagent.subagent_id}>
                <strong>{subagent.profile}</strong>
                <StatusBadge value={subagent.status} />
                <p>{subagent.result || subagent.error || subagent.goal}</p>
              </div>
            ))}
          </Panel>

          <Panel title="Sessions" icon={<ServerCog size={19} />}>
            <div className="list compact-list">
              {sessions.map((session) => (
                <button type="button" className="row-button" key={session.session_id} onClick={() => selectRun(session.latest_run_id)}>
                  <span>
                    <strong>{session.session_id}</strong>
                    <small>{session.latest_message}</small>
                  </span>
                  <StatusBadge value={`${session.run_count} runs`} />
                </button>
              ))}
            </div>
          </Panel>
        </section>

        <section id="approvals" className="content-grid">
          <Panel title="Pending Approvals" icon={<ShieldCheck size={19} />}>
            {approvals.map((approval) => (
              <ApprovalCard key={approval.approval_id} approval={approval} onApprove={decideApproval} />
            ))}
            {approvals.length === 0 && <EmptyState>No blocked actions.</EmptyState>}
          </Panel>
          <Panel title="Approval History" icon={<ClipboardCheck size={19} />}>
            <div className="list">
              {allApprovals.slice(0, 20).map((approval) => (
                <div className="data-row" key={approval.approval_id}>
                  <strong>{approval.tool_name}</strong>
                  <InlineMeta items={[approval.run_id, approval.risk, approval.created_at]} />
                  <StatusBadge value={approval.status} />
                  {approval.result && <JsonBlock value={approval.result} maxHeight="120px" />}
                </div>
              ))}
            </div>
          </Panel>
        </section>

        <section id="memory" className="content-grid wide-left">
          <Panel title="Memory & Context" icon={<Database size={19} />}>
            <div className="layer-grid">
              {memoryLayers.map((layer) => (
                <div className="layer-chip" key={layer.layer}>
                  <strong>{layer.layer}</strong>
                  <StatusBadge value={layer.ok ? "ok" : "failed"} />
                  <small>{layer.backend}</small>
                </div>
              ))}
            </div>
            <form onSubmit={searchMemory} className="inline-form">
              <Field label="Memory query">
                <input value={memoryQuery} onChange={(event) => setMemoryQuery(event.target.value)} />
              </Field>
              <button type="submit"><Search size={15} /> Search</button>
            </form>
            <div className="hit-list">
              {memoryHits.map((hit) => (
                <div className="data-row" key={`${hit.layer}-${hit.record_id ?? hit.title}`}>
                  <strong>{hit.title}</strong>
                  <InlineMeta items={[hit.layer, hit.kind, hit.score.toFixed(2)]} />
                  <p>{hit.snippet}</p>
                </div>
              ))}
            </div>
          </Panel>

          <Panel title="Context Pack" icon={<FileText size={19} />}>
            <form onSubmit={packContext} className="stack-form">
              <Field label="Objective or claim">
                <input value={contextQuery} onChange={(event) => setContextQuery(event.target.value)} />
              </Field>
              <Field label="Layers CSV">
                <input value={contextLayers} onChange={(event) => setContextLayers(event.target.value)} />
              </Field>
              <Field label="Token budget">
                <input value={contextBudget} onChange={(event) => setContextBudget(event.target.value)} inputMode="numeric" />
              </Field>
              <label className="check-row">
                <input type="checkbox" checked={contextExpandRaw} onChange={(event) => setContextExpandRaw(event.target.checked)} />
                <span>Expand raw evidence</span>
              </label>
              <div className="button-row">
                <button type="submit">Pack</button>
                <button type="button" onClick={findConflicts}>Find Conflicts</button>
                <button type="button" disabled={!activeRun} onClick={() => capsule("summarize")}>Capsule Preview</button>
                <button type="button" disabled={!activeRun} onClick={() => capsule("apply")}>Request Capsule Apply</button>
              </div>
            </form>
            {contextResult && <JsonBlock value={contextResult.packed_prompt || contextResult} maxHeight="360px" />}
            {conflictResult && <JsonBlock value={conflictResult} />}
            {capsuleResult && <JsonBlock value={capsuleResult} />}
          </Panel>

          <Panel title="Learning Review" icon={<Brain size={19} />}>
            <form onSubmit={submitLearning} className="stack-form">
              <Field label="Title">
                <input value={learningTitle} onChange={(event) => setLearningTitle(event.target.value)} />
              </Field>
              <Field label="Validated content">
                <textarea value={learningContent} onChange={(event) => setLearningContent(event.target.value)} rows={4} />
              </Field>
              <div className="form-grid">
                <Field label="Kind">
                  <select value={learningKind} onChange={(event) => setLearningKind(event.target.value)}>
                    {["observation", "fact", "event", "failure", "procedure", "policy"].map((kind) => (
                      <option key={kind} value={kind}>{kind}</option>
                    ))}
                  </select>
                </Field>
                <Field label="Validation score">
                  <input value={learningValidation} onChange={(event) => setLearningValidation(event.target.value)} inputMode="decimal" />
                </Field>
                <Field label="Repeat count">
                  <input value={learningRepeat} onChange={(event) => setLearningRepeat(event.target.value)} inputMode="numeric" />
                </Field>
              </div>
              <label className="check-row">
                <input type="checkbox" checked={learningExplicit} onChange={(event) => setLearningExplicit(event.target.checked)} />
                <span>Explicit instruction</span>
              </label>
              <button type="submit">Review Learning Signal</button>
            </form>
            {learningResult && <JsonBlock value={learningResult} />}
          </Panel>

          <Panel title="Lessons & Failures" icon={<TestTube2 size={19} />}>
            <h3>Lessons</h3>
            <RecordList records={lessons} />
            <h3>Failure Episodes</h3>
            <RecordList records={failures} />
            <form onSubmit={diagnose} className="stack-form">
              <Field label="Diagnose failure text">
                <textarea value={diagnosisText} onChange={(event) => setDiagnosisText(event.target.value)} rows={4} />
              </Field>
              <button type="submit">Classify & Recall Lessons</button>
            </form>
            {diagnosisResult && <JsonBlock value={diagnosisResult} />}
          </Panel>
        </section>

        <section id="tools" className="content-grid">
          <Panel title="Tool Inventory" icon={<Wrench size={19} />}>
            <form onSubmit={invokeTool} className="stack-form">
              <Field label="Tool">
                <select
                  value={toolName}
                  onChange={(event) => {
                    const selected = tools.find((tool) => tool.name === event.target.value);
                    setToolName(event.target.value);
                    setToolArgs(JSON.stringify(schemaDefault(selected?.parameters), null, 2));
                  }}
                >
                  <option value="">Select a tool</option>
                  {tools.map((tool) => (
                    <option key={tool.name} value={tool.name}>{tool.name}</option>
                  ))}
                </select>
              </Field>
              <Field label="Arguments JSON">
                <textarea value={toolArgs} onChange={(event) => setToolArgs(event.target.value)} rows={8} />
              </Field>
              <button type="submit" disabled={!toolName}>Invoke Tool</button>
            </form>
            <div className="tool-grid">
              {tools.map((tool) => (
                <button
                  type="button"
                  className="tool-card"
                  key={tool.name}
                  onClick={() => {
                    setToolName(tool.name);
                    setToolArgs(JSON.stringify(schemaDefault(tool.parameters), null, 2));
                  }}
                >
                  <strong>{tool.name}</strong>
                  <InlineMeta items={[tool.source, tool.risk, tool.requires_approval ? "approval" : "direct"]} />
                  <span>{tool.description}</span>
                </button>
              ))}
            </div>
          </Panel>
          <Panel title="Tool Result" icon={<Activity size={19} />}>
            {toolResult ? <JsonBlock value={toolResult} maxHeight="520px" /> : <EmptyState>No tool invoked from the UI yet.</EmptyState>}
          </Panel>
        </section>

        <section id="mcp" className="content-grid wide-left">
          <Panel title="MCP Servers" icon={<PlugZap size={19} />}>
            <form onSubmit={saveMcp} className="stack-form">
              <div className="form-grid">
                <Field label="Server ID"><input value={mcpId} onChange={(event) => setMcpId(event.target.value)} /></Field>
                <Field label="Name"><input value={mcpName} onChange={(event) => setMcpName(event.target.value)} /></Field>
                <Field label="Transport">
                  <select value={mcpTransport} onChange={(event) => setMcpTransport(event.target.value)}>
                    <option value="stdio">stdio</option>
                    <option value="streamable_http">streamable_http</option>
                    <option value="sse">sse</option>
                  </select>
                </Field>
                <Field label="Command or URL"><input value={mcpEndpoint} onChange={(event) => setMcpEndpoint(event.target.value)} /></Field>
                <Field label="Risk policy">
                  <select value={mcpRiskPolicy} onChange={(event) => setMcpRiskPolicy(event.target.value)}>
                    <option value="approval_by_default">approval_by_default</option>
                    <option value="trust_manifest">trust_manifest</option>
                  </select>
                </Field>
              </div>
              <label className="check-row">
                <input type="checkbox" checked={mcpEnabled} onChange={(event) => setMcpEnabled(event.target.checked)} />
                <span>Enabled</span>
              </label>
              <Field label="Args JSON"><textarea value={mcpArgs} onChange={(event) => setMcpArgs(event.target.value)} rows={3} /></Field>
              <Field label="Env JSON"><textarea value={mcpEnv} onChange={(event) => setMcpEnv(event.target.value)} rows={3} /></Field>
              <Field label="Secret env names JSON">
                <textarea value={mcpSecretEnv} onChange={(event) => setMcpSecretEnv(event.target.value)} rows={3} />
              </Field>
              <button type="submit" disabled={!mcpId.trim()}>Save Server</button>
            </form>
            {mcpServers.map((server) => (
              <div className="data-row" key={server.id}>
                <button type="button" className="link-button" onClick={() => loadMcp(server)}>{server.name}</button>
                <InlineMeta items={[server.id, server.transport, server.session_state, `${server.tool_count ?? server.tools.length} tools`]} />
                <StatusBadge value={server.status} />
                {server.error && <p className="danger-text">{server.error}</p>}
                <div className="button-row">
                  {(["connect", "sync", "test", "restart", "disconnect"] as const).map((action) => (
                    <button type="button" key={action} onClick={() => controlMcp(server, action)}>{action}</button>
                  ))}
                  <button type="button" className="danger" onClick={() => deleteMcp(server)}>Delete</button>
                </div>
              </div>
            ))}
          </Panel>
          <Panel title="MCP Tool Invoke" icon={<Wrench size={19} />}>
            <form onSubmit={invokeMcp} className="stack-form">
              <Field label="MCP tool">
                <select
                  value={mcpToolSelection}
                  onChange={(event) => {
                    setMcpToolSelection(event.target.value);
                    const option = mcpToolOptions.find((item) => item.value === event.target.value);
                    setMcpToolArgs(JSON.stringify(schemaDefault(option?.tool.parameters), null, 2));
                  }}
                >
                  <option value="">Select tool</option>
                  {mcpToolOptions.map(({ server, tool, value }) => (
                    <option key={value} value={value}>{server.id} / {tool.remote_name ?? tool.name}</option>
                  ))}
                </select>
              </Field>
              <Field label="Arguments JSON"><textarea value={mcpToolArgs} onChange={(event) => setMcpToolArgs(event.target.value)} rows={8} /></Field>
              <button type="submit" disabled={!mcpToolSelection}>Invoke MCP Tool</button>
            </form>
            {mcpResult && <JsonBlock value={mcpResult} maxHeight="420px" />}
          </Panel>
        </section>

        <section id="skills" className="content-grid">
          <Panel title="Skills" icon={<Sparkles size={19} />} actions={<button type="button" onClick={discoverSkills}>Discover</button>}>
            <div className="list">
              {skills.map((skill) => (
                <div className="data-row" key={skill.id}>
                  <button type="button" className="link-button" onClick={() => setSkillSelection(skill.id)}>{skill.name}</button>
                  <InlineMeta items={[skill.id, skill.enabled ? "enabled" : "disabled"]} />
                  <p>{skill.description}</p>
                  <button type="button" onClick={() => toggleSkill(skill)}>{skill.enabled ? "Disable" : "Enable"}</button>
                </div>
              ))}
            </div>
          </Panel>
          <Panel title="Run or Install Skill" icon={<Bot size={19} />}>
            <form onSubmit={runSkill} className="stack-form">
              <Field label="Skill">
                <select value={skillSelection} onChange={(event) => setSkillSelection(event.target.value)}>
                  <option value="">Select skill</option>
                  {skills.map((skill) => <option key={skill.id} value={skill.id}>{skill.id}</option>)}
                </select>
              </Field>
              <Field label="Skill task"><textarea value={skillTask} onChange={(event) => setSkillTask(event.target.value)} rows={3} /></Field>
              <button type="submit" disabled={!skillSelection || !skillTask.trim()}>Run Skill</button>
            </form>
            <form onSubmit={installSkill} className="stack-form separated">
              <Field label="Skill manifest JSON"><textarea value={skillManifest} onChange={(event) => setSkillManifest(event.target.value)} rows={7} /></Field>
              <Field label="Skill instructions"><textarea value={skillInstructions} onChange={(event) => setSkillInstructions(event.target.value)} rows={5} /></Field>
              <button type="submit" disabled={!skillInstructions.trim()}>Install Skill</button>
            </form>
            {skillResult && <JsonBlock value={skillResult} maxHeight="360px" />}
          </Panel>
          <Panel title="Plugins" icon={<GitBranch size={19} />}>
            <form onSubmit={installPlugin} className="inline-form">
              <Field label="GitHub source"><input value={pluginSource} onChange={(event) => setPluginSource(event.target.value)} /></Field>
              <Field label="Ref"><input value={pluginRef} onChange={(event) => setPluginRef(event.target.value)} /></Field>
              <label className="check-row">
                <input type="checkbox" checked={pluginEnable} onChange={(event) => setPluginEnable(event.target.checked)} />
                <span>Enable after install</span>
              </label>
              <button type="submit" disabled={!pluginSource.trim()}>Install</button>
            </form>
            {plugins.map((plugin) => (
              <div className="data-row" key={plugin.id}>
                <strong>{plugin.name}</strong>
                <InlineMeta items={[plugin.id, plugin.format, plugin.install_status, plugin.enabled ? "enabled" : "disabled"]} />
                <p>{plugin.description}</p>
                <div className="button-row">
                  <button type="button" onClick={() => pluginAction(plugin, plugin.enabled ? "disable" : "enable")}>{plugin.enabled ? "Disable" : "Enable"}</button>
                  <button type="button" onClick={() => pluginAction(plugin, "update")}>Update</button>
                  <button type="button" className="danger" onClick={() => pluginAction(plugin, "remove")}>Remove</button>
                </div>
              </div>
            ))}
            {pluginResult && <JsonBlock value={pluginResult} maxHeight="320px" />}
          </Panel>
        </section>

        <section id="channels" className="content-grid">
          <Panel title="Channels" icon={<Bell size={19} />}>
            <form onSubmit={saveChannel} className="stack-form">
              <div className="form-grid">
                <Field label="Channel ID"><input value={channelId} onChange={(event) => setChannelId(event.target.value)} /></Field>
                <Field label="Provider"><input value={channelProvider} onChange={(event) => setChannelProvider(event.target.value)} /></Field>
                <Field label="Token env"><input value={channelTokenEnv} onChange={(event) => setChannelTokenEnv(event.target.value)} /></Field>
                <Field label="Webhook URL env"><input value={channelWebhookEnv} onChange={(event) => setChannelWebhookEnv(event.target.value)} /></Field>
              </div>
              <div className="check-grid">
                <label className="check-row"><input type="checkbox" checked={channelEnabled} onChange={(event) => setChannelEnabled(event.target.checked)} /><span>Enabled</span></label>
                <label className="check-row"><input type="checkbox" checked={channelSendEnabled} onChange={(event) => setChannelSendEnabled(event.target.checked)} /><span>Send enabled</span></label>
                <label className="check-row"><input type="checkbox" checked={channelAutoReply} onChange={(event) => setChannelAutoReply(event.target.checked)} /><span>Auto reply</span></label>
              </div>
              <Field label="Settings JSON"><textarea value={channelSettings} onChange={(event) => setChannelSettings(event.target.value)} rows={4} /></Field>
              <button type="submit">Save Channel</button>
            </form>
            {channels.map((channel) => (
              <div className="data-row" key={channel.id}>
                <button type="button" className="link-button" onClick={() => loadChannel(channel)}>{channel.id}</button>
                <InlineMeta items={[channel.provider, channel.enabled ? "enabled" : "disabled", channel.send_enabled ? "send" : "dry-run"]} />
                <StatusBadge value={channel.auto_reply ? "auto reply" : "manual"} />
                <div className="button-row">
                  <button type="button" onClick={() => deleteChannel(channel)} className="danger">Delete</button>
                </div>
              </div>
            ))}
          </Panel>
          <Panel title="Webhook Tester" icon={<Send size={19} />}>
            <form onSubmit={ingestChannel} className="stack-form">
              <Field label="Payload JSON"><textarea value={channelPayload} onChange={(event) => setChannelPayload(event.target.value)} rows={8} /></Field>
              <button type="submit">Dry-run Ingest</button>
            </form>
            <div className="webhook-note">
              <strong>Webhook URL</strong>
              <code>/api/channels/{channelProvider}/webhook?channel_id={channelId}&amp;send=false</code>
            </div>
            {channelResult && <JsonBlock value={channelResult} maxHeight="360px" />}
          </Panel>
        </section>

        <section id="observability" className="content-grid wide-left">
          <Panel title="Run Trace" icon={<LineChart size={19} />}>
            {runTrace ? (
              <>
                <div className="metric-row">
                  <StatusBadge value={`${runTrace.summary.event_count} events`} />
                  {Object.entries(runTrace.summary.trace_counts).map(([name, count]) => (
                    <StatusBadge key={name} value={`${name}: ${count}`} />
                  ))}
                </div>
                <div className="trace-list">
                  {runTrace.timeline.slice(-80).map((event) => (
                    <div className="trace-row" key={event.id}>
                      <strong>{event.type}</strong>
                      <small>{event.created_at}</small>
                      <code>{JSON.stringify(event.payload).slice(0, 360)}</code>
                    </div>
                  ))}
                </div>
              </>
            ) : (
              <EmptyState>No run trace selected.</EmptyState>
            )}
          </Panel>
          <Panel title="JSONL Logs" icon={<Layers size={19} />}>
            <div className="trace-list">
              {logs.map((log) => (
                <div className="trace-row" key={log.id}>
                  <strong>{log.type}</strong>
                  <small>{log.created_at}</small>
                  <code>{JSON.stringify(log.payload).slice(0, 360)}</code>
                </div>
              ))}
            </div>
          </Panel>
        </section>

        <section id="settings" className="content-grid">
          <Panel title="Settings & Health" icon={<Settings size={19} />}>
            {runtime ? (
              <>
                <div className="metric-grid">
                  <Metric label="Runs" value={runs.length} />
                  <Metric label="Approvals" value={approvals.length} />
                  <Metric label="MCP Servers" value={mcpServers.length} />
                  <Metric label="Skills" value={skills.length} />
                </div>
                <h3>Feature Flags</h3>
                <div className="flag-grid">
                  {Object.entries((runtime as RuntimeConfig).feature_flags ?? {}).map(([key, value]) => (
                    <span key={key} className="flag"><StatusBadge value={value} /> {key}</span>
                  ))}
                </div>
              </>
            ) : (
              <EmptyState>Runtime config is loading.</EmptyState>
            )}
          </Panel>
          <Panel title="Runtime Config" icon={<FileText size={19} />}>
            {runtime && <JsonBlock value={runtime} maxHeight="680px" />}
          </Panel>
        </section>
      </main>
    </div>
  );
}

function TaskList({ title, tasks, onApprove }: { title: string; tasks: TaskNode[]; onApprove: (task: TaskNode) => void }) {
  return (
    <div className="task-list">
      <h3>{title}</h3>
      {tasks.length === 0 && <EmptyState>No tasks in this group.</EmptyState>}
      {tasks.map((task) => (
        <div className="task-card" key={`${title}-${task.task_id}`}>
          <div>
            <strong>{task.title}</strong>
            <InlineMeta items={[task.profile, task.risk, task.scheduler_reason, `attempts ${task.attempt_count ?? 0}`]} />
          </div>
          <StatusBadge value={task.status} />
          <p>{task.goal}</p>
          {task.failure_reason && <p className="danger-text">{task.failure_reason}</p>}
          {!task.approved && (
            <button type="button" onClick={() => onApprove(task)}>
              <Check size={15} /> Approve Task
            </button>
          )}
          {(task.diagnosis || task.retry_strategy) && <JsonBlock value={{ diagnosis: task.diagnosis, retry_strategy: task.retry_strategy }} />}
        </div>
      ))}
    </div>
  );
}

function ApprovalCard({ approval, onApprove }: { approval: Approval; onApprove: (approval: Approval, approved: boolean) => void }) {
  return (
    <article className="approval-card">
      <div>
        <strong>{approval.tool_name}</strong>
        <InlineMeta items={[approval.risk, approval.run_id, approval.tool_call_id]} />
      </div>
      <JsonBlock value={approval.arguments} maxHeight="160px" />
      <div className="button-row">
        <button type="button" onClick={() => onApprove(approval, true)}><Check size={15} /> Approve</button>
        <button type="button" className="danger" onClick={() => onApprove(approval, false)}><X size={15} /> Deny</button>
      </div>
    </article>
  );
}

function RecordList({ records }: { records: Array<Record<string, unknown>> }) {
  if (records.length === 0) return <EmptyState>No records found.</EmptyState>;
  return (
    <div className="list">
      {records.slice(0, 8).map((item, index) => {
        const record = item.record as Record<string, unknown> | undefined;
        return (
          <div className="data-row" key={`${String(record?.id ?? "record")}-${index}`}>
            <strong>{String(record?.title ?? item.title ?? "Record")}</strong>
            <InlineMeta items={[String(record?.layer ?? ""), String(record?.kind ?? ""), scoreLabel(item.score)]} />
            <p>{String(record?.content ?? item.snippet ?? "").slice(0, 360)}</p>
          </div>
        );
      })}
    </div>
  );
}

function SummaryList({ title, values }: { title: string; values: string[] }) {
  return (
    <div className="summary-list">
      <h3>{title}</h3>
      {values.length === 0 ? <small>none</small> : values.slice(0, 5).map((value) => <span key={value}>{value}</span>)}
    </div>
  );
}

function Metric({ label, value }: { label: string; value: string | number }) {
  return (
    <div className="metric">
      <span>{label}</span>
      <strong>{value}</strong>
    </div>
  );
}

function readJson<T>(text: string, fallback: T): T {
  const trimmed = text.trim();
  if (!trimmed) return fallback;
  return JSON.parse(trimmed) as T;
}

function schemaDefault(schema?: Record<string, unknown>): Record<string, unknown> {
  const properties = schema?.properties;
  if (!properties || typeof properties !== "object") return {};
  return Object.fromEntries(Object.keys(properties).map((key) => [key, ""]));
}

function extractProofOfWork(trace: RunTrace | null): Record<string, unknown> | null {
  if (!trace) return null;
  for (const event of [...trace.timeline].reverse()) {
    const proof = event.payload.proof_of_work;
    if (proof && typeof proof === "object") return proof as Record<string, unknown>;
  }
  return null;
}

function asStringArray(value: unknown): string[] {
  return Array.isArray(value) ? value.map((item) => String(item)).filter(Boolean) : [];
}

function scoreLabel(value: unknown): string {
  return typeof value === "number" ? value.toFixed(2) : "";
}
