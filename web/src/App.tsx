import {
  Activity,
  Bell,
  Bot,
  Brain,
  Check,
  ClipboardCheck,
  Database,
  Feather,
  FileText,
  GitBranch,
  Home,
  KeyRound,
  Layers,
  LineChart,
  MessageCircle,
  PanelRightOpen,
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
import { FormEvent, useEffect, useMemo, useRef, useState } from "react";
import ReactMarkdown, { type Components } from "react-markdown";
import remarkGfm from "remark-gfm";
import { ApiAuthError, deleteJson, getJson, getLearningDashboard, postJson, putJson, queryString, subscribeJsonEvents } from "./api";
import { getApiToken, setApiToken } from "./auth";
import { EmptyState, Field, InlineMeta, JsonBlock, Panel, StatusBadge } from "./components";
import {
  activityItemsForEvents,
  assistantTextForRun,
  deriveThreadTitle,
  eventBelongsToRun,
  eventKey,
  eventTimestamp,
  friendlyEventLabel,
  riskLabel,
  summarizeArguments,
  type LiveActivityItem
} from "./runActivity";
import type {
  AgentLogEvent,
  ApiResult,
  Approval,
  BehaviorDeltaReport,
  Capability,
  CapabilityKind,
  CapabilityMutationResult,
  CapabilitySnapshot,
  LearningDashboard,
  Channel,
  ContextPackResult,
  McpServer,
  MemoryHit,
  MemoryLayerStatus,
  OnboardingProfile,
  PersonaPreset,
  Plugin,
  PluginReviewReport,
  ProviderModelCatalog,
  Run,
  RunTrace,
  RuntimeConfig,
  SelfState,
  SelfOnboardingSaveResult,
  SelfOnboardingState,
  Session,
  SetupReadinessReport,
  SecretRef,
  Skill,
  SkillDiscoveryReport,
  TaskGraph,
  TaskNode,
  ThreadSummary,
  Tool,
  TraceEvent
} from "./types";

type ProviderOption = {
  value: string;
  label: string;
  group: "Local" | "Cloud" | "Advanced";
  baseUrl?: string;
  apiKeyEnv?: string;
  requiresKey?: boolean;
};

const providerOptions: ProviderOption[] = [
  { value: "lm-studio", label: "LM Studio", group: "Local", baseUrl: "http://localhost:1234/v1" },
  { value: "ollama", label: "Ollama (local)", group: "Local", baseUrl: "http://localhost:11434/v1" },
  { value: "openai", label: "OpenAI", group: "Cloud", apiKeyEnv: "OPENAI_API_KEY", requiresKey: true },
  {
    value: "anthropic",
    label: "Claude / Anthropic",
    group: "Cloud",
    apiKeyEnv: "ANTHROPIC_API_KEY",
    requiresKey: true
  },
  { value: "grok", label: "Grok / xAI", group: "Cloud", baseUrl: "https://api.x.ai/v1", apiKeyEnv: "XAI_API_KEY", requiresKey: true },
  { value: "gemini", label: "Gemini", group: "Cloud", apiKeyEnv: "GEMINI_API_KEY", requiresKey: true },
  {
    value: "ollama-cloud",
    label: "Ollama Cloud",
    group: "Cloud",
    baseUrl: "https://ollama.com/api",
    apiKeyEnv: "OLLAMA_API_KEY",
    requiresKey: true
  },
  {
    value: "openrouter",
    label: "OpenRouter",
    group: "Cloud",
    baseUrl: "https://openrouter.ai/api/v1",
    apiKeyEnv: "OPENROUTER_API_KEY",
    requiresKey: true
  },
  {
    value: "deepseek",
    label: "DeepSeek",
    group: "Cloud",
    baseUrl: "https://api.deepseek.com",
    apiKeyEnv: "DEEPSEEK_API_KEY",
    requiresKey: true
  },
  {
    value: "kimi",
    label: "Kimi",
    group: "Cloud",
    baseUrl: "https://api.moonshot.ai/v1",
    apiKeyEnv: "MOONSHOT_API_KEY",
    requiresKey: true
  },
  { value: "openai-compatible", label: "Custom OpenAI-compatible", group: "Advanced" },
  { value: "codex-cli", label: "Codex CLI", group: "Advanced" },
  { value: "mock", label: "Mock test mode", group: "Advanced" }
];
const providerOptionMap = Object.fromEntries(providerOptions.map((item) => [item.value, item]));
const providerGroups: Array<ProviderOption["group"]> = ["Local", "Cloud", "Advanced"];
const modelSuggestionsByProvider: Record<string, string[]> = {
  mock: ["mock"],
  "lm-studio": ["local-model"],
  ollama: ["llama3.1", "qwen2.5-coder", "mistral"],
  openai: ["gpt-5.5", "gpt-5.4", "gpt-5.4-mini"],
  "openai-compatible": ["local-model"],
  "ollama-cloud": ["gpt-oss:120b", "gpt-oss:20b"],
  openrouter: ["openai/gpt-5.5", "anthropic/claude-sonnet-4.5"],
  deepseek: ["deepseek-v4-pro", "deepseek-v4-flash"],
  kimi: ["kimi-k2.6", "kimi-k2.5"],
  anthropic: ["claude-sonnet-4.5", "claude-opus-4.1"],
  grok: ["grok-4.3", "grok-build-0.1", "grok-4.20"],
  gemini: ["gemini-2.5-pro", "gemini-2.5-flash"],
  "codex-cli": ["gpt-5.5", "gpt-5.4"]
};
const autonomyOptions = [
  { value: "background", label: "Safe Auto" },
  { value: "manual", label: "Manual" },
  { value: "autonomous", label: "Autopilot" }
];
type PreparedToolPreview = {
  name: string;
  args: Record<string, unknown>;
};
const exactCallPreviewMessage = "Invoking this request will create or require approval before execution; it has not run yet.";
const markdownComponents: Components = {
  a({ node: _node, ...props }) {
    return <a {...props} target="_blank" rel="noreferrer" />;
  }
};
const markdownPlugins = [remarkGfm];
const toolPermissionDefinitions = [
  {
    key: "allow_shell",
    label: "Command tools",
    description: "shell.run, test.run, lint.run, and shell-backed validation.",
    risk: "high risk"
  },
  {
    key: "allow_file_write",
    label: "File-write tools",
    description: "file.write, patch.apply, repairs, and skill materialization.",
    risk: "high risk"
  },
  {
    key: "allow_codex_cli",
    label: "Codex CLI",
    description: "codex.exec delegation through the local Codex CLI.",
    risk: "high risk"
  },
  {
    key: "allow_web",
    label: "Web context",
    description: "web.search and web.fetch read-only outside context.",
    risk: "medium risk"
  },
  {
    key: "allow_plugin_install",
    label: "Plugin install",
    description: "plugin.install from approved Kestrel manifests.",
    risk: "high risk"
  },
  {
    key: "allow_memory_import",
    label: "Memory import",
    description: "memory.import with provenance and validation metadata.",
    risk: "high risk"
  },
  {
    key: "allow_executable_skills",
    label: "Executable skills",
    description: "Skill-provided executable tool adapters.",
    risk: "high risk"
  },
  {
    key: "allow_git_commit",
    label: "Git commit",
    description: "git.commit under exact-call approval.",
    risk: "high risk"
  },
  {
    key: "allow_self_modification",
    label: "Self proposals",
    description: "self.propose_change through the repair gate.",
    risk: "critical risk"
  }
] as const;
type ToolPermissionKey = (typeof toolPermissionDefinitions)[number]["key"];
type ToolPermissionDraft = Record<ToolPermissionKey, boolean>;
const defaultToolPermissions = Object.fromEntries(
  toolPermissionDefinitions.map((permission) => [permission.key, false])
) as ToolPermissionDraft;
const emptyCapabilitySnapshot: CapabilitySnapshot = {
  items: [],
  counts: { total: 0, configured_enabled: 0, effective_enabled: 0, blocked: 0 }
};
const capabilityKindOrder: CapabilityKind[] = ["mcp_server", "tool", "skill"];
const HASH_ROUTING_ENABLED = typeof navigator === "undefined" || !navigator.userAgent.toLowerCase().includes("jsdom");
const runEventTypes = [
  "run.queued",
  "run.started",
  "run.turn_completed",
  "run.completed",
  "run.blocked",
  "run.failed",
  "run.cancelled",
  "orchestration.plan",
  "review.completed",
  "span.started",
  "span.finished",
  "approval.requested",
  "approval.approved",
  "approval.denied",
  "approval.wait",
  "tool.started",
  "tool.completed",
  "tool.failed",
  "tool.request",
  "tool.executed",
  "assistant.token",
  "assistant.tool_call",
  "assistant.provider_error",
  "assistant.usage",
  "context.compile",
  "memory.write",
  "capsule.completed",
  "capsule.failed",
  "memory.compact",
  "behavior_delta.preflight",
  "retry.blocked",
  "lesson.preflight",
  "lesson.created",
  "lesson.recall",
  "failure.episode",
  "diagnosis.classified",
  "scheduler.step",
  "scheduler.run",
  "task.started",
  "task.approved",
  "task.completed",
  "task.blocked",
  "task.failed",
  "subagent.queued",
  "subagent.started",
  "subagent.completed",
  "subagent.blocked",
  "worker.isolated",
  "subagent.failed"
];
const SETUP_DISMISSED_KEY = "kestrel.setup.dismissed";
const defaultPersonaPresets: PersonaPreset[] = [
  {
    id: "steady",
    name: "Steady Companion",
    summary: "Warm, grounded, concise, and quietly capable.",
    guidance: "Be warm and direct. Keep momentum, explain tradeoffs clearly, and avoid performative enthusiasm."
  },
  {
    id: "mentor",
    name: "Patient Mentor",
    summary: "Explains reasoning, teaches patterns, and checks understanding without dragging.",
    guidance: "Be patient and instructional. Explain the why behind decisions while keeping the next action clear."
  },
  {
    id: "spark",
    name: "Creative Spark",
    summary: "More playful, imaginative, and idea-forward while staying useful.",
    guidance: "Bring more creative options and a livelier voice, but keep answers practical and grounded in evidence."
  },
  {
    id: "operator",
    name: "Calm Operator",
    summary: "Precise, terse, and technical for focused execution.",
    guidance: "Be crisp and operational. Lead with facts, actions, blockers, and verification evidence."
  }
];

type SetupDraft = {
  agent_name: string;
  user_name: string;
  preferred_name: string;
  persona: string;
  working_style: string;
  goals_text: string;
  interests_text: string;
  communication_notes: string;
  continuous_learning: boolean;
};

const emptySetupDraft: SetupDraft = {
  agent_name: "Kestrel",
  user_name: "",
  preferred_name: "",
  persona: "steady",
  working_style: "",
  goals_text: "",
  interests_text: "",
  communication_notes: "",
  continuous_learning: true
};

export function App() {
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [authPromptOpen, setAuthPromptOpen] = useState(false);
  const [apiTokenDraft, setApiTokenDraft] = useState(() => getApiToken());
  const [runtime, setRuntime] = useState<Record<string, unknown> | null>(null);
  const [runtimeSettingsResult, setRuntimeSettingsResult] = useState<Record<string, unknown> | null>(null);
  const [selfState, setSelfState] = useState<SelfState | null>(null);
  const [onboardingState, setOnboardingState] = useState<SelfOnboardingState | null>(null);
  const [setupReadiness, setSetupReadiness] = useState<SetupReadinessReport | null>(null);
  const [setupOpen, setSetupOpen] = useState(false);
  const [setupDismissed, setSetupDismissed] = useState(() => localStorage.getItem(SETUP_DISMISSED_KEY) === "1");
  const [setupDraft, setSetupDraft] = useState<SetupDraft>(emptySetupDraft);
  const [runs, setRuns] = useState<Run[]>([]);
  const [sessions, setSessions] = useState<Session[]>([]);
  const [tools, setTools] = useState<Tool[]>([]);
  const [capabilitySnapshot, setCapabilitySnapshot] = useState<CapabilitySnapshot>(emptyCapabilitySnapshot);
  const [capabilityPending, setCapabilityPending] = useState<Set<string>>(() => new Set());
  const [capabilitySearch, setCapabilitySearch] = useState("");
  const [capabilityKindFilter, setCapabilityKindFilter] = useState<"all" | CapabilityKind>("all");
  const [capabilityStateFilter, setCapabilityStateFilter] = useState("all");
  const [approvals, setApprovals] = useState<Approval[]>([]);
  const [allApprovals, setAllApprovals] = useState<Approval[]>([]);
  const [mcpServers, setMcpServers] = useState<McpServer[]>([]);
  const [skills, setSkills] = useState<Skill[]>([]);
  const [plugins, setPlugins] = useState<Plugin[]>([]);
  const [channels, setChannels] = useState<Channel[]>([]);
  const [secrets, setSecrets] = useState<SecretRef[]>([]);
  const [memoryLayers, setMemoryLayers] = useState<MemoryLayerStatus[]>([]);
  const [behaviorDeltaReport, setBehaviorDeltaReport] = useState<BehaviorDeltaReport | null>(null);
  const [behaviorDeltaError, setBehaviorDeltaError] = useState<string | null>(null);
  const [learningDashboard, setLearningDashboard] = useState<LearningDashboard | null>(null);
  const [learningDashboardError, setLearningDashboardError] = useState<string | null>(null);
  const [lessons, setLessons] = useState<Array<Record<string, unknown>>>([]);
  const [failures, setFailures] = useState<Array<Record<string, unknown>>>([]);
  const [logs, setLogs] = useState<AgentLogEvent[]>([]);
  const [events, setEvents] = useState<TraceEvent[]>([]);
  const [runTrace, setRunTrace] = useState<RunTrace | null>(null);
  const [taskGraph, setTaskGraph] = useState<TaskGraph | null>(null);
  const [activeRunId, setActiveRunId] = useState<string | null>(null);
  const [activeSessionId, setActiveSessionId] = useState<string | null>(null);
  const [threadRuns, setThreadRuns] = useState<Run[]>([]);
  const [localThreads, setLocalThreads] = useState<ThreadSummary[]>([]);
  const activeRunIdRef = useRef<string | null>(null);
  const activeSessionIdRef = useRef<string | null>(null);
  const activeSectionRef = useRef<"chat" | "advanced" | "settings">("chat");
  const idleRefreshInFlightRef = useRef(false);
  const memoryBackendHydratedRef = useRef(false);
  const setupDraftHydratedRef = useRef(false);
  const [inspectorOpen, setInspectorOpen] = useState(false);
  const [activeSection, setActiveSection] = useState<"chat" | "advanced" | "settings">("chat");

  const [message, setMessage] = useState("");
  const [sessionId, setSessionId] = useState("");
  const [workspace, setWorkspace] = useState("");
  const [provider, setProvider] = useState("mock");
  const [model, setModel] = useState("mock");
  const [baseUrl, setBaseUrl] = useState("");
  const [apiKeyEnv, setApiKeyEnv] = useState("");
  const [providerKeyValue, setProviderKeyValue] = useState("");
  const [providerSecretResult, setProviderSecretResult] = useState<SecretRef | null>(null);
  const [temperature, setTemperature] = useState("0.2");
  const [maxToolRounds, setMaxToolRounds] = useState("6");
  const [modelCatalogs, setModelCatalogs] = useState<Record<string, ProviderModelCatalog>>({});
  const [modelCatalogLoading, setModelCatalogLoading] = useState(false);
  const [autonomyMode, setAutonomyMode] = useState("background");
  const [streamResponses, setStreamResponses] = useState(false);
  const [memoryBackendDraft, setMemoryBackendDraft] = useState<"In-memory" | "Memvid">("In-memory");
  const [apiAuthRequired, setApiAuthRequired] = useState(false);
  const [toolPermissions, setToolPermissions] = useState<ToolPermissionDraft>(defaultToolPermissions);

  const [subagentProfile, setSubagentProfile] = useState("worker");
  const [subagentGoal, setSubagentGoal] = useState("");
  const [schedulerTasks, setSchedulerTasks] = useState("3");
  const [schedulerCycles, setSchedulerCycles] = useState("5");
  const [schedulerResult, setSchedulerResult] = useState<Record<string, unknown> | null>(null);

  const [memoryQuery, setMemoryQuery] = useState("");
  const [memoryHits, setMemoryHits] = useState<MemoryHit[]>([]);
  const [memoryInspect, setMemoryInspect] = useState<Record<string, unknown> | null>(null);
  const [contextQuery, setContextQuery] = useState("");
  const [contextLayers, setContextLayers] = useState("policy,self,procedural,semantic,episodic,working");
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
  const [preparedToolPreview, setPreparedToolPreview] = useState<PreparedToolPreview | null>(null);
  const [toolResult, setToolResult] = useState<Record<string, unknown> | null>(null);
  const [toolFilter, setToolFilter] = useState("");
  const [toolSourceFilter, setToolSourceFilter] = useState("all");
  const [toolRiskFilter, setToolRiskFilter] = useState("all");
  const [toolEnabledFilter, setToolEnabledFilter] = useState("all");

  const [mcpId, setMcpId] = useState("");
  const [mcpName, setMcpName] = useState("");
  const [mcpTransport, setMcpTransport] = useState("stdio");
  const [mcpEndpoint, setMcpEndpoint] = useState("");
  const [mcpArgs, setMcpArgs] = useState("[]");
  const [mcpEnv, setMcpEnv] = useState("{}");
  const [mcpSecretEnv, setMcpSecretEnv] = useState("{}");
  const [mcpRiskPolicy, setMcpRiskPolicy] = useState("approval_by_default");
  const [mcpEditingServerId, setMcpEditingServerId] = useState<string | null>(null);
  const [mcpArgsTouched, setMcpArgsTouched] = useState(false);
  const [mcpEnvTouched, setMcpEnvTouched] = useState(false);
  const [mcpSecretEnvTouched, setMcpSecretEnvTouched] = useState(false);
  const [mcpToolSelection, setMcpToolSelection] = useState("");
  const [mcpToolArgs, setMcpToolArgs] = useState("{}");
  const [mcpResult, setMcpResult] = useState<Record<string, unknown> | null>(null);

  const [skillTask, setSkillTask] = useState("");
  const [skillSelection, setSkillSelection] = useState("");
  const [skillManifest, setSkillManifest] = useState('{\n  "id": "local-skill",\n  "name": "Local Skill",\n  "description": "Describe what this skill does.",\n  "risk": "medium"\n}');
  const [skillInstructions, setSkillInstructions] = useState("");
  const [skillResult, setSkillResult] = useState<Record<string, unknown> | null>(null);
  const [skillDiscovery, setSkillDiscovery] = useState<SkillDiscoveryReport | null>(null);
  const [skillDiscovering, setSkillDiscovering] = useState(false);
  const [pluginSource, setPluginSource] = useState("");
  const [pluginRef, setPluginRef] = useState("");
  const [pluginEnable, setPluginEnable] = useState(false);
  const [pluginResult, setPluginResult] = useState<Record<string, unknown> | null>(null);
  const [pluginReview, setPluginReview] = useState<PluginReviewReport | null>(null);
  const [pluginReviewSource, setPluginReviewSource] = useState("");
  const [pluginReviewRef, setPluginReviewRef] = useState<string | null>(null);

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
  const [telegramWebhookUrl, setTelegramWebhookUrl] = useState("");
  const [telegramActionResult, setTelegramActionResult] = useState<Record<string, unknown> | null>(null);
  const [secretName, setSecretName] = useState("TELEGRAM_BOT_TOKEN");
  const [secretPurpose, setSecretPurpose] = useState("Enable Telegram channel delivery.");
  const [secretValue, setSecretValue] = useState("");
  const [secretValidate, setSecretValidate] = useState(true);
  const [secretResult, setSecretResult] = useState<SecretRef | null>(null);

  const [diagnosisText, setDiagnosisText] = useState("");
  const [diagnosisResult, setDiagnosisResult] = useState<Record<string, unknown> | null>(null);
  const [selfTitle, setSelfTitle] = useState("");
  const [selfContent, setSelfContent] = useState("");
  const [selfSchema, setSelfSchema] = useState("user_workflow_preference");
  const [selfRememberResult, setSelfRememberResult] = useState<Record<string, unknown> | null>(null);
  const [webQuery, setWebQuery] = useState("");
  const [webResult, setWebResult] = useState<Record<string, unknown> | null>(null);

  const sortedThreadRuns = useMemo(
    () => [...threadRuns].sort((left, right) => left.created_at.localeCompare(right.created_at)),
    [threadRuns]
  );
  const activeRun = useMemo(() => {
    if (!activeRunId) return null;
    const threadRun = sortedThreadRuns.find((run) => run.run_id === activeRunId);
    if (threadRun) return threadRun;
    const globalRun = runs.find((run) => run.run_id === activeRunId);
    if (!globalRun) return null;
    if (activeSessionId && globalRun.session_id !== activeSessionId) return null;
    return globalRun;
  }, [runs, sortedThreadRuns, activeRunId, activeSessionId]);
  const threadSummaries = useMemo(() => {
    const remoteThreads = sessions.map((session) => ({
      session_id: session.session_id,
      title: deriveThreadTitle(session.latest_message || session.session_id),
      latest_message: session.latest_message,
      latest_status: session.latest_status,
      latest_run_id: session.latest_run_id,
      run_count: session.run_count,
      updated_at: session.updated_at
    }));
    const remoteIds = new Set(remoteThreads.map((thread) => thread.session_id));
    return [...localThreads.filter((thread) => !remoteIds.has(thread.session_id)), ...remoteThreads].sort((left, right) =>
      right.updated_at.localeCompare(left.updated_at)
    );
  }, [sessions, localThreads]);
  const activeRunIds = useMemo(() => new Set(sortedThreadRuns.map((run) => run.run_id)), [sortedThreadRuns]);
  const activeApprovals = useMemo(
    () => approvals.filter((approval) => activeRunIds.has(approval.run_id) || approval.run_id === activeRun?.run_id),
    [approvals, activeRunIds, activeRun?.run_id]
  );
  const activeRunEvents = useMemo(() => {
    const rows = new Map<string, TraceEvent>();
    const traceEvents = runTrace && runTrace.run.run_id === activeRun?.run_id ? runTrace.timeline : [];
    traceEvents
      .filter((event) => event.type !== "assistant.token")
      .forEach((event) => rows.set(eventKey(event), event));
    events
      .filter((event) => eventBelongsToRun(event, activeRun?.run_id) && event.type !== "assistant.token")
      .forEach((event) => rows.set(eventKey(event), event));
    return [...rows.values()].sort((left, right) => eventTimestamp(left).localeCompare(eventTimestamp(right)));
  }, [events, activeRun?.run_id, runTrace]);
  const providerCatalog = modelCatalogs[provider] ?? null;
  const modelSuggestions = providerCatalog?.models?.length ? providerCatalog.models : (modelSuggestionsByProvider[provider] ?? []);
  const modelCatalogLabel = modelCatalogLoading
    ? "loading"
    : providerCatalog?.ok
      ? providerCatalog.source === "provider"
        ? `${providerCatalog.models.length} provider models`
        : "static models"
      : providerCatalog?.error
        ? "fallback models"
        : "static models";
  const streamedAssistant = useMemo(
    () =>
      events
        .filter((event) => event.type === "assistant.token")
        .map((event) => String(event.payload.content ?? ""))
        .join(""),
    [events]
  );
  const proofOfWork = useMemo(() => extractProofOfWork(runTrace), [runTrace]);
  const capabilities = capabilitySnapshot.items;
  const mcpToolOptions = useMemo(
    () =>
      mcpServers.flatMap((server) => {
        const serverCapability = capabilityForMcpServer(capabilities, server.id);
        const serverEnabled = serverCapability?.effective_enabled ?? server.enabled;
        if (!serverEnabled) return [];
        return server.tools.flatMap((tool) => {
          const toolCapability = capabilityForMcpTool(capabilities, server.id, tool);
          const toolEnabled = toolCapability?.effective_enabled ?? tool.enabled ?? true;
          return toolEnabled
            ? [{ server, tool, value: `${server.id}::${tool.remote_name ?? tool.name}` }]
            : [];
        });
      }),
    [mcpServers, capabilities]
  );
  const enabledSkills = useMemo(
    () =>
      skills.filter((skill) => {
        const capability = capabilityForSkill(capabilities, skill.id);
        return capability?.effective_enabled ?? skill.enabled;
      }),
    [skills, capabilities]
  );
  const filteredCapabilities = useMemo(
    () => {
      const query = capabilitySearch.trim().toLowerCase();
      return [...capabilities]
        .filter((capability) => {
          if (capabilityKindFilter !== "all" && capability.kind !== capabilityKindFilter) return false;
          if (capabilityStateFilter === "active" && !capability.effective_enabled) return false;
          if (capabilityStateFilter === "off" && capability.configured_enabled) return false;
          if (capabilityStateFilter === "blocked" && capability.blocked_by.length === 0) return false;
          if (!query) return true;
          return [capability.name, capability.id, capability.description, capability.source, capability.parent_key ?? ""]
            .join(" ")
            .toLowerCase()
            .includes(query);
        })
        .sort((left, right) => left.name.localeCompare(right.name));
    },
    [capabilities, capabilitySearch, capabilityKindFilter, capabilityStateFilter]
  );
  const selectedTool = useMemo(() => tools.find((tool) => tool.name === toolName) ?? null, [tools, toolName]);
  const selectedToolEnabled = Boolean(
    selectedTool && isToolEffectivelyEnabled(selectedTool, toolPermissions, capabilities)
  );
  const selectedMcpToolEnabled = mcpToolOptions.some((option) => option.value === mcpToolSelection);
  const selectedSkillEnabled = enabledSkills.some((skill) => skill.id === skillSelection);
  const loadedMcpServer = mcpEditingServerId
    ? mcpServers.find((server) => server.id === mcpEditingServerId) ?? null
    : null;
  const activeThread = useMemo(
    () => threadSummaries.find((thread) => thread.session_id === activeSessionId) ?? null,
    [threadSummaries, activeSessionId]
  );
  const enabledToolCount = useMemo(
    () => tools.filter((tool) => isToolEffectivelyEnabled(tool, toolPermissions, capabilities)).length,
    [tools, toolPermissions, capabilities]
  );
  const filteredTools = useMemo(
    () =>
      tools.filter((tool) => {
        const enabled = isToolEffectivelyEnabled(tool, toolPermissions, capabilities);
        const query = toolFilter.trim().toLowerCase();
        const haystack = [
          tool.name,
          tool.description,
          tool.source,
          tool.risk,
          ...(tool.capabilities ?? [])
        ].join(" ").toLowerCase();
        if (query && !haystack.includes(query)) return false;
        if (toolSourceFilter !== "all" && tool.source !== toolSourceFilter) return false;
        if (toolRiskFilter !== "all" && tool.risk !== toolRiskFilter) return false;
        if (toolEnabledFilter === "enabled" && !enabled) return false;
        if (toolEnabledFilter === "disabled" && enabled) return false;
        return true;
      }),
    [tools, toolPermissions, capabilities, toolFilter, toolSourceFilter, toolRiskFilter, toolEnabledFilter]
  );
  const toolSources = useMemo(() => uniqueStrings(tools.map((tool) => tool.source)), [tools]);
  const toolRisks = useMemo(() => uniqueStrings(tools.map((tool) => tool.risk)), [tools]);
  const pluginSourceValue = pluginSource.trim();
  const pluginRefValue = pluginRef.trim() || null;
  const reviewedCurrentPlugin =
    Boolean(pluginReview) && pluginReviewSource === pluginSourceValue && pluginReviewRef === pluginRefValue;
  const pluginEnableBlockers = reviewedCurrentPlugin ? pluginReview?.enable_blockers ?? [] : [];

  function routeToSection(section: "chat" | "advanced" | "settings") {
    setActiveSection(section);
    if (!HASH_ROUTING_ENABLED) return;
    const hash = `#${section}`;
    if (window.location.hash !== hash) {
      window.history.replaceState(null, "", hash);
    }
  }

  function jumpToAdvanced(anchor: string) {
    routeToSection("advanced");
    window.setTimeout(() => {
      scrollToElement(anchor);
    }, 0);
  }

  function selectSessionId(sessionId: string | null) {
    activeSessionIdRef.current = sessionId;
    setActiveSessionId(sessionId);
  }

  function selectRunId(runId: string | null) {
    activeRunIdRef.current = runId;
    setActiveRunId(runId);
  }

  function chooseProvider(nextProvider: string) {
    const nextOption = providerOptionMap[nextProvider];
    setProvider(nextProvider);
    setBaseUrl(nextOption?.baseUrl ?? "");
    setApiKeyEnv(nextOption?.apiKeyEnv ?? "");
    setProviderKeyValue("");
    setProviderSecretResult(null);
    const suggestions = modelsForProvider(nextProvider, modelCatalogs);
    setModel((current) => {
      if (!current.trim() || !isKnownProviderModel(nextProvider, current, modelCatalogs)) {
        return suggestions[0] ?? current;
      }
      return current;
    });
  }

  async function refreshProviderModels(nextProvider = provider) {
    setModelCatalogLoading(true);
    try {
      const catalog = await getJson<ProviderModelCatalog>(`/api/runtime/models${queryString({ provider: nextProvider })}`);
      setModelCatalogs((catalogs) => ({ ...catalogs, [catalog.provider]: catalog }));
      setApiKeyEnv((current) => current.trim() || catalog.api_key_env || providerOptionMap[catalog.provider]?.apiKeyEnv || "");
      setModel((current) => {
        if (!catalog.models.length || !catalog.ok) return current;
        const staticModels = modelSuggestionsByProvider[catalog.provider] ?? [];
        if (!current.trim() || staticModels.includes(current)) {
          return catalog.models[0] ?? current;
        }
        return current;
      });
    } catch {
      const fallback = modelSuggestionsByProvider[nextProvider] ?? [];
      setModelCatalogs((catalogs) => ({
        ...catalogs,
        [nextProvider]: {
          provider: nextProvider,
          models: fallback,
          fallback_models: fallback,
          source: "fallback",
          ok: false,
          fetchable: true,
          error: "model catalog unavailable",
          base_url_configured: false,
          api_key_configured: false
        }
      }));
    } finally {
      setModelCatalogLoading(false);
    }
  }

  useEffect(() => {
    activeSessionIdRef.current = activeSessionId;
  }, [activeSessionId]);

  useEffect(() => {
    activeRunIdRef.current = activeRunId;
  }, [activeRunId]);

  useEffect(() => {
    activeSectionRef.current = activeSection;
  }, [activeSection]);

  useEffect(() => {
    if (!onboardingState) return;
    if (onboardingState.profile && !setupDraftHydratedRef.current) {
      setSetupDraft(setupDraftFromProfile(onboardingState.profile));
      setupDraftHydratedRef.current = true;
    }
    if (!onboardingState.completed && !setupDismissed) {
      setSetupOpen(true);
    }
  }, [onboardingState, setupDismissed]);

  useEffect(() => {
    refreshAll().catch(reportError);
    const timer = window.setInterval(() => refreshIdleSummary().catch(reportError), 3500);
    return () => window.clearInterval(timer);
  }, []);

  useEffect(() => {
    void refreshProviderModels(provider);
  }, [provider]);

  useEffect(() => {
    if (!HASH_ROUTING_ENABLED) return;
    const syncRoute = () => {
      const next = sectionFromHash(window.location.hash);
      if (next) setActiveSection(next);
    };
    syncRoute();
    window.addEventListener("hashchange", syncRoute);
    return () => window.removeEventListener("hashchange", syncRoute);
  }, []);

  useEffect(() => {
    if (!activeSessionId) {
      setThreadRuns([]);
      return;
    }
    refreshThreadRuns(activeSessionId).catch(reportError);
  }, [activeSessionId]);

  useEffect(() => {
    if (!activeRun?.run_id) return;
    setEvents([]);
    refreshRunDetails(activeRun.run_id).catch(reportError);
    const appendEvent = (parsed: TraceEvent) => {
      setEvents((rows) => [...rows.slice(-120), parsed]);
      if (parsed.type !== "assistant.token") {
        refreshChatSummary().catch(reportError);
        refreshRunDetails(activeRun.run_id).catch(reportError);
        refreshThreadRuns(activeRun.session_id).catch(reportError);
      }
    };
    return subscribeJsonEvents<TraceEvent>(`/api/runs/${activeRun.run_id}/events`, runEventTypes, appendEvent, reportError);
  }, [activeRun?.run_id]);

  function applyRunSessionSelection(runList: Run[], sessionList: Session[], pendingApprovalList: Approval[]) {
    const selectedSessionId = activeSessionIdRef.current;
    const selectedRunId = activeRunIdRef.current;
    if (!selectedSessionId && sessionList.length > 0) {
      const pendingRunIds = new Set(pendingApprovalList.map((approval) => approval.run_id));
      const attentionRun = runList.find((run) => pendingRunIds.has(run.run_id));
      const initialSession = attentionRun
        ? sessionList.find((session) => session.session_id === attentionRun.session_id) ?? sessionList[0]
        : sessionList[0];
      selectSessionId(initialSession.session_id);
      selectRunId(attentionRun?.run_id ?? initialSession.latest_run_id);
    } else if (selectedSessionId && !selectedRunId) {
      const selectedSession = sessionList.find((session) => session.session_id === selectedSessionId);
      if (selectedSession?.latest_run_id) selectRunId(selectedSession.latest_run_id);
    }
  }

  async function refreshChatSummary() {
    const [runList, sessionList, pendingApprovalList] = await Promise.all([
      getJson<Run[]>("/api/runs"),
      getJson<Session[]>("/api/sessions"),
      getJson<Approval[]>("/api/approvals?status=pending")
    ]);
    setRuns(runList);
    setSessions(sessionList);
    setApprovals(pendingApprovalList);
    applyRunSessionSelection(runList, sessionList, pendingApprovalList);
  }

  async function refreshSummary() {
    const [runList, sessionList, toolList, capabilityReport, pendingApprovalList, approvalList, mcpList, skillList, pluginList, channelList, secretList, layerList] =
      await Promise.all([
        getJson<Run[]>("/api/runs"),
        getJson<Session[]>("/api/sessions"),
        getJson<Tool[]>("/api/tools"),
        getJson<CapabilitySnapshot>("/api/capabilities"),
        getJson<Approval[]>("/api/approvals?status=pending"),
        getJson<Approval[]>("/api/approvals"),
        getJson<McpServer[]>("/api/mcp/servers"),
        getJson<Skill[]>("/api/skills"),
        getJson<Plugin[]>("/api/plugins"),
        getJson<Channel[]>("/api/channels"),
        getJson<SecretRef[]>("/api/secrets"),
        getJson<MemoryLayerStatus[]>("/api/memory/layers")
      ]);
    setRuns(runList);
    setSessions(sessionList);
    setTools(toolList);
    setCapabilitySnapshot(capabilityReport);
    setApprovals(pendingApprovalList);
    setAllApprovals(approvalList);
    setMcpServers(mcpList);
    setSkills(skillList);
    setPlugins(pluginList);
    setChannels(channelList);
    setSecrets(secretList);
    setMemoryLayers(layerList);
    if (!memoryBackendHydratedRef.current) {
      setMemoryBackendDraft(layerList.some((layer) => layer.backend.toLowerCase().includes("memvid")) ? "Memvid" : "In-memory");
      memoryBackendHydratedRef.current = true;
    }
    applyRunSessionSelection(runList, sessionList, pendingApprovalList);
  }

  async function refreshIdleSummary() {
    if (idleRefreshInFlightRef.current) return;
    idleRefreshInFlightRef.current = true;
    try {
      if (activeSectionRef.current === "advanced") {
        await refreshSummary();
      } else {
        await refreshChatSummary();
      }
    } finally {
      idleRefreshInFlightRef.current = false;
    }
  }

  async function refreshAll() {
    await refreshSummary();
    const [
      runtimeConfig,
      selfSnapshot,
      onboardingSnapshot,
      setupReadinessReport,
      logList,
      lessonList,
      failureList,
      deltaReport,
      learningReport
    ] = await Promise.all([
      getJson<RuntimeConfig>("/api/runtime/config"),
      getJson<SelfState>("/api/self"),
      getJson<SelfOnboardingState>("/api/self/onboarding"),
      getJson<SetupReadinessReport>("/api/product/setup").catch((error) => {
        reportError(error);
        return null;
      }),
      getJson<AgentLogEvent[]>("/api/logs?limit=120"),
      getJson<{ items: Array<Record<string, unknown>> }>("/api/cognition/lessons?k=20"),
      getJson<{ items: Array<Record<string, unknown>> }>("/api/cognition/failures?k=20"),
      getJson<BehaviorDeltaReport>("/api/memory/deltas?since=all").catch((error) => {
        setBehaviorDeltaError(error instanceof Error ? error.message : String(error));
        return null;
      }),
      getLearningDashboard<LearningDashboard>("all").catch((error) => {
        setLearningDashboardError(error instanceof Error ? error.message : String(error));
        return null;
      })
    ]);
    setRuntime(runtimeConfig);
    setSelfState(selfSnapshot);
    setOnboardingState(onboardingSnapshot);
    setSetupReadiness(setupReadinessReport);
    const savedSettings = runtimeSettingsFrom(runtimeConfig);
    const nextProvider = String(savedSettings.provider ?? runtimeConfig.provider?.name ?? "mock");
    const nextProviderOption = providerOptionMap[nextProvider];
    setProvider(nextProvider);
    setModel(String(savedSettings.model ?? runtimeConfig.provider?.model ?? "mock"));
    setBaseUrl(String(savedSettings.base_url ?? nextProviderOption?.baseUrl ?? ""));
    setApiKeyEnv(String(savedSettings.api_key_env ?? runtimeConfig.provider?.api_key_env ?? nextProviderOption?.apiKeyEnv ?? ""));
    setProviderSecretResult(null);
    setProviderKeyValue("");
    setTemperature(formatTemperature(savedSettings.temperature ?? runtimeConfig.provider?.temperature ?? 0.2));
    setMaxToolRounds(formatToolRounds(savedSettings.max_tool_rounds ?? runtimeConfig.limits?.max_tool_rounds ?? 6));
    setWorkspace(String(savedSettings.workspace ?? runtimeConfig.paths?.workspace ?? ""));
    setAutonomyMode(validAutonomyMode(savedSettings.autonomy_mode, "background"));
    setMemoryBackendDraft(String(savedSettings.backend ?? "").toLowerCase() === "memvid" ? "Memvid" : "In-memory");
    setStreamResponses(Boolean(savedSettings.stream ?? runtimeConfig.provider?.stream));
    setApiAuthRequired(Boolean(savedSettings.require_api_auth ?? runtimeConfig.feature_flags?.require_api_auth));
    setToolPermissions(toolPermissionsFromRuntime(runtimeConfig));
    setLogs(logList);
    setLessons(lessonList.items);
    setFailures(failureList.items);
    if (deltaReport) {
      setBehaviorDeltaReport(deltaReport);
      setBehaviorDeltaError(null);
    }
    if (learningReport) {
      setLearningDashboard(learningReport);
      setLearningDashboardError(null);
    }
  }

  async function refreshThreadRuns(sessionId: string) {
    const runList = await getJson<Run[]>(`/api/sessions/${encodeURIComponent(sessionId)}/runs`);
    if (activeSessionIdRef.current === sessionId) {
      setThreadRuns(runList);
      if (!activeRunIdRef.current && runList.length > 0) {
        selectRunId(runList[runList.length - 1].run_id);
      }
    }
    setLocalThreads((threads) =>
      threads.map((thread) =>
        thread.session_id === sessionId && runList.length > 0
          ? {
              ...thread,
              latest_message: runList[runList.length - 1].message,
              latest_run_id: runList[runList.length - 1].run_id,
              latest_status: runList[runList.length - 1].status,
              run_count: runList.length,
              title: deriveThreadTitle(runList[0].message || runList[runList.length - 1].message),
              updated_at: runList[runList.length - 1].updated_at
            }
          : thread
      )
    );
  }

  async function refreshRunDetails(runId: string) {
    const [graph, trace] = await Promise.all([
      getJson<TaskGraph>(`/api/runs/${runId}/task-graph`),
      getJson<RunTrace>(`/api/runs/${runId}/trace?limit=700`)
    ]);
    if (activeRunIdRef.current !== runId) return;
    setTaskGraph(graph);
    setRunTrace(trace);
  }

  function reportError(value: unknown) {
    if (value instanceof ApiAuthError) {
      setAuthPromptOpen(true);
      setApiTokenDraft(getApiToken());
      setError(null);
      return;
    }
    setError(value instanceof Error ? value.message : String(value));
  }

  async function saveToken(event: FormEvent) {
    event.preventDefault();
    setApiToken(apiTokenDraft);
    setAuthPromptOpen(false);
    setError(null);
    await refreshAll().catch(reportError);
  }

  async function saveRuntimeSettings() {
    const currentRuntime = runtime as RuntimeConfig | null;
    const savedSettings = runtimeSettingsFrom(currentRuntime);
    await guarded(async () => {
      const result = await putJson<Record<string, unknown>>("/api/runtime/settings", {
        provider,
        model: model.trim() || "mock",
        base_url: baseUrl.trim() || null,
        api_key_env: apiKeyEnv.trim() || null,
        temperature: coerceTemperature(temperature),
        max_tool_rounds: coerceToolRounds(maxToolRounds),
        backend: memoryBackendDraft === "Memvid" ? "memvid" : "memory",
        memory_dir: String(savedSettings.memory_dir ?? currentRuntime?.paths?.memory_dir ?? ".nest/memory"),
        workspace: workspace.trim() || String(currentRuntime?.paths?.workspace ?? "."),
        stream: streamResponses,
        autonomy_mode: autonomyMode,
        ...toolPermissions
      });
      setRuntimeSettingsResult(result);
      await refreshAll();
    }, "Settings saved and applied to new runs.");
  }

  async function storeProviderKey() {
    const targetEnv = apiKeyEnv.trim();
    if (!targetEnv || !providerKeyValue.trim()) return;
    await guarded(async () => {
      const result = await postJson<SecretRef>("/api/secrets", {
        name: targetEnv,
        purpose: `Enable ${providerDisplayName} as an LLM provider.`,
        value: providerKeyValue,
        validate: true
      });
      setProviderSecretResult(result);
      setProviderKeyValue("");
      await refreshProviderModels(provider);
      await refreshSummary();
    }, "Provider key stored.");
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
      const targetSessionId = sessionId.trim() || activeSessionIdRef.current || createThreadId();
      const payload: Record<string, unknown> = {
        message,
        session_id: targetSessionId,
        autonomy_mode: submissionAutonomyMode(autonomyMode)
      };
      if (workspace.trim()) payload.workspace = workspace.trim();
      const runtimeProvider = String((runtime as RuntimeConfig | null)?.provider?.name ?? "");
      const runtimeModel = String((runtime as RuntimeConfig | null)?.provider?.model ?? "");
      if (provider.trim() && provider.trim() !== runtimeProvider) payload.provider = provider.trim();
      if (model.trim() && model.trim() !== runtimeModel) payload.model = model.trim();
      const run = await postJson<Run>("/api/runs", payload);
      setMessage("");
      selectSessionId(run.session_id);
      selectRunId(run.run_id);
      setThreadRuns((rows) => [...rows.filter((row) => row.run_id !== run.run_id), run]);
      setLocalThreads((threads) => [
        {
          session_id: run.session_id,
          title: deriveThreadTitle(run.message),
          latest_message: run.message,
          latest_status: run.status,
          latest_run_id: run.run_id,
          run_count: Math.max(1, (threads.find((thread) => thread.session_id === run.session_id)?.run_count ?? 0) + 1),
          updated_at: run.updated_at,
          is_local: true
        },
        ...threads.filter((thread) => thread.session_id !== run.session_id)
      ]);
      await refreshSummary();
      await refreshThreadRuns(run.session_id);
      await refreshRunDetails(run.run_id);
    }, "Run queued.");
  }

  function createNewThread() {
    const threadId = createThreadId();
    const now = new Date().toISOString();
    selectSessionId(threadId);
    selectRunId(null);
    setThreadRuns([]);
    setEvents([]);
    setRunTrace(null);
    setTaskGraph(null);
    setLocalThreads((threads) => [
      {
        session_id: threadId,
        title: "New chat",
        latest_message: "New chat",

        latest_run_id: "",
        latest_status: "ready",
        run_count: 0,
        updated_at: now,
        is_local: true
      },
      ...threads
    ]);
  }

  async function selectThread(thread: ThreadSummary) {
    selectSessionId(thread.session_id);
    selectRunId(thread.latest_run_id || null);
    setEvents([]);
    setRunTrace(null);
    setTaskGraph(null);
    await guarded(async () => {
      await refreshThreadRuns(thread.session_id);
      if (thread.latest_run_id) await refreshRunDetails(thread.latest_run_id);
    });
  }

  async function selectRun(runId: string) {
    const run = sortedThreadRuns.find((row) => row.run_id === runId) ?? runs.find((row) => row.run_id === runId);
    if (run) selectSessionId(run.session_id);
    selectRunId(runId);
    setEvents([]);
    await guarded(async () => {
      if (run) await refreshThreadRuns(run.session_id);
      await refreshRunDetails(runId);
    });
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
      if (!selectedTool || !selectedToolEnabled) {
        throw new Error("This tool is disabled. Enable it in Settings before invoking it.");
      }
      const args = readJson<Record<string, unknown>>(toolArgs, {});
      setPreparedToolPreview(null);
      const result = await postJson<Record<string, unknown>>(`/api/tools/${encodeURIComponent(toolName)}/invoke`, {
        arguments: args,
        session_id: activeRun?.session_id ?? "manual",
        run_id: activeRun?.run_id ?? null
      });
      setToolResult(result);
      await refreshSummary();
    });
  }

  async function setCapabilityEnabled(capability: Capability, enabled: boolean) {
    if (
      enabled &&
      ["high", "critical"].includes(capability.risk.toLowerCase()) &&
      !window.confirm(
        `Enable ${capability.name}? This ${capability.risk}-risk capability${
          capability.requires_approval ? " will still require approval when invoked" : " can be invoked without per-call approval"
        }.`
      )
    ) {
      return;
    }

    setError(null);
    setCapabilityPending((pending) => new Set(pending).add(capability.key));
    try {
      const result = await putJson<CapabilityMutationResult>(
        `/api/capabilities/${capability.kind}/${encodeURIComponent(capability.id)}`,
        { enabled, expected_revision: capability.revision }
      );
      setCapabilitySnapshot((snapshot) => replaceCapability(snapshot, result.capability));
      await refreshSummary();
      const revoked = result.revoked_approvals
        ? ` ${result.revoked_approvals} pending approval${result.revoked_approvals === 1 ? " was" : "s were"} revoked.`
        : "";
      const capabilityState = enabled && !result.capability.effective_enabled
        ? `configured on but blocked by ${result.capability.blocked_by.map(formatCapabilityBlocker).join(", ")}`
        : enabled
          ? "enabled"
          : "disabled";
      setNotice(
        `${result.capability.name} ${capabilityState} for future invocations.${revoked}`
      );
    } catch (value) {
      reportError(value);
      await refreshSummary().catch(() => undefined);
    } finally {
      setCapabilityPending((pending) => {
        const next = new Set(pending);
        next.delete(capability.key);
        return next;
      });
    }
  }

  function loadMcp(server: McpServer) {
    setMcpId(server.id);
    setMcpName(server.name);
    setMcpTransport(server.transport);
    setMcpEndpoint(server.transport === "stdio" ? server.command ?? "" : server.url ?? "");
    setMcpArgs("[]");
    setMcpEnv("{}");
    setMcpSecretEnv("{}");
    setMcpRiskPolicy(server.risk_policy ?? "approval_by_default");
    setMcpEditingServerId(server.id);
    setMcpArgsTouched(false);
    setMcpEnvTouched(false);
    setMcpSecretEnvTouched(false);
  }

  async function saveMcp(event: FormEvent) {
    event.preventDefault();
    await guarded(async () => {
      const payload: Record<string, unknown> = {
        id: mcpId,
        name: mcpName || mcpId,
        transport: mcpTransport,
        command: mcpTransport === "stdio" ? mcpEndpoint || null : null,
        url: mcpTransport === "stdio" ? null : mcpEndpoint || null,
        risk_policy: mcpRiskPolicy
      };
      if (mcpArgsTouched) payload.args = readJson<string[]>(mcpArgs, []);
      if (mcpEnvTouched) payload.env = readJson<Record<string, string>>(mcpEnv, {});
      if (mcpSecretEnvTouched) payload.secret_env = readJson<Record<string, string>>(mcpSecretEnv, {});
      const path = mcpServers.some((server) => server.id === mcpId) ? `/api/mcp/servers/${encodeURIComponent(mcpId)}` : "/api/mcp/servers";
      const saved = path === "/api/mcp/servers" ? await postJson<McpServer>(path, payload) : await putJson<McpServer>(path, payload);
      setMcpId(saved.id);
      setMcpEditingServerId(saved.id);
      setMcpArgsTouched(false);
      setMcpEnvTouched(false);
      setMcpSecretEnvTouched(false);
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
      if (!selectedMcpToolEnabled) {
        throw new Error("This MCP tool is disabled. Enable its server and tool before invoking it.");
      }
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
      setSkillDiscovering(true);
      try {
        const result = await postJson<SkillDiscoveryReport>("/api/skills/discover");
        setSkillDiscovery(result);
        setSkillResult(result as unknown as Record<string, unknown>);
        setSkills(result.skills);
        await refreshSummary();
        setNotice(result.message);
      } finally {
        setSkillDiscovering(false);
      }
    });
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
      if (!selectedSkillEnabled) {
        throw new Error("This skill is disabled. Enable it in Settings before running it.");
      }
      const result = await postJson<Record<string, unknown>>(`/api/skills/${encodeURIComponent(skillSelection)}/run`, {
        arguments: { task: skillTask, context: { active_run_id: activeRun?.run_id ?? null } },
        session_id: activeRun?.session_id ?? "manual",
        run_id: activeRun?.run_id ?? null
      });
      setSkillResult(result);
      await refreshSummary();
    });
  }

  async function reviewPlugin(event: FormEvent) {
    event.preventDefault();
    await guarded(async () => {
      const source = pluginSource.trim();
      const ref = pluginRef.trim() || null;
      const result = await postJson<PluginReviewReport>("/api/plugins/review", {
        source,
        ref
      });
      setPluginReview(result);
      setPluginReviewSource(source);
      setPluginReviewRef(ref);
      setPluginResult(result as unknown as Record<string, unknown>);
      if (result.enable_blockers.length > 0) {
        setPluginEnable(false);
      }
      setNotice(result.enable_blockers.length ? "Plugin review found enable blockers." : "Plugin review complete.");
    });
  }

  async function installPlugin() {
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

  async function telegramWebhookInfo(channel: Channel) {
    await guarded(async () => {
      const result = await getJson<Record<string, unknown>>(`/api/channels/${encodeURIComponent(channel.id)}/telegram/webhook-info`);
      setTelegramActionResult(result);
    }, "Telegram webhook info loaded.");
  }

  async function telegramSetWebhook(channel: Channel) {
    await guarded(async () => {
      const result = await postJson<Record<string, unknown>>(`/api/channels/${encodeURIComponent(channel.id)}/telegram/set-webhook`, {
        url: telegramWebhookUrl,
        drop_pending_updates: false
      });
      setTelegramActionResult(result);
    }, "Telegram webhook updated.");
  }

  async function telegramDeleteWebhook(channel: Channel) {
    await guarded(async () => {
      const result = await postJson<Record<string, unknown>>(`/api/channels/${encodeURIComponent(channel.id)}/telegram/delete-webhook`, {
        drop_pending_updates: false
      });
      setTelegramActionResult(result);
    }, "Telegram webhook removed.");
  }

  function telegramOwnerLabels(channel: Channel): string[] {
    const raw = channel.settings?.owner_user_ids ?? channel.settings?.admin_user_ids ?? channel.settings?.telegram_owner_ids;
    const values = Array.isArray(raw) ? raw : typeof raw === "string" ? raw.split(",") : [];
    return values.map((item) => String(item).trim()).filter(Boolean).map((item) => `owner ${item}`);
  }

  function channelEnvFlag(channel: Channel, key: string): boolean {
    const status = channel.env_status;
    if (!status || typeof status !== "object") return false;
    return Boolean((status as Record<string, unknown>)[key]);
  }

  async function saveSecret(event: FormEvent) {
    event.preventDefault();
    await guarded(async () => {
      const saved = await postJson<SecretRef>("/api/secrets", {
        name: secretName,
        purpose: secretPurpose,
        value: secretValue,
        validate: secretValidate
      });
      setSecretResult(saved);
      setSecretValue("");
      await refreshSummary();
    }, "Secret stored.");
  }

  async function validateSecret(secret: SecretRef) {
    await guarded(async () => {
      const result = await postJson<SecretRef>(`/api/secrets/${encodeURIComponent(secret.id)}/validate`);
      setSecretResult(result);
      await refreshSummary();
    }, "Secret validated.");
  }

  async function deleteSecret(secret: SecretRef) {
    await guarded(async () => {
      await deleteJson(`/api/secrets/${encodeURIComponent(secret.id)}`);
      setSecretResult(null);
      await refreshSummary();
    }, "Secret removed.");
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

  async function rememberSelf(event: FormEvent) {
    event.preventDefault();
    await guarded(async () => {
      const result = await postJson<Record<string, unknown>>("/api/self/remember", {
        title: selfTitle,
        content: selfContent,
        schema: selfSchema,
        validation_status: "user_confirmed",
        confidence: 0.88
      });
      setSelfRememberResult(result);
      await refreshAll();
    }, "Soul memory reviewed.");
  }

  async function saveSetup(event: FormEvent) {
    event.preventDefault();
    await guarded(async () => {
      const result = await postJson<SelfOnboardingSaveResult>("/api/self/onboarding", {
        agent_name: setupDraft.agent_name,
        user_name: setupDraft.user_name,
        preferred_name: setupDraft.preferred_name,
        persona: setupDraft.persona,
        working_style: setupDraft.working_style,
        goals: splitSetupList(setupDraft.goals_text),
        interests: splitSetupList(setupDraft.interests_text),
        communication_notes: setupDraft.communication_notes,
        continuous_learning: setupDraft.continuous_learning
      });
      if (!result.success) {
        throw new Error(String(result.memory?.error ?? "Setup could not be saved to Soul memory."));
      }
      setOnboardingState({
        completed: result.success,
        profile: result.profile,
        personas: result.personas
      });
      setSelfRememberResult(result.memory);
      localStorage.setItem(SETUP_DISMISSED_KEY, "1");
      setSetupDismissed(true);
      setSetupOpen(false);
      setupDraftHydratedRef.current = true;
      await refreshAll();
    }, "Setup saved to Soul memory.");
  }

  function dismissSetup() {
    localStorage.setItem(SETUP_DISMISSED_KEY, "1");
    setSetupDismissed(true);
    setSetupOpen(false);
  }

  async function searchWeb(event: FormEvent) {
    event.preventDefault();
    await guarded(async () => {
      const result = await postJson<Record<string, unknown>>("/api/web/search", {
        query: webQuery,
        max_results: 5
      });
      setWebResult(result);
    });
  }

  const runtimeConfig = runtime as RuntimeConfig | null;
  const runtimeProvider = runtimeConfig?.provider ?? {};
  const runtimeLimits = runtimeConfig?.limits ?? {};
  const runtimePaths = runtimeConfig?.paths ?? {};
  const featureFlags = runtimeConfig?.feature_flags ?? {};
  const selectedProviderOption = providerOptionMap[provider] ?? null;
  const providerDisplayName = selectedProviderOption?.label ?? provider;
  const selectedProviderCatalog = providerCatalog?.provider === provider ? providerCatalog : null;
  const providerRequiresKey = Boolean(selectedProviderOption?.requiresKey || apiKeyEnv.trim());
  const providerKeyConfigured =
    selectedProviderCatalog?.api_key_configured ??
    (String(runtimeProvider.name ?? "") === provider ? Boolean(runtimeProvider.api_key_configured) : false);
  const providerKeyStatus = providerRequiresKey ? (providerKeyConfigured ? "configured" : "missing") : "not needed";
  const activeDeltaCount = behaviorDeltaReport?.summary.active_deltas ?? 0;
  const totalDeltaCount = behaviorDeltaReport?.summary.total_deltas ?? 0;
  const pendingApprovalCount = approvals.filter((approval) => approval.status === "pending").length;
  const oracleShadowLabel = `${events.filter((event) => event.type.includes("oracle") || event.type.includes("routing")).length} observations`;
  const onboardingProfile = onboardingState?.profile ?? null;
  const personaPresets = onboardingState?.personas?.length ? onboardingState.personas : defaultPersonaPresets;
  const agentDisplayName = String(onboardingProfile?.agent_name || selfState?.identity?.name || "Kestrel");
  const userDisplayName = String(onboardingProfile?.preferred_name || onboardingProfile?.user_name || "");
  const simpleStatus = simpleChatStatus(activeRun, pendingApprovalCount, setupReadiness);
  const chatIntro = userDisplayName
    ? `Ready when you are, ${userDisplayName}.`
    : "Ready when you are.";

  return (
    <>
      <header className="topbar">
        <div className="topbar-inner">
          <a className="brand" href="#workspace">
            <span className="brand-mark" aria-hidden="true">
              <Feather size={22} />
            </span>
            <span>
              <span className="brand-name">{agentDisplayName}</span>
              <span className="brand-tag">{onboardingProfile?.persona_name ?? "Local-first agent"}</span>
            </span>
          </a>
          <nav className="primary-nav" aria-label="Primary">
            <button type="button" className={activeSection === "chat" ? "active" : ""} onClick={() => routeToSection("chat")}>Chat</button>
            <button type="button" className={activeSection === "settings" ? "active" : ""} onClick={() => routeToSection("settings")}>Settings</button>
            <button type="button" className={activeSection === "advanced" ? "active" : ""} onClick={() => routeToSection("advanced")}>Advanced</button>
          </nav>
          <div className="topbar-meta">
            <button type="button" className="setup-button" onClick={() => setSetupOpen(true)}>
              <Sparkles size={14} /> Setup
            </button>
            <span className="status-pill"><span className="status-dot"></span>{simpleStatus.label}</span>
          </div>
        </div>
      </header>
      {authPromptOpen ? (
        <main className="conversation" id="workspace">
          <section className="settings-grid" aria-label="API authentication">
            <Panel title={`${agentDisplayName} API token`} icon={<KeyRound size={19} />}>
              <form className="stacked-form" onSubmit={saveToken}>
                <Field label="API token">
                  <input
                    type="password"
                    value={apiTokenDraft}
                    onChange={(event) => setApiTokenDraft(event.target.value)}
                    autoComplete="off"
                    autoFocus
                  />
                </Field>
                <button type="submit">
                  <ShieldCheck size={15} /> Save token
                </button>
              </form>
            </Panel>
          </section>
        </main>
      ) : (
      <div className={`chat-shell ${inspectorOpen ? "" : "no-inspector"}`} data-active-section={activeSection}>
      <a className="skip-link" href="#workspace">Skip to workspace</a>
      <aside className="rail" aria-label="Threads">
        <div className="rail-head">
          <h2>Chats <small>{threadSummaries.length}</small></h2>
          <button type="button" className="new-chat" onClick={createNewThread} title="New chat">
            <MessageCircle size={16} />
          </button>
        </div>
        <div className="rail-search">
          <Search size={14} />
          <input type="text" placeholder="Search threads..." />
        </div>
        <div className="thread-list" aria-label="Conversation threads">
          {threadSummaries.map((thread) => (
            <button
              type="button"
              className={`thread-button ${thread.session_id === activeSessionId ? "active" : ""}`}
              key={thread.session_id}
              onClick={() => selectThread(thread)}
            >
              <span>
                <strong>{thread.title}</strong>
                <small>{thread.latest_message !== thread.title ? thread.latest_message : messageCountLabel(thread.run_count)}</small>
              </span>
              <StatusBadge value={thread.run_count ? simpleThreadStatus(thread.latest_status) : "Ready"} />
            </button>
          ))}
          {threadSummaries.length === 0 && <EmptyState>No threads yet.</EmptyState>}
        </div>
      </aside>

      <main className="conversation" id="workspace">
        {activeSection === "chat" && (
          <>
        <header className="conv-head simple-conv-head" data-section="chat">
          <div>
            <h1>Ask {agentDisplayName}</h1>
            <div className="conv-meta simple-meta">
              <span>{activeThread ? "Current chat" : "New chat"}</span>
              <span className="sep">·</span>
              <span>{activeRun ? simpleStatus.detail : chatIntro}</span>
            </div>
          </div>
          <div className="conv-tools simple-conv-tools">
            <StatusBadge value={simpleStatus.label} />
            {setupReadiness && !setupReadiness.ready && (
              <button type="button" onClick={() => setSetupOpen(true)}>
                <Sparkles size={15} /> Setup
              </button>
            )}
            {activeRun && (
              <button type="button" onClick={() => setInspectorOpen((open) => !open)}>
                <PanelRightOpen size={15} /> Details
              </button>
            )}
            <button type="button" onClick={() => refreshAll().catch(reportError)}>
              <RefreshCw size={15} /> Refresh
            </button>
          </div>
        </header>

        <div className="announcer" aria-live="polite">
          {notice}
        </div>
        {error && <ActionError message={error} onDismiss={() => setError(null)} />}

        <section className={`conversation-layout ${inspectorOpen ? "with-inspector" : ""}`} data-section="chat">
          <div className="transcript-inner">
            <div className="transcript" aria-label="Conversation transcript">
              {sortedThreadRuns.length === 0 ? (
                <div className="empty-state">
                  <MessageCircle size={28} />
                  <h2>Tell {agentDisplayName} what to do.</h2>
                  <p>Start with a build, fix, research, inspection, or continuation request. {agentDisplayName} will keep the work in this thread.</p>
                </div>
              ) : (
                sortedThreadRuns.map((run) => (
                  <div className="turn" key={run.run_id}>
                    <article className="msg user">
                      <strong>You</strong>
                      <p>{run.message}</p>
                    </article>
                    <article className="msg kestrel">

                      <strong>Kestrel</strong>
                      <MarkdownMessage text={assistantTextForRun(run, activeRun?.run_id, streamedAssistant)} />
                      {run.run_id === activeRun?.run_id && <LiveRunActivity run={run} events={activeRunEvents} />}
                    </article>
                  </div>
                ))
              )}
              {activeApprovals.map((approval) => (
                <ApprovalCardInline key={approval.approval_id} approval={approval} onApprove={decideApproval} />
              ))}
            </div>
            <form className="composer" onSubmit={submitRun}>
              <label className="composer-field">
                <span>Ask {agentDisplayName}</span>
                <textarea
                  value={message}
                  onChange={(event) => setMessage(event.target.value)}
                  placeholder={`Ask ${agentDisplayName} to build, fix, research, inspect, or continue something...`}
                  rows={3}
                />
              </label>
              <div className="composer-bar">
                <label className="mode-select">
                  <span>Mode</span>
                  <select value={autonomyMode} onChange={(event) => setAutonomyMode(event.target.value)}>
                    {autonomyOptions
                      .filter((option) => option.value !== "autonomous" || Boolean((runtime as RuntimeConfig | null)?.feature_flags?.enable_autonomous_scheduler))
                      .map((option) => (
                        <option key={option.value} value={option.value}>
                          {option.label}
                        </option>
                      ))}
                  </select>
                </label>
                <button type="submit" disabled={!message.trim()}>
                  <Send size={15} /> Send
                </button>
              </div>
            </form>
          </div>

        </section>

          </>
        )}
        {activeSection === "advanced" && (
          <section id="advanced" className="shell page-shell advanced-page" data-section="advanced" aria-label="Advanced Operator Console">
            <header className="page-head">
              <div>
                <p className="page-eyebrow">Operator Console</p>
                <h1 className="page-title">Advanced<em>.</em></h1>
                <p className="page-subtitle">
                  Tuning surfaces for the runtime that powers Kestrel: runs, approvals, memory,
                  tools, MCP, plugins, channels, traces, and gated capabilities. Defaults stay conservative.
                </p>
              </div>
              <div className="page-actions">
                <button className="btn subtle" type="button" onClick={() => refreshAll().catch(reportError)}>
                  <RefreshCw size={15} /> Refresh
                </button>
                <button className="btn primary" type="button" onClick={() => routeToSection("chat")}>
                  <X size={15} /> Close
                </button>
              </div>
            </header>
            {error && <ActionError message={error} onDismiss={() => setError(null)} />}
            <section className="stitch-command-deck advanced-overview" aria-label="Advanced overview">
              <div className="stitch-hero-card">
                <div>
                  <span className="stitch-kicker"><span aria-hidden="true"></span> Command Center</span>
                  <h2>{activeRun ? "Run selected" : "Runtime cockpit"}</h2>
                  <p>{activeRun ? `${activeRun.run_id} · ${activeRun.workspace || "configured workspace"}` : "Inspect evidence, memory, tools, gates, and runtime internals from here."}</p>
                </div>
                <StatusBadge value={activeRun?.status ?? "ready"} />
              </div>
              <div className="stitch-stat-grid">
                <Metric label="Task Capsules" value={runs.length} />
                <Metric label="Mutation Gate" value={`${activeDeltaCount}/${totalDeltaCount}`} />
                <Metric label="Approvals" value={pendingApprovalCount} />
                <Metric label="Tools Online" value={enabledToolCount} />
              </div>
              <div className="stitch-oracle-card">
                <span className="stitch-kicker"><Route size={13} /> ORACLE Shadow</span>
                <strong>{oracleShadowLabel}</strong>
                <p>Routing remains advisory. Policy writes stay behind exact-call gates.</p>
              </div>
            </section>
            <nav className="section-index" aria-label="Advanced section index">
              {[
                ["runtime", "Run agent"],
                ["runs", "Runs"],
                ["approvals", "Approvals"],
                ["soul", "Soul"],
                ["memory", "Memory"],
                ["behavior-deltas", "Behavior Deltas"],
                ["tools", "Tools"],
                ["mcp", "MCP"],
                ["skills", "Skills"],
                ["channels", "Channels"],
                ["observability", "Observability"]
              ].map(([id, label]) => (
                <button className="tag ghost" type="button" key={id} onClick={() => scrollToElement(id)}>
                  {label}
                </button>
              ))}
            </nav>

        <section id="runtime" className="section">
          <Panel
            title="Run Agent"
            icon={<TerminalSquare size={19} />}
            actions={<StatusBadge value={runtime ? "runtime loaded" : "loading"} />}
          >
            <form className="stack-form" onSubmit={submitRun}>
              <Field label="Objective">
                <textarea value={message} onChange={(event) => setMessage(event.target.value)} rows={5} />
              </Field>
              <div className="field-row">
                <Field label="Session ID" hint="Leave blank to create a new session.">
                  <input value={sessionId} onChange={(event) => setSessionId(event.target.value)} />
                </Field>
                <Field label="Workspace" hint="Leave blank for configured workspace.">
                  <input value={workspace} onChange={(event) => setWorkspace(event.target.value)} />
                </Field>
                <Field label="Provider">
                  <select value={provider} onChange={(event) => chooseProvider(event.target.value)}>
                    <ProviderSelectOptions />
                  </select>
                </Field>
                <Field label="Model" hint={providerCatalog?.error ?? modelCatalogLabel}>
                  <div className="model-picker">
                    <input aria-label="Model" list="models" value={model} onChange={(event) => setModel(event.target.value)} />
                    <button
                      type="button"
                      className="icon-btn"
                      title="Refresh model list"
                      aria-label="Refresh model list"
                      onClick={() => refreshProviderModels(provider).catch(reportError)}
                    >
                      <RefreshCw size={15} />
                    </button>
                  </div>
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
              <datalist id="models">
                {modelSuggestions.map((item) => (
                  <option key={`${provider}-${item}`} value={item} />
                ))}
              </datalist>
              <div className="page-actions">
                <button type="submit" disabled={!message.trim()}>
                  <Send size={15} /> Queue Run
                </button>
                {activeRun?.status === "running" && (
                  <button
                    type="button"
                    className="btn danger"
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

          <Panel title="Active Run" icon={<Activity size={19} />}>
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
                  <article className="msg user">
                    <strong>User</strong>
                    <p>{activeRun.message}</p>
                  </article>
                  <article className="msg kestrel">
                    <strong>Kestrel</strong>
                    <MarkdownMessage text={activeRun.assistant_message || streamedAssistant || activeRun.stop_reason || "Working..."} />
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

        <section id="runs" className="section">
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
            <RepairPatchReview
              tasks={taskGraph?.tasks ?? []}
              onPrepareTool={(name, args) => {
                setToolName(name);
                setToolArgs(JSON.stringify(args, null, 2));
                setPreparedToolPreview({ name, args });
              }}
            />
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

        <section id="approvals" className="section">
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

        <section id="soul" className="content-grid wide-left">
          <Panel title="Soul" icon={<Brain size={19} />}>
            {selfState ? (
              <div className="run-detail">
                <div className="run-title">
                  <h3>{String(selfState.identity.display_name ?? "Soul")} / {String(selfState.identity.name ?? "Kestrel")}</h3>
                  <StatusBadge value={Boolean(selfState.config.allow_self_modification) ? "self-edit gated" : "self-edit off"} />
                </div>
                <p className="muted">{String(selfState.identity.description ?? "")}</p>
                <div className="metric-grid">
                  <Metric label="Memory Layers" value={selfState.memory_layers.length} />
                  <Metric label="Tools" value={selfState.tools?.length ?? selfState.tool_count ?? tools.length} />
                  <Metric label="Skills" value={selfState.skills?.length ?? skills.length} />
                  <Metric label="Plugins" value={selfState.plugins?.length ?? plugins.length} />
                </div>
                {onboardingProfile && (
                  <>
                    <h3>Active Profile</h3>
                    <div className="data-row">
                      <strong>{onboardingProfile.agent_name}</strong>
                      <InlineMeta items={[onboardingProfile.persona_name, onboardingProfile.preferred_name || onboardingProfile.user_name]} />
                      <p>{onboardingProfile.working_style || onboardingProfile.communication_notes}</p>
                    </div>
                  </>
                )}
                <h3>Soul Memory Layers</h3>
                <div className="layer-grid">
                  {selfState.memory_layers.map((layer) => (
                    <div className="layer-chip" key={String(layer.layer)}>
                      <strong>{String(layer.layer)}</strong>
                      <small>{String(layer.mv2_file ?? "")}</small>
                    </div>
                  ))}
                </div>
                <h3>Self-Awareness Tools</h3>
                <div className="tool-grid">
                  {(selfState.tools ?? tools)
                    .filter((tool) => tool.name.startsWith("self.") || tool.name.startsWith("web."))
                    .map((tool) => (
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
                        <InlineMeta items={[tool.risk, tool.requires_approval ? "approval" : "direct"]} />
                        <span>{tool.description}</span>
                      </button>
                    ))}
                </div>
              </div>
            ) : (
              <EmptyState>Soul snapshot is loading.</EmptyState>
            )}
          </Panel>

          <Panel title="Soul Memory & Web Context" icon={<Search size={19} />}>
            <form onSubmit={rememberSelf} className="stack-form">
              <Field label="Validated self-memory title">
                <input value={selfTitle} onChange={(event) => setSelfTitle(event.target.value)} />
              </Field>
              <Field label="Validated self-memory content">
                <textarea value={selfContent} onChange={(event) => setSelfContent(event.target.value)} rows={4} />
              </Field>
              <Field label="Schema">
                <select value={selfSchema} onChange={(event) => setSelfSchema(event.target.value)}>
                  <option value="identity_summary">identity_summary</option>
                  <option value="capability_snapshot">capability_snapshot</option>
                  <option value="user_profile">user_profile</option>
                  <option value="agent_persona">agent_persona</option>
                  <option value="user_workflow_preference">user_workflow_preference</option>
                  <option value="self_change_request">self_change_request</option>
                  <option value="validation_metadata">validation_metadata</option>
                </select>
              </Field>
              <button type="submit" disabled={!selfTitle.trim() || !selfContent.trim()}>Remember in Soul</button>
            </form>
            {selfRememberResult && <JsonBlock value={selfRememberResult} />}
            <form onSubmit={searchWeb} className="stack-form separated">
              <Field label="Gated web query">
                <input value={webQuery} onChange={(event) => setWebQuery(event.target.value)} />
              </Field>
              <button type="submit" disabled={!webQuery.trim()}>Search Web</button>
            </form>
            {webResult && <JsonBlock value={webResult} />}
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
              <div className="page-actions">
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
              <div className="field-row">
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

          <Panel title="Behavior Deltas Review" icon={<ShieldCheck size={19} />}>
            <section aria-label="Behavior Deltas Review" className="run-detail">
              <h3>Behavior Deltas Review</h3>
              <p className="muted">Mutation actions require exact-call approval and MutationGate review.</p>
              {behaviorDeltaError && <p className="danger-text">Behavior delta ledger unavailable: {behaviorDeltaError}</p>}
              {behaviorDeltaReport ? (
                <>
                  <div className="metric-grid">
                    <Metric label="Total Deltas" value={behaviorDeltaReport.summary.total_deltas} />
                    <Metric label="Active" value={behaviorDeltaReport.summary.active_deltas} />
                    <Metric label="Useful Rate" value={formatPercent(behaviorDeltaReport.summary.useful_rate)} />
                    <Metric label="Never Activated" value={behaviorDeltaReport.summary.never_activated} />
                  </div>

                  <section aria-label="Learning Dashboard" className="run-detail">
                    <h3>Learning Dashboard</h3>
                    <p className="muted">Read-only rollout telemetry for autonomous learning defaults and rollback safety.</p>
                    {learningDashboardError && <p className="danger-text">Learning dashboard unavailable: {learningDashboardError}</p>}
                    {learningDashboard ? (
                      <>
                        <div className="metric-grid">
                          <Metric label="Auto-activations" value={learningDashboard.headline.auto_activations} />
                          <Metric label="Rollbacks" value={learningDashboard.headline.rollbacks} />
                          <Metric label="FP Rate" value={formatPercent(learningDashboard.headline.false_positive_rate)} />
                          <Metric label="Activations then rolled back" value={learningDashboard.headline.activations_then_rolled_back} />
                        </div>
                        <div className="list compact-list">
                          {learningDashboard.layers.map((layer) => (
                            <div className="data-row" key={layer.layer}>
                              <strong>{layer.layer}</strong>
                              <InlineMeta items={[`${layer.activations} activations`, `${layer.auto_activations} auto`, `${layer.rollbacks} rollbacks`]} />
                              <p>{`False positives ${formatPercent(layer.false_positive_rate)} · rollback avg ${layer.average_time_to_rollback_hours ?? "n/a"}h`}</p>
                            </div>
                          ))}
                          {learningDashboard.layers.length === 0 && <EmptyState>No learning dashboard activity recorded.</EmptyState>}
                        </div>
                      </>
                    ) : (
                      <EmptyState>Learning dashboard is loading.</EmptyState>
                    )}
                  </section>
                  <div className="list compact-list">
                    {behaviorDeltaReport.deltas.slice(0, 12).map((delta) => (
                      <div className="data-row" key={delta.delta_id}>
                        <strong>{delta.title}</strong>
                        <InlineMeta items={[delta.delta_id, `${delta.status} · ${delta.kind} · ${delta.risk}`, `${delta.activation_count} activations`]} />
                        <p>{`Useful ${formatPercent(delta.useful_rate)} · Failure ${formatPercent(delta.failure_rate)} · Rollback ${formatPercent(delta.rollback_rate)}`}</p>
                        <StatusBadge value={delta.target_layer} />
                      </div>
                    ))}
                    {behaviorDeltaReport.deltas.length === 0 && <EmptyState>No behavior deltas recorded.</EmptyState>}
                  </div>
                </>
              ) : (
                <EmptyState>Behavior delta report is loading.</EmptyState>
              )}
            </section>
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

        <section id="tools" className="section">
          <Panel title="Connected Tools" icon={<Wrench size={19} />}>
            <form onSubmit={invokeTool} className="stack-form">
              <Field label="Tool">
                <select
                  value={toolName}
                  onChange={(event) => {
                    const selected = tools.find((tool) => tool.name === event.target.value);
                    setPreparedToolPreview(null);
                    setToolName(event.target.value);
                    setToolArgs(JSON.stringify(schemaDefault(selected?.parameters), null, 2));
                  }}
                >
                  <option value="">Select a tool</option>
                  {tools.filter((tool) => isToolEffectivelyEnabled(tool, toolPermissions, capabilities)).map((tool) => (
                    <option key={tool.name} value={tool.name}>{tool.name}</option>
                  ))}
                </select>
              </Field>

              <Field label="Arguments JSON">
                <textarea
                  value={toolArgs}
                  onChange={(event) => {
                    setPreparedToolPreview(null);
                    setToolArgs(event.target.value);
                  }}
                  rows={8}
                />
              </Field>
              {preparedToolPreview && <ExactCallApprovalPreview preview={preparedToolPreview} />}
              <button type="submit" disabled={!toolName || !selectedToolEnabled}>Invoke Tool</button>
            </form>
            <div className="field-row compact">
              <Field label="Filter tools">
                <input value={toolFilter} onChange={(event) => setToolFilter(event.target.value)} />
              </Field>
              <Field label="Tool source">
                <select value={toolSourceFilter} onChange={(event) => setToolSourceFilter(event.target.value)}>
                  <option value="all">All sources</option>
                  {toolSources.map((source) => <option key={source} value={source}>{source}</option>)}
                </select>
              </Field>
              <Field label="Tool risk">
                <select value={toolRiskFilter} onChange={(event) => setToolRiskFilter(event.target.value)}>
                  <option value="all">All risks</option>
                  {toolRisks.map((risk) => <option key={risk} value={risk}>{risk}</option>)}
                </select>
              </Field>
              <Field label="Tool enabled state">
                <select value={toolEnabledFilter} onChange={(event) => setToolEnabledFilter(event.target.value)}>
                  <option value="all">All states</option>
                  <option value="enabled">Enabled</option>
                  <option value="disabled">Disabled</option>
                </select>
              </Field>
            </div>
            <InlineMeta items={[`${filteredTools.length}/${tools.length} tools shown`]} />
            <div className="tool-grid" aria-label="Tool cards">
              {filteredTools.length === 0 ? <EmptyState>No tools match the current filters.</EmptyState> : filteredTools.map((tool) => {
                const enabled = isToolEffectivelyEnabled(tool, toolPermissions, capabilities);
                return (
                  <button
                    type="button"
                    className={`tool-card ${enabled ? "" : "disabled"}`}
                    key={tool.name}
                    disabled={!enabled}
                    title={enabled ? `Prepare ${tool.name}` : `${tool.name} is disabled in Settings`}
                    onClick={() => {
                      setToolName(tool.name);
                      setToolArgs(JSON.stringify(schemaDefault(tool.parameters), null, 2));
                    }}
                  >
                    <strong>{tool.name}</strong>
                    <InlineMeta
                      items={[
                        tool.source,
                        tool.risk,
                        enabled ? "enabled" : `disabled: ${tool.enablement_flag ?? "config"}`,
                        tool.requires_approval ? "approval" : "direct"
                      ]}
                    />
                    <span>{tool.description}</span>
                  </button>
                );
              })}
            </div>
          </Panel>
          <Panel title="Tool Result" icon={<Activity size={19} />}>
            {toolResult ? <JsonBlock value={toolResult} maxHeight="520px" /> : <EmptyState>No tool invoked from the UI yet.</EmptyState>}
          </Panel>
          <Panel title="Secret Broker" icon={<KeyRound size={19} />}>
            <form onSubmit={saveSecret} className="stack-form">
              <div className="field-row">
                <Field label="Secret name">
                  <input value={secretName} onChange={(event) => setSecretName(event.target.value)} autoComplete="off" />
                </Field>
                <Field label="Purpose">
                  <input value={secretPurpose} onChange={(event) => setSecretPurpose(event.target.value)} />
                </Field>
              </div>
              <Field label="Secret value" hint="Value is stored by the backend and never returned in API payloads.">
                <input
                  type="password"
                  value={secretValue}
                  onChange={(event) => setSecretValue(event.target.value)}
                  autoComplete="new-password"
                />
              </Field>
              <label className="check-row">
                <input type="checkbox" checked={secretValidate} onChange={(event) => setSecretValidate(event.target.checked)} />
                <span>Validate after save</span>
              </label>
              <button type="submit" disabled={!secretName.trim() || !secretValue.trim()}>
                <KeyRound size={15} /> Store Secret
              </button>
            </form>
            <div className="list separated">
              {secrets.length === 0 ? (
                <EmptyState>No brokered secrets configured.</EmptyState>
              ) : (
                secrets.map((secret) => (
                  <div className="data-row" key={secret.id}>
                    <strong>{secret.name}</strong>
                    <InlineMeta items={[secret.secret_ref, secret.configured ? "configured" : "missing", secret.validated ? "validated" : "unvalidated"]} />
                    {secret.purpose && <p>{secret.purpose}</p>}
                    <div className="page-actions">
                      <button type="button" onClick={() => validateSecret(secret)}>Validate</button>
                      <button type="button" className="btn danger" onClick={() => deleteSecret(secret)}>Delete</button>
                    </div>
                  </div>
                ))
              )}
            </div>
            {secretResult && <JsonBlock value={secretResult} maxHeight="220px" />}
          </Panel>
        </section>

        <section id="mcp" className="content-grid wide-left">
          <Panel title="MCP Servers" icon={<PlugZap size={19} />}>
            <form onSubmit={saveMcp} className="stack-form">
              <div className="field-row">
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
              <div className="check-row">
                <StatusBadge value={loadedMcpServer?.enabled ?? false} />
                <span>Enable or disable this server with its capability switch after saving.</span>
              </div>
              <Field
                label="Args JSON"
                hint={loadedMcpServer && !mcpArgsTouched ? `${loadedMcpServer.argument_count ?? 0} stored arguments are hidden. Edit to replace them.` : undefined}
              >
                <textarea value={mcpArgs} onChange={(event) => { setMcpArgs(event.target.value); setMcpArgsTouched(true); }} rows={3} />
              </Field>
              <Field
                label="Env JSON"
                hint={loadedMcpServer && !mcpEnvTouched ? `${loadedMcpServer.env_keys?.length ?? 0} stored environment names are hidden. Edit to replace them.` : undefined}
              >
                <textarea value={mcpEnv} onChange={(event) => { setMcpEnv(event.target.value); setMcpEnvTouched(true); }} rows={3} />
              </Field>
              <Field
                label="Secret env names JSON"
                hint={loadedMcpServer && !mcpSecretEnvTouched ? `${Object.keys(loadedMcpServer.secret_env_status ?? {}).length} secret bindings are hidden. Edit to replace them.` : undefined}
              >
                <textarea value={mcpSecretEnv} onChange={(event) => { setMcpSecretEnv(event.target.value); setMcpSecretEnvTouched(true); }} rows={3} />
              </Field>
              <button type="submit" disabled={!mcpId.trim()}>Save Server</button>
            </form>
            {mcpServers.map((server) => {
              const serverCapability = capabilityForMcpServer(capabilities, server.id);
              const childCapabilities = serverCapability
                ? capabilities.filter((capability) => capability.kind === "tool" && capability.parent_key === serverCapability.key)
                : [];
              return (
                <div className="data-row" key={server.id}>
                  <button type="button" className="link-button" onClick={() => loadMcp(server)}>{server.name}</button>
                  <InlineMeta
                    items={[
                      server.id,
                      server.transport,
                      server.session_state,
                      `${server.tool_count ?? server.tools.length} tools`,
                      serverCapability?.effective_enabled ?? server.enabled ? "enabled" : "disabled"
                    ]}
                  />
                  <div className="capability-inline-control">
                    <StatusBadge value={server.status} />
                    {serverCapability && (
                      <CapabilitySwitch
                        capability={serverCapability}
                        pending={capabilityPending.has(serverCapability.key)}
                        onChange={setCapabilityEnabled}
                        compact
                      />
                    )}
                  </div>
                  {server.error && <p className="danger-text">{server.error}</p>}
                  {childCapabilities.length > 0 && (
                    <div className="capability-child-list" aria-label={`${server.name} tools`}>
                      {childCapabilities.map((capability) => (
                        <div className="capability-child-row" key={capability.key}>
                          <span>{capability.name}</span>
                          <StatusBadge value={capability.effective_enabled ? "effective on" : "effective off"} />
                          <CapabilitySwitch
                            capability={capability}
                            pending={capabilityPending.has(capability.key)}
                            onChange={setCapabilityEnabled}
                            compact
                          />
                        </div>
                      ))}
                    </div>
                  )}
                  <div className="page-actions">
                    {(["connect", "sync", "test", "restart", "disconnect"] as const).map((action) => (
                      <button type="button" key={action} onClick={() => controlMcp(server, action)}>{action}</button>
                    ))}
                    <button type="button" className="btn danger" onClick={() => deleteMcp(server)}>Delete</button>
                  </div>
                </div>
              );
            })}
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
              <button type="submit" disabled={!mcpToolSelection || !selectedMcpToolEnabled}>Invoke MCP Tool</button>
            </form>
            {mcpResult && <JsonBlock value={mcpResult} maxHeight="420px" />}
          </Panel>
        </section>

        <section id="skills" className="section">
          <Panel
            title="Skills"
            icon={<Sparkles size={19} />}
            actions={<button type="button" onClick={discoverSkills} disabled={skillDiscovering}>{skillDiscovering ? "Discovering" : "Discover"}</button>}
          >
            {skillDiscovery ? (
              <div className="data-row compact">
                <strong>{skillDiscovery.message}</strong>
                <InlineMeta
                  items={[
                    skillDiscovery.skills_dir,
                    `${skillDiscovery.discovered_count} discovered`,
                    `${skillDiscovery.enabled_count} enabled`,
                    `${skillDiscovery.validation_errors.length} rejected`
                  ]}
                />
              </div>
            ) : null}
            <div className="list">
              {skills.length === 0 ? (
                <EmptyState>No discovered skills in the registry.</EmptyState>
              ) : skills.map((skill) => {
                const capability = capabilityForSkill(capabilities, skill.id);
                const effectiveEnabled = capability?.effective_enabled ?? skill.enabled;
                return (
                  <div className="data-row" key={skill.id}>
                    <button
                      type="button"
                      className="link-button"
                      disabled={!effectiveEnabled}
                      onClick={() => setSkillSelection(skill.id)}
                    >
                      {skill.name}
                    </button>
                    <InlineMeta items={[skill.id, effectiveEnabled ? "enabled" : "disabled"]} />
                    <p>{skill.description}</p>
                    {capability ? (
                      <CapabilitySwitch
                        capability={capability}
                        pending={capabilityPending.has(capability.key)}
                        onChange={setCapabilityEnabled}
                        compact
                      />
                    ) : (
                      <button type="button" onClick={() => toggleSkill(skill)}>{skill.enabled ? "Disable" : "Enable"}</button>
                    )}
                  </div>
                );
              })}
            </div>
            {skillDiscovery?.validation_errors.length ? <JsonBlock value={skillDiscovery.validation_errors} maxHeight="180px" /> : null}
          </Panel>
          <Panel title="Run or Install Skill" icon={<Bot size={19} />}>
            <form onSubmit={runSkill} className="stack-form">
              <Field label="Skill">
                <select value={skillSelection} onChange={(event) => setSkillSelection(event.target.value)}>
                  <option value="">Select skill</option>
                  {enabledSkills.map((skill) => <option key={skill.id} value={skill.id}>{skill.id}</option>)}
                </select>
              </Field>
              <Field label="Skill task"><textarea value={skillTask} onChange={(event) => setSkillTask(event.target.value)} rows={3} /></Field>
              <button type="submit" disabled={!skillSelection || !skillTask.trim() || !selectedSkillEnabled}>Run Skill</button>
            </form>
            <form onSubmit={installSkill} className="stack-form separated">
              <Field label="Skill manifest JSON"><textarea value={skillManifest} onChange={(event) => setSkillManifest(event.target.value)} rows={7} /></Field>
              <Field label="Skill instructions"><textarea value={skillInstructions} onChange={(event) => setSkillInstructions(event.target.value)} rows={5} /></Field>
              <button type="submit" disabled={!skillInstructions.trim()}>Install Skill</button>
            </form>
            {skillResult && <JsonBlock value={skillResult} maxHeight="360px" />}
          </Panel>
          <Panel title="Plugins" icon={<GitBranch size={19} />}>
            <form onSubmit={reviewPlugin} className="inline-form">
              <Field label="GitHub source"><input value={pluginSource} onChange={(event) => setPluginSource(event.target.value)} /></Field>
              <Field label="Ref"><input value={pluginRef} onChange={(event) => setPluginRef(event.target.value)} /></Field>
              <label className="check-row">
                <input
                  type="checkbox"
                  checked={pluginEnable}
                  disabled={!reviewedCurrentPlugin || pluginEnableBlockers.length > 0}
                  onChange={(event) => setPluginEnable(event.target.checked)}
                />
                <span>Enable after install</span>
              </label>
              <button type="submit" disabled={!pluginSource.trim()}>Review</button>
              <button
                type="button"
                disabled={!pluginSource.trim() || !reviewedCurrentPlugin || (pluginEnable && pluginEnableBlockers.length > 0)}
                onClick={() => installPlugin().catch(reportError)}
              >
                Install
              </button>
            </form>
            {reviewedCurrentPlugin && pluginReview && (
              <div className="data-row">
                <strong>Review: {pluginReviewName(pluginReview)}</strong>
                <InlineMeta items={[String(pluginReview.risk_report.risk ?? "medium"), pluginReview.commit_sha.slice(0, 12)]} />
                <p>Dependencies: {pluginDependencySummary(pluginReview)}</p>
                <p>Isolation: {pluginIsolationSummary(pluginReview)}</p>
                {pluginEnableBlockers.length > 0 && <InlineMeta items={pluginEnableBlockers} />}
              </div>
            )}
            {plugins.map((plugin) => (
              <div className="data-row" key={plugin.id}>
                <strong>{plugin.name}</strong>
                <InlineMeta items={[plugin.id, plugin.format, plugin.install_status, plugin.enabled ? "enabled" : "disabled"]} />
                <p>{plugin.description}</p>
                {pluginBlockers(plugin).length > 0 && <InlineMeta items={pluginBlockers(plugin)} />}
                <div className="page-actions">
                  <button
                    type="button"
                    disabled={!plugin.enabled && pluginBlockers(plugin).length > 0}
                    onClick={() => pluginAction(plugin, plugin.enabled ? "disable" : "enable")}
                  >
                    {plugin.enabled ? "Disable" : "Enable"}
                  </button>
                  <button type="button" onClick={() => pluginAction(plugin, "update")}>Update</button>
                  <button type="button" className="btn danger" onClick={() => pluginAction(plugin, "remove")}>Remove</button>
                </div>
              </div>
            ))}
            {pluginResult && <JsonBlock value={pluginResult} maxHeight="320px" />}
          </Panel>
        </section>

        <section id="channels" className="section">
          <Panel title="Channels" icon={<Bell size={19} />}>
            <form onSubmit={saveChannel} className="stack-form">
              <div className="field-row">
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
                <div className="page-actions">
                  <button type="button" onClick={() => deleteChannel(channel)} className="btn danger">Delete</button>
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
      </section>
      )}
        {activeSection === "settings" && (
          <section id="settings" className="shell page-shell settings-page" data-section="settings" aria-label="Settings">
            <header className="page-head">
              <div>
                <p className="page-eyebrow">Configuration</p>
                <h1 className="page-title">Settings<em>.</em></h1>
                <p className="page-subtitle">
                  The everyday surface for Kestrel: identity, provider, memory, channels,
                  secrets, and permissions. Deep runtime controls stay one click away in Advanced.
                </p>
              </div>
              <div className="page-actions">
                <button className="btn subtle" type="button" onClick={() => refreshAll().catch(reportError)}>
                  <RefreshCw size={15} /> Refresh
                </button>
                <button className="btn primary" type="button" onClick={() => saveRuntimeSettings().catch(reportError)}>
                  <Check size={15} /> Save Settings
                </button>
                <button className="btn subtle" type="button" onClick={() => jumpToAdvanced("runtime")}>
                  Open Advanced
                </button>
              </div>
            </header>
            {notice && (
              <div className="announcer page-notice" aria-live="polite">
                {notice}
              </div>
            )}
            {error && <ActionError message={error} onDismiss={() => setError(null)} />}

            <section className="section" id="identity">
              <div className="section-head">
                <h2>Identity</h2>
                <p>How this Kestrel instance presents itself across chat, channels, and logs.</p>
                <span className="anchor">/api/runtime/config · name</span>
              </div>
              <div className="section-body">
                <div className="row">
                  <div className="row-label">
                    <strong>Agent name</strong>
                    <p>Shown on the chat surface and used in run metadata.</p>
                  </div>
                  <div className="row-control">
                    <input className="input short" type="text" value={runtimeConfig?.name ?? "Kestrel"} readOnly />
                  </div>
                </div>
                <div className="row">
                  <div className="row-label">
                    <strong>Default autonomy</strong>
                    <p>The level Kestrel starts with for new conversation runs from this browser.</p>
                  </div>
                  <div className="row-control">
                    <div className="segmented" role="tablist" aria-label="Autonomy mode">
                      {autonomyOptions.map((option) => (
                        <button
                          type="button"
                          key={option.value}
                          className={autonomyMode === option.value ? "active" : ""}
                          aria-pressed={autonomyMode === option.value}
                          onClick={() => {
                            setAutonomyMode(option.value);
                            setNotice(`Autonomy set to ${option.label}.`);
                          }}
                        >
                          {option.label}
                        </button>
                      ))}
                    </div>
                  </div>
                </div>
                <div className="row">
                  <div className="row-label">
                    <strong>Workspace</strong>
                    <p>The project root Kestrel operates from. Blank uses the configured workspace.</p>
                  </div>
                  <div className="row-control">
                    <input
                      className="input mono short"
                      type="text"
                      value={workspace}
                      placeholder={String(runtimePaths.workspace ?? ".")}
                      onChange={(event) => setWorkspace(event.target.value)}
                    />
                  </div>
                </div>
              </div>
            </section>

            <section className="section" id="provider">
              <div className="section-head">
                <h2>Provider</h2>
                <p>Which model powers the response loop. The controls here feed new runs immediately.</p>
                <span className="anchor">provider · model · fallback_provider</span>
              </div>
              <div className="section-body">
                <div className="section-row-group">
                  <label>
                    Provider
                    <select className="select" value={provider} onChange={(event) => chooseProvider(event.target.value)}>
                      <ProviderSelectOptions />
                    </select>
                  </label>
                  <label>
                    Model
                    <div className="model-picker">
                      <input className="input" type="text" aria-label="Model" value={model} list="settings-models" onChange={(event) => setModel(event.target.value)} />
                      <button
                        type="button"
                        className="icon-btn"
                        title="Refresh model list"
                        aria-label="Refresh model list"
                        onClick={() => refreshProviderModels(provider).catch(reportError)}
                      >
                        <RefreshCw size={15} />
                      </button>
                      <span className="model-picker-meta">{providerCatalog?.error ?? modelCatalogLabel}</span>
                    </div>
                    <datalist id="settings-models">
                      {modelSuggestions.map((item) => <option key={`settings-${provider}-${item}`} value={item} />)}
                    </datalist>
                  </label>
                  <label>
                    Base URL
                    <input
                      className="input mono"
                      type="text"
                      value={baseUrl}
                      placeholder={selectedProviderOption?.baseUrl ?? "not required"}
                      onChange={(event) => setBaseUrl(event.target.value)}
                    />
                  </label>
                  <label>
                    API key env
                    <input
                      className="input mono"
                      type="text"
                      value={apiKeyEnv}
                      placeholder={providerRequiresKey ? "API_KEY_ENV" : "not required"}
                      onChange={(event) => setApiKeyEnv(event.target.value)}
                    />
                  </label>
                  <label>
                    Provider API key
                    <div className="model-picker">
                      <input
                        className="input mono"
                        type="password"
                        aria-label="Provider API key"
                        value={providerKeyValue}
                        placeholder={providerRequiresKey ? `Paste ${apiKeyEnv || "provider key"}` : "No key needed"}
                        disabled={!apiKeyEnv.trim()}
                        autoComplete="off"
                        onChange={(event) => setProviderKeyValue(event.target.value)}
                      />
                      <button
                        type="button"
                        className="btn"
                        disabled={!apiKeyEnv.trim() || !providerKeyValue.trim()}
                        onClick={() => storeProviderKey().catch(reportError)}
                      >
                        Store provider key
                      </button>
                    </div>
                    <span className="model-picker-meta">
                      {providerRequiresKey ? (providerSecretResult?.secret_ref ?? "Stored in secret broker") : "No key needed"}
                    </span>
                  </label>
                  <label>
                    Temperature
                    <input
                      className="input num"
                      type="number"
                      aria-label="Temperature"
                      min="0"
                      max="2"
                      step="0.1"
                      value={temperature}
                      onChange={(event) => setTemperature(event.target.value)}
                    />
                  </label>
                  <label>
                    Max tool calls
                    <input
                      className="input num"
                      type="number"
                      aria-label="Max tool calls"
                      min="0"
                      max="50"
                      step="1"
                      value={maxToolRounds}
                      onChange={(event) => setMaxToolRounds(event.target.value)}
                    />
                  </label>
                  <label>
                    Key status
                    <span className="settings-status"><StatusBadge value={providerKeyStatus} /></span>
                  </label>
                </div>
                <div className="row">
                  <div className="row-label">
                    <strong>Stream responses</strong>
                    <p>Provider-reported streaming support for this runtime config.</p>
                  </div>
                  <div className="row-control">
                    <label className="toggle">
                      <input
                        type="checkbox"
                        aria-label="Stream responses"
                        checked={streamResponses}
                        onChange={(event) => {
                          setStreamResponses(event.target.checked);
                          setNotice(`Response streaming ${event.target.checked ? "enabled" : "disabled"} for new runs.`);
                        }}
                      />
                      <span className="track"><span className="thumb"></span></span>
                    </label>
                  </div>
                </div>
                <div className="row">
                  <div className="row-label">
                    <strong>Provider timeout</strong>
                    <p>Per-request timeout before the provider path fails.</p>
                  </div>
                  <div className="row-control">
                    <input className="input num" type="number" value={Number(runtimeProvider.timeout_seconds ?? 60)} readOnly />
                    <span className="muted">s</span>
                  </div>
                </div>
              </div>
            </section>

            <section className="section" id="memory-settings">
              <div className="section-head">
                <h2>Memory</h2>
                <p>Kestrel keeps six nested memory layers with conservative promotion gates.</p>
                <span className="anchor">/api/memory/layers · memory_dir</span>
              </div>
              <div className="section-body">
                <div className="row">
                  <div className="row-label">
                    <strong>Backend</strong>
                    <p>In-memory keeps local tests deterministic; Memvid persists in durable <code className="mono">.mv2</code> files.</p>
                  </div>
                  <div className="row-control">
                    <div className="segmented" aria-label="Memory backend">
                      {(["In-memory", "Memvid"] as const).map((backend) => (
                        <button
                          type="button"
                          key={backend}
                          className={memoryBackendDraft === backend ? "active" : ""}
                          aria-pressed={memoryBackendDraft === backend}
                          onClick={() => {
                            setMemoryBackendDraft(backend);
                            setNotice(`Memory backend preference set to ${backend}.`);
                          }}
                        >
                          {backend}
                        </button>
                      ))}
                    </div>
                  </div>
                </div>
                <div className="row">
                  <div className="row-label">
                    <strong>Memory directory</strong>
                    <p>Where the six layer files live when using the Memvid backend.</p>
                  </div>
                  <div className="row-control">
                    <input className="input mono short" type="text" value={String(runtimePaths.memory_dir ?? ".nest/memory")} readOnly />
                  </div>
                </div>
                <div className="layer-grid settings-layer-grid">
                  {memoryLayers.map((layer) => (
                    <article className="layer-card" key={layer.layer}>
                      <h3>{layer.layer}<span className="file">{layer.path}</span></h3>
                      <p className="desc">{layer.backend}</p>
                      <div className="row-meta">
                        <StatusBadge value={layer.ok ? "ok" : "failed"} />
                        <StatusBadge value={layer.exists ? "file present" : "virtual"} />
                      </div>
                    </article>
                  ))}
                </div>
              </div>
            </section>

            <section className="section" id="capabilities" aria-labelledby="capabilities-title">
              <div className="section-head">
                <h2 id="capabilities-title">Capabilities</h2>
                <p>Turn individual tools, MCP servers and their tools, and skills on or off. Changes persist immediately.</p>
                <span className="anchor">/api/capabilities · future invocations</span>
              </div>
              <div className="section-body">
                <div className="metric-grid settings-metrics capability-metrics" aria-label="Capability counts">
                  <Metric label="Total" value={capabilitySnapshot.counts.total} />
                  <Metric label="Configured on" value={capabilitySnapshot.counts.configured_enabled} />
                  <Metric label="Effective on" value={capabilitySnapshot.counts.effective_enabled} />
                  <Metric label="Blocked" value={capabilitySnapshot.counts.blocked} />
                </div>
                <div className="section-row-group capability-toolbar">
                  <label>
                    Search capabilities
                    <input
                      className="input"
                      type="search"
                      value={capabilitySearch}
                      onChange={(event) => setCapabilitySearch(event.target.value)}
                      placeholder="Name, ID, source, or parent"
                    />
                  </label>
                  <label>
                    Kind
                    <select
                      className="select"
                      value={capabilityKindFilter}
                      onChange={(event) => setCapabilityKindFilter(event.target.value as "all" | CapabilityKind)}
                    >
                      <option value="all">All kinds</option>
                      <option value="tool">Tools</option>
                      <option value="mcp_server">MCP servers</option>
                      <option value="skill">Skills</option>
                    </select>
                  </label>
                  <label>
                    State
                    <select
                      className="select"
                      value={capabilityStateFilter}
                      onChange={(event) => setCapabilityStateFilter(event.target.value)}
                    >
                      <option value="all">All states</option>
                      <option value="active">Effective on</option>
                      <option value="off">Configured off</option>
                      <option value="blocked">Blocked</option>
                    </select>
                  </label>
                </div>
                <div className="capability-groups" aria-live="polite">
                  {filteredCapabilities.length === 0 ? (
                    <EmptyState>No capabilities match the current filters.</EmptyState>
                  ) : (
                    capabilityKindOrder.map((kind) => {
                      const rows = filteredCapabilities.filter((capability) => capability.kind === kind);
                      if (rows.length === 0) return null;
                      const groupId = `capability-group-${kind}`;
                      return (
                        <section className="capability-group" key={kind} aria-labelledby={groupId}>
                          <div className="capability-group-head">
                            <h3 id={groupId}>{capabilityKindLabel(kind)}</h3>
                            <span>{rows.length}</span>
                          </div>
                          <div className="capability-list">
                            {rows.map((capability) => (
                              <CapabilityRow
                                key={capability.key}
                                capability={capability}
                                pending={capabilityPending.has(capability.key)}
                                onChange={setCapabilityEnabled}
                              />
                            ))}
                          </div>
                        </section>
                      );
                    })
                  )}
                </div>
              </div>
            </section>

            <section className="section" id="permissions">
              <div className="section-head">
                <h2>Permissions</h2>
                <p>Safe defaults for the local runtime. High-risk work still requires approval.</p>
                <span className="anchor">feature_flags</span>
              </div>
              <div className="section-body">
                <div className="metric-grid settings-metrics">
                  <Metric label="Runs" value={runs.length} />
                  <Metric label="Pending approvals" value={approvals.length} />
                  <Metric label="Tools enabled" value={`${enabledToolCount}/${tools.length}`} />
                  <Metric label="MCP servers" value={mcpServers.length} />
                </div>
                <div className="permission-grid">
                  {toolPermissionDefinitions.map((permission) => {
                    const affectedTools = tools.filter((tool) => tool.enablement_flag === permission.key);
                    const isEnabled = toolPermissions[permission.key];
                    return (
                      <article className="permission-card" key={permission.key}>
                        <div>
                          <strong>{permission.label}</strong>
                          <p>{permission.description}</p>
                          <InlineMeta items={[permission.key, `${affectedTools.length} tools`, permission.risk]} />
                        </div>
                        <label className={`toggle ${permission.risk.includes("critical") ? "danger" : permission.risk.includes("high") ? "warn" : ""}`}>
                          <input
                            type="checkbox"
                            aria-label={permission.label}
                            checked={isEnabled}
                            onChange={(event) => {
                              const checked = event.target.checked;
                              setToolPermissions((draft) => ({ ...draft, [permission.key]: checked }));
                              setNotice(`${permission.label} ${checked ? "enabled" : "disabled"} in the settings draft.`);
                            }}
                          />
                          <span className="track"><span className="thumb"></span></span>
                        </label>
                      </article>
                    );
                  })}
                </div>
                <div className="flag-grid settings-flags">
                  {Object.entries(featureFlags).map(([key, value]) => (
                    <span key={key} className="flag"><StatusBadge value={value} /> {key}</span>
                  ))}
                </div>
              </div>
            </section>

            <section className="section" id="channels-settings">
              <div className="section-head">
                <h2>Channels</h2>
                <p>Inbound and outbound channel adapters. Editing routes to the advanced channel console.</p>
                <span className="anchor">/api/channels</span>
              </div>
              <div className="section-body">
                {channels.length === 0 && <EmptyState>No channels configured.</EmptyState>}
                {channels.map((channel) => (
                  <article
                    className="channel-card"
                    key={channel.id}
                    role={channel.provider === "telegram" ? "group" : undefined}
                    aria-label={channel.provider === "telegram" ? "Telegram setup" : undefined}
                  >
                    <span className="channel-icon"><Bell size={16} /></span>
                    <div className="channel-meta">
                      <strong>{channel.id}</strong>
                      <span className="env">{channel.provider} · {channel.token_env || channel.webhook_url_env || "no env binding"}</span>
                      {channel.provider === "telegram" && (
                        <div className="inline-meta">
                          <StatusBadge value={channelEnvFlag(channel, "token_env_configured") ? "token configured" : "token missing"} />
                          <StatusBadge value={channelEnvFlag(channel, "signature_secret_env_configured") ? "signature configured" : "signature missing"} />
                          {telegramOwnerLabels(channel).map((owner) => <span className="chip" key={owner}>{owner}</span>)}
                        </div>
                      )}
                    </div>
                    <div className="channel-toggles">
                      <span className="mini"><label>enabled</label><StatusBadge value={channel.enabled} /></span>
                      <span className="mini"><label>send</label><StatusBadge value={channel.send_enabled} /></span>
                      <button className="btn" type="button" onClick={() => { loadChannel(channel); jumpToAdvanced("channels"); }}>Edit</button>
                    </div>
                    {channel.provider === "telegram" && (
                      <div className="telegram-setup-row">
                        <label>
                          Telegram public webhook URL
                          <input
                            className="input"
                            aria-label="Telegram public webhook URL"
                            value={telegramWebhookUrl}
                            onChange={(event) => setTelegramWebhookUrl(event.target.value)}
                            placeholder="https://your-public-host/api/channels/telegram/webhook?channel_id=telegram"
                          />
                        </label>
                        <div className="page-actions">
                          <button className="btn" type="button" onClick={() => telegramWebhookInfo(channel)}>Webhook info</button>
                          <button className="btn primary" type="button" onClick={() => telegramSetWebhook(channel)} disabled={!telegramWebhookUrl.trim()}>Set webhook</button>
                          <button className="btn" type="button" onClick={() => telegramDeleteWebhook(channel)}>Delete webhook</button>
                        </div>
                        {telegramActionResult && <JsonBlock value={telegramActionResult} maxHeight="180px" />}
                      </div>
                    )}
                  </article>
                ))}
              </div>
            </section>

            <section className="section" id="secrets-settings">
              <div className="section-head">
                <h2>Secrets</h2>
                <p>Stored locally by the secret broker. API routes return status and handles, not raw values.</p>
                <span className="anchor">/api/secrets</span>
              </div>
              <div className="section-body">
                <form className="section-row-group" onSubmit={saveSecret}>
                  <label>
                    Secret name
                    <input className="input mono" value={secretName} onChange={(event) => setSecretName(event.target.value)} autoComplete="off" />
                  </label>
                  <label>
                    Purpose
                    <input className="input" value={secretPurpose} onChange={(event) => setSecretPurpose(event.target.value)} />
                  </label>
                  <label>
                    Secret value
                    <input className="input" type="password" value={secretValue} onChange={(event) => setSecretValue(event.target.value)} autoComplete="new-password" />
                  </label>
                  <label className="settings-inline-action">
                    <span>Broker action</span>
                    <button className="btn primary" type="submit" disabled={!secretName.trim() || !secretValue.trim()}>Store secret</button>
                  </label>
                </form>
                {secrets.length === 0 ? (
                  <div className="row"><div className="row-label"><strong>No brokered secrets configured.</strong><p>Values saved here are stored by the backend and never echoed back.</p></div></div>
                ) : (
                  secrets.map((secret) => (
                    <div className="env-row" key={secret.id}>
                      <div>
                        <span className="key">{secret.name}</span>
                        <span className="desc">{secret.purpose || secret.secret_ref}</span>
                      </div>
                      <div className="row-control">
                        <StatusBadge value={secret.validated ? "validated" : secret.configured ? "stored" : "missing"} />
                        <button className="btn" type="button" onClick={() => validateSecret(secret)}>Validate</button>
                      </div>
                    </div>
                  ))
                )}
                {secretResult && <JsonBlock value={secretResult} maxHeight="180px" />}
              </div>
            </section>

            <section className="section" id="api-access">
              <div className="section-head">
                <h2>API access</h2>
                <p>The local FastAPI workbench can stay open or be gated by a bearer token.</p>
                <span className="anchor">require_api_auth · NEST_AGENT_API_TOKEN</span>
              </div>
              <div className="section-body">
                <div className="row">
                  <div className="row-label">
                    <strong>Require API authentication</strong>
                    <p>
                      When on, requests need <code className="mono">Authorization: Bearer</code> or <code className="mono">X-Kestrel-API-Key</code>.
                      This launch-controlled boundary requires a configured restart to change.
                    </p>
                  </div>
                  <div className="row-control">
                    <StatusBadge value={apiAuthRequired ? "enabled" : "disabled"} />
                    <span className="muted">Restart required to change</span>
                  </div>
                </div>
                <form className="row" onSubmit={saveToken}>
                  <div className="row-label">
                    <strong>Browser API token</strong>
                    <p>Stored only in this browser client and used for authenticated routes.</p>
                  </div>
                  <div className="row-control">
                    <input className="input mono short" type="password" value={apiTokenDraft} onChange={(event) => setApiTokenDraft(event.target.value)} autoComplete="off" />
                    <button className="btn" type="submit">Save</button>
                  </div>
                </form>
              </div>
            </section>

            <section className="section" id="runtime-json">
              <div className="section-head">
                <h2>Runtime JSON</h2>
                <p>Raw live configuration returned by the server, for auditing and support.</p>
                <span className="anchor">/api/runtime/config</span>
              </div>
              <div className="section-body json-section">
                {runtimeSettingsResult && <JsonBlock value={runtimeSettingsResult} maxHeight="240px" />}
                {runtime ? <JsonBlock value={runtime} maxHeight="680px" /> : <EmptyState>Runtime config is loading.</EmptyState>}
              </div>
            </section>
          </section>
        )}
      </main>
      {activeSection === "chat" && inspectorOpen && (
        <aside className="inspector" aria-label="Run details">
          <div className="inspector-head">
            <h2>Run details</h2>
            <button type="button" aria-label="Close panel" onClick={() => setInspectorOpen(false)}>
              <X size={15} />
            </button>
          </div>
          {activeRun ? (
            <>
              <section>
                <h3>Current run</h3>
                <StatusBadge value={activeRun.status} />
                <InlineMeta items={[activeRun.run_id, activeRun.session_id, activeRun.model]} />
                {activeRun.error && <p className="danger-text">{activeRun.error}</p>}
              </section>
              <section>
                <h3>Plan</h3>
                <TaskList title="Needs You" tasks={taskGraph?.approval_blocked_tasks ?? []} onApprove={approveTask} />
                <TaskList title="Ready" tasks={taskGraph?.ready_tasks ?? []} onApprove={approveTask} />
              </section>
              {proofOfWork && (
                <section>
                  <h3>Validation</h3>
                  <SummaryList title="Completed" values={asStringArray(proofOfWork.completed_steps)} />
                  <SummaryList title="Evidence" values={asStringArray(proofOfWork.validation_evidence)} />
                  <SummaryList title="Risks" values={asStringArray(proofOfWork.remaining_risks)} />
                </section>
              )}
              <section>
                <h3>Activity</h3>
                <div className="trace-list compact-trace">
                  {(runTrace?.timeline ?? events).slice(-12).map((event) => (
                    <div className="trace-row" key={`${event.id}-${event.type}`}>
                      <strong>{friendlyEventLabel(event.type)}</strong>
                      <small>{event.created_at}</small>
                      <code>{JSON.stringify(event.payload).slice(0, 220)}</code>
                    </div>
                  ))}
                </div>
              </section>
            </>
          ) : (
            <EmptyState>No run selected.</EmptyState>
          )}
        </aside>
      )}
    </div>
      )}
      {setupOpen && (
        <SetupWizard
          draft={setupDraft}
          personas={personaPresets}
          existingProfile={onboardingProfile}
          setupReadiness={setupReadiness}
          userDisplayName={userDisplayName}
          onChange={setSetupDraft}
          onSubmit={saveSetup}
          onClose={dismissSetup}
        />
      )}
  </>
  );
}

function SetupWizard({
  draft,
  personas,
  existingProfile,
  setupReadiness,
  userDisplayName,
  onChange,
  onSubmit,
  onClose
}: {
  draft: SetupDraft;
  personas: PersonaPreset[];
  existingProfile: OnboardingProfile | null;
  setupReadiness: SetupReadinessReport | null;
  userDisplayName: string;
  onChange: (draft: SetupDraft) => void;
  onSubmit: (event: FormEvent) => void;
  onClose: () => void;
}) {
  const selectedPersona = personas.find((persona) => persona.id === draft.persona) ?? personas[0] ?? defaultPersonaPresets[0];
  const update = (patch: Partial<SetupDraft>) => onChange({ ...draft, ...patch });
  return (
    <div className="setup-backdrop" role="presentation">
      <section className="setup-dialog" role="dialog" aria-modal="true" aria-labelledby="setup-title">
        <header className="setup-head">
          <div>
            <p className="page-eyebrow">{existingProfile ? "Soul Setup" : "Welcome"}</p>
            <h1 id="setup-title">{existingProfile ? "Tune your Kestrel" : "Meet your Kestrel"}</h1>
            <p>
              {userDisplayName
                ? `${userDisplayName}, this profile lives in the Soul layer so your agent can keep the relationship coherent.`
                : "Name the agent, choose its voice, and give it a first sketch of how you like to work."}
            </p>
          </div>
          <button type="button" aria-label="Close setup" onClick={onClose}>
            <X size={16} />
          </button>
        </header>

        <form className="setup-grid" onSubmit={onSubmit}>
          <div className="setup-section setup-readiness-panel">
            <h2>First-run readiness</h2>
            {setupReadiness ? (
              <>
                <div className="readiness-summary">
                  <StatusBadge value={setupReadiness.ready ? "ready" : "not ready"} />
                  <span>{setupReadiness.pass_count} pass · {setupReadiness.warn_count} warn · {setupReadiness.fail_count} fail</span>
                </div>
                <p className="setup-hint">{setupReadiness.next_action}</p>
                <div className="readiness-check-list">
                  {setupReadiness.checks.slice(0, 4).map((check) => (
                    <article className="readiness-check" key={check.check_id}>
                      <div>
                        <strong>{check.title}</strong>
                        <p>{check.detail}</p>
                      </div>
                      <StatusBadge value={check.status} />
                    </article>
                  ))}
                </div>
              </>
            ) : (
              <EmptyState>Setup readiness has not loaded yet.</EmptyState>
            )}
          </div>

          <div className="setup-section">
            <h2>Names</h2>
            <div className="field-row">
              <Field label="Agent name">
                <input value={draft.agent_name} onChange={(event) => update({ agent_name: event.target.value })} />
              </Field>
              <Field label="Your name">
                <input value={draft.user_name} onChange={(event) => update({ user_name: event.target.value })} />
              </Field>
              <Field label="What should it call you?">
                <input value={draft.preferred_name} onChange={(event) => update({ preferred_name: event.target.value })} />
              </Field>
            </div>
          </div>

          <div className="setup-section">
            <h2>Persona</h2>
            <div className="persona-grid" role="radiogroup" aria-label="Kestrel persona">
              {personas.map((persona) => (
                <button
                  type="button"
                  role="radio"
                  aria-checked={draft.persona === persona.id}
                  className={`persona-card ${draft.persona === persona.id ? "active" : ""}`}
                  key={persona.id}
                  onClick={() => update({ persona: persona.id })}
                >
                  <strong>{persona.name}</strong>
                  <span>{persona.summary}</span>
                </button>
              ))}
            </div>
            <p className="setup-hint">{selectedPersona.guidance}</p>
          </div>

          <div className="setup-section">
            <h2>Working Together</h2>
            <Field label="What are you usually trying to get done?">
              <textarea
                value={draft.goals_text}
                onChange={(event) => update({ goals_text: event.target.value })}
                rows={3}
                placeholder="Ship Kestrel, build local tools, research product ideas..."
              />
            </Field>
            <Field label="How do you like collaboration to feel?">
              <textarea
                value={draft.working_style}
                onChange={(event) => update({ working_style: event.target.value })}
                rows={3}
                placeholder="Short plans, direct tradeoffs, live verification, no fluff..."
              />
            </Field>
          </div>

          <div className="setup-section">
            <h2>Fun Details</h2>
            <Field label="Interests or recurring themes">
              <textarea
                value={draft.interests_text}
                onChange={(event) => update({ interests_text: event.target.value })}
                rows={3}
                placeholder="Local-first software, thoughtful UI, creative automation..."
              />
            </Field>
            <Field label="Anything else it should remember?">
              <textarea
                value={draft.communication_notes}
                onChange={(event) => update({ communication_notes: event.target.value })}
                rows={3}
                placeholder="Tone preferences, pet peeves, project rituals, decision style..."
              />
            </Field>
            <label className="check-row">
              <input
                type="checkbox"
                checked={draft.continuous_learning}
                onChange={(event) => update({ continuous_learning: event.target.checked })}
              />
              <span>Keep adapting from explicit remember requests and confirmed corrections.</span>
            </label>
          </div>

          <footer className="setup-actions">
            <button type="button" className="btn subtle" onClick={onClose}>Later</button>
            <button type="submit" className="btn primary" disabled={!draft.agent_name.trim()}>
              <Sparkles size={15} /> Save to Soul
            </button>
          </footer>
        </form>
      </section>
    </div>
  );
}

function ExactCallApprovalPreview({ preview }: { preview: PreparedToolPreview }) {
  return (
    <section aria-label="Exact-call approval preview" className="run-detail exact-call-preview">
      <div className="run-title">
        <h3>{`Prepared exact-call request: ${preview.name}`}</h3>
        <StatusBadge value="not executed" />
      </div>
      <p>{exactCallPreviewMessage}</p>
      <a className="btn subtle" href="#tools">Review prepared request in tool form</a>
      <JsonBlock value={preview.args} maxHeight="180px" />
    </section>
  );
}

function RepairPatchReview({
  tasks,
  onPrepareTool
}: {
  tasks: TaskNode[];
  onPrepareTool: (name: string, args: Record<string, unknown>) => void;
}) {
  const repairTasks = tasks.filter((task) =>
    (task.required_tools ?? []).some((tool) => tool.startsWith("repair.") || tool === "git.commit")
  );
  if (repairTasks.length === 0) return null;

  const validationTask = repairTasks.find((task) => taskUsesTool(task, "repair.validate") || taskUsesTool(task, "repair.orchestrate_validate"));
  const reviewTask = repairTasks.find((task) => taskUsesTool(task, "repair.review"));
  const rollbackTask = repairTasks.find((task) => taskUsesTool(task, "repair.rollback"));

  const validationResult = validationTask?.result ?? null;
  const validation = readRecord(validationResult?.validation);
  const validationSuccess = validation?.success === true;
  const validationCommand = formatCommand(validation?.command);

  const reviewResult = reviewTask?.result ?? null;
  const reviewId = String(reviewResult?.review_id ?? "pending");
  const diffHash = String(reviewResult?.diff_hash ?? "pending");
  const changedFiles = asStringArray(reviewResult?.changed_files);
  const commitGate = readRecord(reviewResult?.commit_gate);
  const commitApprovalRequired = commitGate?.approval_required_before_commit === true;

  const rollbackResult = rollbackTask?.result ?? null;
  const rollbackId = String(rollbackResult?.rollback_id ?? "pending");
  const restoredFiles = asStringArray(rollbackResult?.restored_files);
  const artifactPath = String(rollbackResult?.artifact_path ?? ".nest/repair_rollbacks");
  const hasReviewArtifact = reviewId !== "pending";
  const prepareCommit = () => {
    onPrepareTool("git.commit", {
      message: `repair: commit reviewed changes for ${reviewId}`,
      repair_review_id: reviewId
    });
  };
  const prepareRollback = () => {
    onPrepareTool("repair.rollback", {
      reason: `Rollback reviewed repair ${reviewId}`,
      review_id: reviewId
    });
  };

  return (
    <section aria-label="Repair Patch Review" className="run-detail repair-review-panel">
      <div className="run-title">
        <h3>Repair Patch Review</h3>
        <StatusBadge value={reviewTask?.status ?? validationTask?.status ?? "pending"} />
      </div>
      <p className="muted">Validation, reviewer gate, and rollback evidence for the selected repair DAG.</p>
      <div className="list compact-list">
        {validationTask && (
          <div className="data-row">
            <strong>{validationSuccess ? "Validation passed" : "Validation pending"}</strong>
            <InlineMeta items={[validationTask.status, validationTask.risk, validationTask.scheduler_reason]} />
            <p>{`${validationSuccess ? "Validation passed" : "Validation state"}: ${validationCommand || validationTask.title}`}</p>
          </div>
        )}
        {reviewTask && (
          <div className="data-row">
            <strong>Review gate</strong>
            <InlineMeta items={[reviewTask.status, reviewTask.profile, commitApprovalRequired ? "exact-call commit approval" : "commit gate pending"]} />
            <p>{`Review gate: ${reviewId} · ${commitApprovalRequired ? "commit approval required" : "commit gate pending"}`}</p>
            <p>{`Diff ${diffHash} · ${changedFiles.length ? changedFiles.join(", ") : "no changed files recorded"}`}</p>
            <button type="button" className="btn subtle" disabled={!hasReviewArtifact} onClick={prepareCommit}>
              Prepare exact-call git.commit request
            </button>
          </div>
        )}
        {rollbackTask && (
          <div className="data-row">
            <strong>Rollback state</strong>
            <InlineMeta items={[rollbackTask.status, rollbackTask.risk, rollbackTask.approved ? "approved" : "approval required"]} />
            <p>{`Rollback state: ${rollbackTask.status} · ${rollbackId}`}</p>
            <p>{`Restores ${restoredFiles.length ? restoredFiles.join(", ") : "recorded repair files"} and preserves ${artifactPath}`}</p>
            <button type="button" className="btn subtle" disabled={!hasReviewArtifact} onClick={prepareRollback}>
              Prepare exact-call repair.rollback request
            </button>
          </div>
        )}
      </div>
    </section>
  );
}

function taskUsesTool(task: TaskNode, toolName: string): boolean {
  return (task.required_tools ?? []).includes(toolName);
}

function readRecord(value: unknown): Record<string, unknown> | null {
  return value && typeof value === "object" && !Array.isArray(value) ? (value as Record<string, unknown>) : null;
}

function formatCommand(value: unknown): string {
  if (Array.isArray(value)) return value.map((part) => String(part)).filter(Boolean).join(" ");
  return typeof value === "string" ? value : "";
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
    <article className="approval-card" role="group" aria-label={`Approval for ${approval.tool_name}`}>
      <div>
        <strong>{approval.tool_name}</strong>
        <InlineMeta items={[riskLabel(approval.risk), approval.run_id, approval.tool_call_id]} />
      </div>
      <JsonBlock value={approval.arguments} maxHeight="160px" />
      <div className="page-actions">
        <button type="button" onClick={() => onApprove(approval, true)}><Check size={15} /> Approve</button>
        <button type="button" className="btn danger" onClick={() => onApprove(approval, false)}><X size={15} /> Deny</button>
      </div>
    </article>
  );
}

function ApprovalCardInline({ approval, onApprove }: { approval: Approval; onApprove: (approval: Approval, approved: boolean) => void }) {
  return (
    <div className="approval-card inline-approval" role="group" aria-label={`Approval for ${approval.tool_name}`}>
      <div>
        <span className="progress-chip">Needs approval</span>
        <strong>{approval.tool_name}</strong>
        <InlineMeta items={[riskLabel(approval.risk), summarizeArguments(approval.arguments)]} />
      </div>
      <details>
        <summary>View raw JSON</summary>
        <JsonBlock value={approval.arguments} maxHeight="160px" />
      </details>
      <div className="page-actions">
        <button type="button" onClick={() => onApprove(approval, true)}><Check size={15} /> Approve</button>
        <button type="button" className="btn danger" onClick={() => onApprove(approval, false)}><X size={15} /> Deny</button>
      </div>
    </div>
  );
}

function MarkdownMessage({ text }: { text: string }) {
  return (
    <div className="markdown-message">
      <ReactMarkdown remarkPlugins={markdownPlugins} components={markdownComponents}>
        {text}
      </ReactMarkdown>
    </div>
  );
}

function LiveRunActivity({ run, events }: { run: Run; events: TraceEvent[] }) {
  const items = activityItemsForEvents(events);
  const isRunning = run.status === "queued" || run.status === "running";
  if (items.length === 0 && !isRunning) return null;
  return (
    <div className="activity" aria-label="Live run activity" aria-live="polite">
      <div className="act-heading">
        <Brain size={15} />
        <strong>Thinking</strong>
      </div>
      {items.map((item) => (
        <div className={`act-row ${item.status === "completed" ? "done" : item.status === "running" ? "run" : item.status === "failed" ? "fail" : "info"}`} key={item.id}>
          <span className="act-icon" aria-hidden="true">
            {activityIcon(item)}
          </span>
          <span className="text">
            <strong>{item.label}</strong>
            {item.meta && <code>{item.meta}</code>}
            {item.detail && <span className="detail">{item.detail}</span>}
          </span>
        </div>
      ))}
      {isRunning && <TypingIndicator />}
    </div>
  );
}

function TypingIndicator() {
  return (
    <div className="typing" aria-label="Kestrel is responding">
      <span>Working</span>
      <span className="dots" aria-hidden="true">
        <span></span>
        <span></span>
        <span></span>
      </span>
    </div>
  );
}

function activityIcon(item: LiveActivityItem) {
  if (item.status === "completed") return <Check size={14} />;
  if (item.status === "failed") return <X size={14} />;
  if (item.kind === "tool") return <Wrench size={14} />;
  return <Sparkles size={14} />;
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

function ActionError({ message, onDismiss }: { message: string; onDismiss: () => void }) {
  return (
    <div className="alert" role="alert">
      <strong>Action failed</strong>
      <span>{message}</span>
      <button type="button" onClick={onDismiss} aria-label="Dismiss error">
        <X size={15} />
      </button>
    </div>
  );
}

function CapabilityRow({
  capability,
  pending,
  onChange
}: {
  capability: Capability;
  pending: boolean;
  onChange: (capability: Capability, enabled: boolean) => Promise<void>;
}) {
  const rowId = capabilityDomId(capability.key);
  const titleId = `${rowId}-title`;
  const blockerId = capability.blocked_by.length > 0 ? `${rowId}-blockers` : undefined;
  const needsReauthorization =
    capability.configured_enabled && capability.blocked_by.includes("resource_changed");
  return (
    <article className="capability-row" aria-labelledby={titleId} aria-busy={pending}>
      <div className="capability-row-copy">
        <div className="capability-row-title">
          <strong id={titleId}>{capability.name}</strong>
          <code>{capability.id}</code>
        </div>
        <p>{capability.description}</p>
        <InlineMeta items={[capability.source, capability.parent_key, capability.enablement_flag, capability.status]} />
        {blockerId && (
          <p className="capability-blockers" id={blockerId}>
            <strong>Blocked by:</strong> {capability.blocked_by.map(formatCapabilityBlocker).join(", ")}
          </p>
        )}
      </div>
      <div className="capability-row-status">
        <div className="capability-badges" aria-label={`${capability.name} policy`}>
          <StatusBadge value={capability.risk} />
          <StatusBadge value={capability.requires_approval ? "approval required" : "direct"} />
          <StatusBadge value={capability.effective_enabled ? "effective on" : "effective off"} />
        </div>
        <CapabilitySwitch
          capability={capability}
          pending={pending}
          onChange={onChange}
          describedBy={blockerId}
        />
        {needsReauthorization && (
          <button
            type="button"
            className="btn subtle"
            disabled={pending}
            onClick={() => void onChange(capability, true)}
          >
            Reauthorize
          </button>
        )}
      </div>
    </article>
  );
}

function CapabilitySwitch({
  capability,
  pending,
  onChange,
  describedBy,
  compact = false
}: {
  capability: Capability;
  pending: boolean;
  onChange: (capability: Capability, enabled: boolean) => Promise<void>;
  describedBy?: string;
  compact?: boolean;
}) {
  const action = capability.configured_enabled ? "Disable" : "Enable";
  return (
    <label className={`capability-toggle ${compact ? "compact" : ""}`}>
      <span>{pending ? "Saving…" : capability.configured_enabled ? "On" : "Off"}</span>
      <span className="toggle">
        <input
          type="checkbox"
          role="switch"
          aria-label={`${action} ${capability.name}`}
          aria-describedby={describedBy}
          aria-checked={capability.configured_enabled}
          checked={capability.configured_enabled}
          disabled={pending}
          onChange={(event) => void onChange(capability, event.currentTarget.checked)}
        />
        <span className="track"><span className="thumb"></span></span>
      </span>
    </label>
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

function simpleChatStatus(
  activeRun: Run | null,
  pendingApprovalCount: number,
  setupReadiness: SetupReadinessReport | null
): { label: string; detail: string } {
  if (pendingApprovalCount > 0) {
    return {
      label: "Needs approval",
      detail: "Review the request before Kestrel continues."
    };
  }
  if (activeRun?.status === "queued" || activeRun?.status === "running") {
    return {
      label: "Working",
      detail: "Kestrel is working and will show progress here."
    };
  }
  if (activeRun?.status === "blocked") {
    return {
      label: "Needs attention",
      detail: "Kestrel needs a decision before continuing."
    };
  }
  if (activeRun?.status === "failed") {
    return {
      label: "Needs attention",
      detail: activeRun.error || "The last run failed."
    };
  }
  if (setupReadiness && !setupReadiness.ready) {
    return {
      label: "Needs setup",
      detail: setupReadiness.next_action || "Finish setup before relying on this Kestrel."
    };
  }
  return {
    label: "Ready",
    detail: activeRun ? "Kestrel is ready for the next message." : "Start a chat to begin."
  };
}

function simpleThreadStatus(status: string): string {
  if (status === "queued" || status === "running") return "Working";
  if (status === "blocked") return "Needs approval";
  if (status === "failed") return "Needs attention";
  if (status === "cancelled") return "Cancelled";
  return "Ready";
}

function messageCountLabel(count: number): string {
  return `${count} ${count === 1 ? "message" : "messages"}`;
}

function asStringArray(value: unknown): string[] {
  return Array.isArray(value) ? value.map((item) => String(item)).filter(Boolean) : [];
}

function scoreLabel(value: unknown): string {
  return typeof value === "number" ? value.toFixed(2) : "";
}

function formatPercent(value: number): string {
  if (!Number.isFinite(value)) return "0%";
  return `${Math.round(value * 100)}%`;
}

function uniqueStrings(values: string[]): string[] {
  return Array.from(new Set(values.filter((value) => value.trim()).sort()));
}

function pluginReviewName(review: PluginReviewReport): string {
  return String(review.manifest.id ?? review.source_url);
}

function pluginDependencySummary(review: PluginReviewReport): string {
  const declared = review.dependency_review.declared;
  if (!declared || typeof declared !== "object" || Array.isArray(declared)) return "none";
  const parts = Object.entries(declared).flatMap(([kind, value]) =>
    stringArray(value).map((item) => `${kind}:${item}`)
  );
  return parts.length ? parts.join(", ") : "none";
}

function pluginIsolationSummary(review: PluginReviewReport): string {
  const mode = String(review.isolation_review.mode ?? "shared");
  const required = Boolean(review.isolation_review.required);
  const available = Boolean(review.isolation_review.available);
  return `${mode}${required ? " required" : ""}${available ? "" : " unavailable"}`;
}

function pluginBlockers(plugin: Plugin): string[] {
  return stringArray(plugin.risk_report.enable_blockers);
}

function stringArray(value: unknown): string[] {
  return Array.isArray(value) ? value.map((item) => String(item)).filter(Boolean) : [];
}

function createThreadId(): string {
  return `thread_${crypto.randomUUID()}`;
}

function sectionFromHash(hash: string): "chat" | "advanced" | "settings" | null {
  const normalized = hash.replace(/^#/, "").toLowerCase();
  return normalized === "chat" || normalized === "advanced" || normalized === "settings" ? normalized : null;
}

function scrollToElement(id: string) {
  const target = document.getElementById(id);
  if (typeof target?.scrollIntoView === "function") {
    target.scrollIntoView({ block: "start", behavior: "smooth" });
  }
}

function runtimeSettingsFrom(config: RuntimeConfig | null): Record<string, unknown> {
  const runtimeSettings = config?.settings?.runtime;
  return runtimeSettings && typeof runtimeSettings === "object" && !Array.isArray(runtimeSettings)
    ? runtimeSettings as Record<string, unknown>
    : {};
}

function coerceTemperature(value: unknown): number {
  const parsed = Number(value);
  if (!Number.isFinite(parsed)) return 0.2;
  return Math.min(2, Math.max(0, parsed));
}

function formatTemperature(value: unknown): string {
  return String(coerceTemperature(value));
}

function coerceToolRounds(value: unknown): number {
  const parsed = Number(value);
  if (!Number.isFinite(parsed)) return 6;
  return Math.min(50, Math.max(0, Math.trunc(parsed)));
}

function formatToolRounds(value: unknown): string {
  return String(coerceToolRounds(value));
}

function ProviderSelectOptions() {
  return (
    <>
      {providerGroups.map((group) => (
        <optgroup key={group} label={group}>
          {providerOptions
            .filter((option) => option.group === group)
            .map((option) => (
              <option key={option.value} value={option.value}>
                {option.label}
              </option>
            ))}
        </optgroup>
      ))}
    </>
  );
}

function modelsForProvider(provider: string, catalogs: Record<string, ProviderModelCatalog>): string[] {
  const catalogModels = catalogs[provider]?.models ?? [];
  return catalogModels.length ? catalogModels : (modelSuggestionsByProvider[provider] ?? []);
}

function isKnownProviderModel(provider: string, model: string, catalogs: Record<string, ProviderModelCatalog>): boolean {
  return [...modelsForProvider(provider, catalogs), ...(modelSuggestionsByProvider[provider] ?? [])].includes(model);
}

function toolPermissionsFromRuntime(config: RuntimeConfig): ToolPermissionDraft {
  const savedSettings = runtimeSettingsFrom(config);
  const featureFlags = config.feature_flags ?? {};
  return Object.fromEntries(
    toolPermissionDefinitions.map((permission) => [
      permission.key,
      Boolean(savedSettings[permission.key] ?? featureFlags[permission.key])
    ])
  ) as ToolPermissionDraft;
}

function isToolEnabled(tool: Tool, permissions: ToolPermissionDraft): boolean {
  const flag = tool.enablement_flag;
  if (!flag) return typeof tool.enabled === "boolean" ? tool.enabled : true;
  if (flag in permissions) return permissions[flag as ToolPermissionKey];
  if (typeof tool.enabled === "boolean") return tool.enabled;
  return false;
}

function capabilityForTool(capabilities: Capability[], toolName: string): Capability | undefined {
  return capabilities.find((capability) => capability.kind === "tool" && capability.id === toolName);
}

function capabilityForMcpServer(capabilities: Capability[], serverId: string): Capability | undefined {
  return capabilities.find((capability) => capability.kind === "mcp_server" && capability.id === serverId);
}

function capabilityForMcpTool(
  capabilities: Capability[],
  serverId: string,
  tool: Tool & { remote_name?: string }
): Capability | undefined {
  const remoteName = tool.remote_name ?? tool.name;
  const registeredName = tool.name.startsWith("mcp.") ? tool.name : `mcp.${serverId}.${remoteName}`;
  return (
    capabilityForTool(capabilities, registeredName) ??
    capabilityForTool(capabilities, tool.name) ??
    capabilities.find(
      (capability) =>
        capability.kind === "tool" &&
        capability.parent_key === `mcp_server:${serverId}` &&
        [remoteName, registeredName].includes(capability.id)
    )
  );
}

function capabilityForSkill(capabilities: Capability[], skillId: string): Capability | undefined {
  return capabilities.find((capability) => capability.kind === "skill" && capability.id === skillId);
}

function isToolEffectivelyEnabled(
  tool: Tool,
  permissions: ToolPermissionDraft,
  capabilities: Capability[]
): boolean {
  return capabilityForTool(capabilities, tool.name)?.effective_enabled ?? isToolEnabled(tool, permissions);
}

function replaceCapability(snapshot: CapabilitySnapshot, capability: Capability): CapabilitySnapshot {
  const found = snapshot.items.some((item) => item.key === capability.key);
  const items = found
    ? snapshot.items.map((item) => item.key === capability.key ? capability : item)
    : [...snapshot.items, capability];
  return { items, counts: capabilityCounts(items) };
}

function capabilityCounts(items: Capability[]): CapabilitySnapshot["counts"] {
  return {
    total: items.length,
    configured_enabled: items.filter((item) => item.configured_enabled).length,
    effective_enabled: items.filter((item) => item.effective_enabled).length,
    blocked: items.filter((item) => item.blocked_by.length > 0).length
  };
}

function capabilityKindLabel(kind: CapabilityKind): string {
  if (kind === "mcp_server") return "MCP Servers";
  if (kind === "skill") return "Skills";
  return "Tools";
}

function capabilityDomId(key: string): string {
  return `capability-${key.replace(/[^a-zA-Z0-9_-]+/g, "-")}`;
}

function formatCapabilityBlocker(value: string): string {
  return value.replace(/[_:]+/g, " ");
}

function setupDraftFromProfile(profile: OnboardingProfile): SetupDraft {
  return {
    agent_name: profile.agent_name || "Kestrel",
    user_name: profile.user_name || "",
    preferred_name: profile.preferred_name || "",
    persona: profile.persona || "steady",
    working_style: profile.working_style || "",
    goals_text: (profile.goals ?? []).join("\n"),
    interests_text: (profile.interests ?? []).join("\n"),
    communication_notes: profile.communication_notes || "",
    continuous_learning: profile.continuous_learning !== false
  };
}

function splitSetupList(value: string): string[] {
  return value
    .split(/[\n,]/)
    .map((item) => item.trim())
    .filter(Boolean)
    .slice(0, 6);
}

function validAutonomyMode(value: unknown, fallback: string): string {
  const mode = String(value ?? "");
  return mode === "background" || mode === "manual" || mode === "autonomous" ? mode : fallback;
}

function submissionAutonomyMode(value: string): string {
  if (value === "manual") return "manual";
  return "autonomous";
}

function autonomyLabel(value: string): string {
  if (value === "background") return "Safe Auto";
  if (value === "manual") return "Manual";
  if (value === "autonomous") return "Autopilot";
  return value;
}
