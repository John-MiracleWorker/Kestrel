import {
  Activity,
  Bell,
  Bot,
  Brain,
  CalendarClock,
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
  Pencil,
  Play,
  PlugZap,
  Plus,
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
  Trash2,
  Wrench,
  X
} from "lucide-react";
import { FormEvent, useCallback, useEffect, useMemo, useRef, useState } from "react";
import ReactMarkdown, { type Components } from "react-markdown";
import remarkGfm from "remark-gfm";
import { AutomateWorkspace } from "./automate/AutomateWorkspace";
import { ApiAuthError, ApiResponseError, deleteJson, getJson, postJson, putJson, queryString, subscribeJsonEvents } from "./api";
import { getApiToken, setApiToken } from "./auth";
import { ConversationPanel } from "./chat/ConversationPanel";
import { ActionError, EmptyState, Field, InlineMeta, JsonBlock, Metric, Notice, Panel, StatusBadge } from "./components";
import {
  ExtendWorkspace,
  useExtendWorkspace,
} from "./extend/ExtendWorkspace";
import { CapabilityRow } from "./extend/CapabilityControls";
import { capabilityKindLabel, capabilityKindOrder, isToolEffectivelyEnabled } from "./extend/extendUtils";
import { FlockWorkspace } from "./flock/FlockWorkspace";
import { MemoryWorkspace, useMemoryWorkspace } from "./memory/MemoryWorkspace";
import type { MissionLaunch } from "./mission/types";
import { OutcomesDashboard } from "./outcomes/OutcomesDashboard";
import { readDesktopBridge } from "./platform/desktopBridge";
import { isDesktopRuntime } from "./platform/runtimeTransport";
import { ProjectsWorkspace } from "./projects/ProjectsWorkspace";
import { RepairReviewPanel } from "./repair/RepairReviewPanel";
import { SettingsWorkspace } from "./settings/SettingsWorkspace";
import { useSettingsWorkspace } from "./settings/settingsController";
import { SetupCenter } from "./setup/SetupCenter";
import { hasVisitedSetupCenter } from "./setup/presentation";
import {
  deriveThreadTitle,
  eventBelongsToRun,
  eventKey,
  eventTimestamp,
  friendlyEventLabel,
  riskLabel
} from "./runActivity";
import type {
  AgentLogEvent,
  ApiResult,
  Approval,
  Capability,
  CapabilityKind,
  CapabilityMutationResult,
  CapabilitySnapshot,
  Channel,
  McpServer,
  Plugin,
  PluginReviewReport,
  ProviderModelCatalog,
  Run,
  RunTrace,
  Routine,
  RoutineDelivery,
  RoutineOccurrence,
  RoutineRunNowResult,
  RoutineStatus,
  RuntimeConfig,
  SelfState,
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

export type LegacyWorkbenchSection =
  | "mission"
  | "chat"
  | "outcomes"
  | "memory"
  | "routines"
  | "routing"
  | "advanced"
  | "settings";

type SimpleChatStatus = {
  label: string;
  detail: string;
  action?: "setup" | "model-settings";
};

const RUN_EVENT_REFRESH_DEBOUNCE_MS = 250;

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
const desktopCredentialProviders = new Set([
  "openai",
  "openrouter",
  "deepseek",
  "kimi",
  "ollama-cloud",
  "anthropic",
  "grok",
  "gemini"
]);
const providerGroups: Array<ProviderOption["group"]> = ["Local", "Cloud", "Advanced"];
const deterministicModelDefaults: Record<string, string[]> = { mock: ["mock"] };
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
const HASH_ROUTING_ENABLED = typeof navigator === "undefined" || !navigator.userAgent.toLowerCase().includes("jsdom");
const DEFAULT_APP_SECTION: LegacyWorkbenchSection = HASH_ROUTING_ENABLED
  ? "mission"
  : "chat";
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
  "capsule.retention",
  "capsule.retention_failed",
  "memory.compact",
  "memory.compact_failed",
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
  "subagent.failed",
  "routing.selected",
  "routing.attempt_started",
  "routing.shadow_unavailable",
  "routing.guardrail_blocked",
  "routing.assignment_failed",
  "routing.start_failed",
  "routing.outcome_recorded",
  "routing.outcome_failed"
];
const DESKTOP_AUTH_RECOVERY_MESSAGE =
  "The Desktop connection needs to be restored. Retry, or restart Kestrel if its local runtime has stopped.";

type StoredDesktopCredential = {
  status: "stored";
  secretRef: string;
  validation: "unverified" | "valid" | "invalid";
  fingerprint: string;
};

type DesktopCredentialDialogResult =
  | StoredDesktopCredential
  | { status: "cancelled" };

function desktopCredentialDialogResult(
  value: unknown
): DesktopCredentialDialogResult {
  try {
    if (
      typeof value !== "object" ||
      value === null ||
      ![Object.prototype, null].includes(
        Object.getPrototypeOf(value)
      )
    ) {
      throw new Error("invalid");
    }
    const keys = Reflect.ownKeys(value);
    const descriptors = Object.getOwnPropertyDescriptors(value);
    if (
      keys.some((key) => typeof key !== "string") ||
      Object.values(descriptors).some(
        (descriptor) => !("value" in descriptor)
      )
    ) {
      throw new Error("invalid");
    }
    const status = descriptors.status?.value;
    if (
      status === "cancelled" &&
      keys.length === 1 &&
      keys[0] === "status"
    ) {
      return { status };
    }
    const expectedKeys = new Set([
      "status",
      "secretRef",
      "validation",
      "fingerprint"
    ]);
    const secretRef = descriptors.secretRef?.value;
    const validation = descriptors.validation?.value;
    const fingerprint = descriptors.fingerprint?.value;
    if (
      status !== "stored" ||
      keys.length !== expectedKeys.size ||
      keys.some(
        (key) =>
          typeof key !== "string" || !expectedKeys.has(key)
      ) ||
      typeof secretRef !== "string" ||
      secretRef.length > 256 ||
      !/^secret:\/\/[A-Za-z0-9._/-]+$/.test(secretRef) ||
      !["unverified", "valid", "invalid"].includes(
        validation
      ) ||
      typeof fingerprint !== "string" ||
      fingerprint.length < 1 ||
      fingerprint.length > 256
    ) {
      throw new Error("invalid");
    }
    return {
      status,
      secretRef,
      validation,
      fingerprint
    } as StoredDesktopCredential;
  } catch {
    throw new Error("desktop_credential_result_invalid");
  }
}

function BrowserProviderCredentialForm({
  apiKeyEnv,
  providerDisplayName,
  providerRequiresKey,
  providerSecretResult,
  onStored,
  onError
}: {
  apiKeyEnv: string;
  providerDisplayName: string;
  providerRequiresKey: boolean;
  providerSecretResult: SecretRef | null;
  onStored: (result: SecretRef) => Promise<void>;
  onError: (error: unknown) => void;
}) {
  const [credentialValue, setCredentialValue] =
    useState("");

  async function storeCredential() {
    const targetEnv = apiKeyEnv.trim();
    if (!targetEnv || !credentialValue.trim()) return;
    try {
      const result = await postJson<SecretRef>("/api/secrets", {
        name: targetEnv,
        purpose: `Enable ${providerDisplayName} as an LLM provider.`,
        value: credentialValue,
        validate: true
      });
      setCredentialValue("");
      await onStored(result);
    } catch (error) {
      onError(error);
    }
  }

  return (
    <label>
      Provider API key
      <div className="model-picker">
        <input
          className="input mono"
          type="password"
          aria-label="Provider API key"
          value={credentialValue}
          placeholder={
            providerRequiresKey
              ? `Paste ${apiKeyEnv || "provider key"}`
              : "No key needed"
          }
          disabled={!apiKeyEnv.trim()}
          autoComplete="off"
          onChange={(event) =>
            setCredentialValue(event.target.value)}
        />
        <button
          type="button"
          className="btn"
          disabled={
            !apiKeyEnv.trim() || !credentialValue.trim()
          }
          onClick={() => {
            void storeCredential();
          }}
        >
          Store provider key
        </button>
      </div>
      <span className="model-picker-meta">
        {providerRequiresKey
          ? providerSecretResult?.secret_ref ??
            "Stored in secret broker"
          : "No key needed"}
      </span>
    </label>
  );
}

function BrowserSecretMutationForm({
  variant,
  onStored,
  onError
}: {
  variant: "advanced" | "settings";
  onStored: (result: SecretRef) => Promise<void>;
  onError: (error: unknown) => void;
}) {
  const [name, setName] = useState("TELEGRAM_BOT_TOKEN");
  const [purpose, setPurpose] = useState(
    "Enable Telegram channel delivery."
  );
  const [value, setValue] = useState("");
  const [validate, setValidate] = useState(true);

  async function save(event: FormEvent) {
    event.preventDefault();
    if (!name.trim() || !value.trim()) return;
    try {
      const stored = await postJson<SecretRef>("/api/secrets", {
        name,
        purpose,
        value,
        validate
      });
      setValue("");
      await onStored(stored);
    } catch (error) {
      onError(error);
    }
  }

  if (variant === "advanced") {
    return (
      <form onSubmit={save} className="stack-form">
        <div className="field-row">
          <Field label="Secret name">
            <input
              value={name}
              onChange={(event) => setName(event.target.value)}
              autoComplete="off"
            />
          </Field>
          <Field label="Purpose">
            <input
              value={purpose}
              onChange={(event) =>
                setPurpose(event.target.value)}
            />
          </Field>
        </div>
        <Field
          label="Secret value"
          hint="Value is stored by the backend and never returned in API payloads."
        >
          <input
            type="password"
            value={value}
            onChange={(event) => setValue(event.target.value)}
            autoComplete="new-password"
          />
        </Field>
        <label className="check-row">
          <input
            type="checkbox"
            checked={validate}
            onChange={(event) =>
              setValidate(event.target.checked)}
          />
          <span>Validate after save</span>
        </label>
        <button
          type="submit"
          disabled={!name.trim() || !value.trim()}
        >
          <KeyRound size={15} /> Store Secret
        </button>
      </form>
    );
  }

  return (
    <form className="section-row-group" onSubmit={save}>
      <label>
        Secret name
        <input
          className="input mono"
          value={name}
          onChange={(event) => setName(event.target.value)}
          autoComplete="off"
        />
      </label>
      <label>
        Purpose
        <input
          className="input"
          value={purpose}
          onChange={(event) => setPurpose(event.target.value)}
        />
      </label>
      <label>
        Secret value
        <input
          className="input"
          type="password"
          value={value}
          onChange={(event) => setValue(event.target.value)}
          autoComplete="new-password"
        />
      </label>
      <label className="settings-inline-action">
        <span>Broker action</span>
        <button
          className="btn primary"
          type="submit"
          disabled={!name.trim() || !value.trim()}
        >
          Store secret
        </button>
      </label>
    </form>
  );
}

export function LegacyWorkbench({
  requestedSection,
  requestedSubroute,
  onRouteSection,
  onOpenSetup,
  onOpenMission,
}: {
  requestedSection?: LegacyWorkbenchSection;
  requestedSubroute?: string;
  onRouteSection?: (section: LegacyWorkbenchSection) => void;
  onOpenSetup?: () => void;
  onOpenMission?: () => void;
}) {
  const desktopRuntime = isDesktopRuntime();
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [authPromptOpen, setAuthPromptOpen] = useState(false);
  const [apiReady, setApiReady] = useState(false);
  const [apiTokenDraft, setApiTokenDraft] = useState(() => getApiToken());
  const [selfState, setSelfState] = useState<SelfState | null>(null);
  const [onboardingState, setOnboardingState] = useState<SelfOnboardingState | null>(null);
  const [setupReadiness, setSetupReadiness] = useState<SetupReadinessReport | null>(null);
  const [runs, setRuns] = useState<Run[]>([]);
  const [sessions, setSessions] = useState<Session[]>([]);
  const [approvals, setApprovals] = useState<Approval[]>([]);
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
  const activeSectionRef = useRef<LegacyWorkbenchSection>(
    requestedSection ?? DEFAULT_APP_SECTION,
  );
  const threadRunsRef = useRef<Run[]>([]);
  const topbarRef = useRef<HTMLElement | null>(null);
  const conversationRef = useRef<HTMLElement | null>(null);
  const idleRefreshInFlightRef = useRef(false);
  const setupEntryRoutedRef = useRef(false);
  const [activeSection, setActiveSection] =
    useState<LegacyWorkbenchSection>(
      requestedSection ?? DEFAULT_APP_SECTION,
    );

  const handleAuthRequired = useCallback(() => {
    setApiReady(false);
    setAuthPromptOpen(false);
    setApiTokenDraft("");
    if (desktopRuntime) {
      setError(DESKTOP_AUTH_RECOVERY_MESSAGE);
      return;
    }
    setAuthPromptOpen(true);
    setApiTokenDraft(getApiToken());
    setError(null);
  }, [desktopRuntime]);
  const reportError = useCallback(
    (value: unknown) => {
      if (value instanceof ApiAuthError) {
        handleAuthRequired();
        return;
      }
      setError(value instanceof Error ? value.message : String(value));
    },
    [handleAuthRequired],
  );

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
  const settingsWorkspace = useSettingsWorkspace({
    enabled:
      apiReady &&
      (activeSection === "advanced" ||
        (activeSection === "settings" &&
          requestedSubroute !== "setup")),
    includeCapabilities: activeSection === "settings",
    desktopRuntime,
    onError: reportError,
    onNotice: setNotice,
    refreshCore: () =>
      refreshCoreAfterCommittedMutation({
        runId: activeRun?.run_id,
        sessionId: activeRun?.session_id,
      }),
  });
  const {
    runtime,
    runtimeSettingsResult,
    hydrateRuntime,
    workspace,
    setWorkspace,
    provider,
    model,
    setModel,
    baseUrl,
    setBaseUrl,
    apiKeyEnv,
    setApiKeyEnv,
    providerSecretResult,
    temperature,
    setTemperature,
    maxToolRounds,
    setMaxToolRounds,
    modelCatalogs,
    modelCatalogLoading,
    providerCatalog,
    modelSuggestions,
    modelCatalogLabel,
    autonomyMode,
    setAutonomyMode,
    streamResponses,
    setStreamResponses,
    memoryBackendDraft,
    setMemoryBackendDraft,
    apiAuthRequired,
    setApiAuthRequired,
    toolPermissions,
    setToolPermissions,
    channels,
    secrets,
    channelId,
    setChannelId,
    channelProvider,
    setChannelProvider,
    channelTokenEnv,
    setChannelTokenEnv,
    channelWebhookEnv,
    setChannelWebhookEnv,
    channelEnabled,
    setChannelEnabled,
    channelSendEnabled,
    setChannelSendEnabled,
    channelAutoReply,
    setChannelAutoReply,
    channelSettings,
    setChannelSettings,
    channelPayload,
    setChannelPayload,
    channelResult,
    telegramWebhookUrl,
    setTelegramWebhookUrl,
    telegramActionResult,
    secretResult,
    chooseProvider,
    refreshProviderModels,
    saveRuntimeSettings,
    storeDesktopProviderKey,
    loadChannel,
    saveChannel,
    deleteChannel,
    ingestChannel,
    telegramWebhookInfo,
    telegramSetWebhook,
    telegramDeleteWebhook,
    acceptBrowserSecret,
    acceptBrowserProviderSecret,
    validateSecret,
    deleteSecret,
  } = settingsWorkspace;
  const extendWorkspace = useExtendWorkspace({
    enabled: apiReady && activeSection === "advanced",
    activeRun,
    activeSessionId,
    workspace,
    toolPermissions,
    enqueueRun,
    onError: reportError,
    onNotice: setNotice,
    refreshCore: () =>
      refreshCoreAfterCommittedMutation({
        runId: activeRun?.run_id,
        sessionId: activeRun?.session_id,
      }),
    refreshRunDetails,
    refreshAll,
    createSessionId: createThreadId,
  });
  const {
    operatorMessage,
    setOperatorMessage,
    sessionId,
    setSessionId,
    subagentProfile,
    setSubagentProfile,
    subagentGoal,
    setSubagentGoal,
    schedulerTasks,
    setSchedulerTasks,
    schedulerCycles,
    setSchedulerCycles,
    schedulerResult,
    selfTitle,
    setSelfTitle,
    selfContent,
    setSelfContent,
    selfSchema,
    setSelfSchema,
    selfRememberResult,
    webQuery,
    setWebQuery,
    webResult,
    submitRun,
    runScheduler,
    submitSubagent,
    rememberSelf,
    searchWeb,
    allApprovals,
    mcpServers,
    skills,
    plugins,
    toolName,
    setToolName,
    toolArgs,
    setToolArgs,
    preparedToolPreview,
    setPreparedToolPreview,
    toolResult,
    toolFilter,
    setToolFilter,
    toolSourceFilter,
    setToolSourceFilter,
    toolRiskFilter,
    setToolRiskFilter,
    toolEnabledFilter,
    setToolEnabledFilter,
    mcpId,
    setMcpId,
    mcpName,
    setMcpName,
    mcpTransport,
    setMcpTransport,
    mcpEndpoint,
    setMcpEndpoint,
    mcpArgs,
    setMcpArgs,
    mcpEnv,
    setMcpEnv,
    mcpSecretEnv,
    setMcpSecretEnv,
    mcpRiskPolicy,
    setMcpRiskPolicy,
    mcpArgsTouched,
    setMcpArgsTouched,
    mcpEnvTouched,
    setMcpEnvTouched,
    mcpSecretEnvTouched,
    setMcpSecretEnvTouched,
    mcpToolSelection,
    setMcpToolSelection,
    mcpToolArgs,
    setMcpToolArgs,
    mcpResult,
    skillTask,
    setSkillTask,
    skillSelection,
    setSkillSelection,
    skillManifest,
    setSkillManifest,
    skillInstructions,
    setSkillInstructions,
    skillResult,
    skillDiscovery,
    skillDiscovering,
    pluginSource,
    setPluginSource,
    pluginRef,
    setPluginRef,
    pluginEnable,
    setPluginEnable,
    pluginResult,
    pluginReview,
    pluginUpdateReviews,
    mcpToolOptions,
    enabledSkills,
    selectedTool,
    selectedToolEnabled,
    selectedMcpToolEnabled,
    selectedSkillEnabled,
    loadedMcpServer,
    filteredTools,
    toolSources,
    toolRisks,
    reviewedCurrentPlugin,
    pluginEnableBlockers,
    invokeTool,
    loadMcp,
    saveMcp,
    controlMcp,
    deleteMcp,
    invokeMcp,
    toggleSkill,
    discoverSkills,
    installSkill,
    runSkill,
    reviewPlugin,
    installPlugin,
    pluginAction,
  } = extendWorkspace;
  const capabilityWorkspace =
    activeSection === "settings" ? settingsWorkspace : extendWorkspace;
  const {
    tools,
    capabilitySnapshot,
    capabilityPending,
    capabilitySearch,
    setCapabilitySearch,
    capabilityKindFilter,
    setCapabilityKindFilter,
    capabilityStateFilter,
    setCapabilityStateFilter,
    capabilities,
    filteredCapabilities,
    enabledToolCount,
    setCapabilityEnabled,
  } = capabilityWorkspace;
  const memoryWorkspace = useMemoryWorkspace({
    enabled:
      apiReady &&
      (activeSection === "memory" ||
        activeSection === "advanced" ||
        activeSection === "settings"),
    activeRunId: activeRun?.run_id ?? null,
    onAuthRequired: handleAuthRequired,
    onError: setError,
    onNotice: setNotice,
  });
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
  const streamedAssistant = useMemo(
    () =>
      events
        .filter((event) => event.type === "assistant.token")
        .map((event) => String(event.payload.content ?? ""))
        .join(""),
    [events]
  );
  const proofOfWork = useMemo(() => extractProofOfWork(runTrace), [runTrace]);
  const activeThread = useMemo(
    () => threadSummaries.find((thread) => thread.session_id === activeSessionId) ?? null,
    [threadSummaries, activeSessionId]
  );
  function routeToSection(section: LegacyWorkbenchSection) {
    setNotice(null);
    setError(null);
    setActiveSection(section);
    if (onRouteSection) {
      onRouteSection(section);
      return;
    }
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

  function openSetupCenter() {
    setNotice(null);
    setError(null);
    setActiveSection("settings");
    if (onOpenSetup) {
      onOpenSetup();
      return;
    }
    window.location.hash = "#/settings/setup";
  }

  function openSettingsSection(anchor?: string) {
    routeToSection("settings");
    if (anchor) {
      window.setTimeout(() => scrollToElement(anchor), 0);
    }
  }

  function openMissionCommand() {
    if (onOpenMission) {
      onOpenMission();
      return;
    }
    routeToSection("mission");
  }

  function selectSessionId(sessionId: string | null) {
    activeSessionIdRef.current = sessionId;
    setActiveSessionId(sessionId);
  }

  function selectRunId(runId: string | null) {
    activeRunIdRef.current = runId;
    setActiveRunId(runId);
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
    if (requestedSection) setActiveSection(requestedSection);
  }, [requestedSection]);

  useEffect(() => {
    threadRunsRef.current = threadRuns;
  }, [threadRuns]);

  useEffect(() => {
    const topbar = topbarRef.current;
    if (!topbar) return;
    const syncTopbarHeight = () => {
      const height = Math.ceil(topbar.getBoundingClientRect().height);
      if (height > 0) {
        document.documentElement.style.setProperty("--kestrel-topbar-height", `${height}px`);
      }
    };
    syncTopbarHeight();
    window.addEventListener("resize", syncTopbarHeight);
    const observer = typeof ResizeObserver === "undefined" ? null : new ResizeObserver(syncTopbarHeight);
    observer?.observe(topbar);
    return () => {
      observer?.disconnect();
      window.removeEventListener("resize", syncTopbarHeight);
      document.documentElement.style.removeProperty("--kestrel-topbar-height");
    };
  }, []);

  useEffect(() => {
    if (conversationRef.current) conversationRef.current.scrollTop = 0;
    const applicationWorkspace = document.getElementById("workspace");
    if (applicationWorkspace) applicationWorkspace.scrollTop = 0;
  }, [activeSection]);

  useEffect(() => {
    if (notice !== "Run queued." || !activeRun) return;
    if (activeRun.status !== "queued" && activeRun.status !== "running") {
      setNotice(null);
    }
  }, [notice, activeRun?.status]);

  useEffect(() => {
    if (!onboardingState) return;
    if (
      onboardingState.completed ||
      setupEntryRoutedRef.current ||
      hasVisitedSetupCenter()
    ) {
      return;
    }
    setupEntryRoutedRef.current = true;
    if (
      requestedSection === "settings" &&
      requestedSubroute === "setup"
    ) {
      return;
    }
    openSetupCenter();
  }, [
    onboardingState,
    requestedSection,
    requestedSubroute,
    onOpenSetup,
  ]);

  useEffect(() => {
    let cancelled = false;
    getJson<Record<string, unknown>>("/api/health")
      .then(() => {
        if (!cancelled) setApiReady(true);
      })
      .catch(reportError);
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    if (!apiReady) return;
    refreshAll().catch(reportError);
    const timer = window.setInterval(() => refreshIdleSummary().catch(reportError), 3500);
    return () => window.clearInterval(timer);
  }, [apiReady]);

  useEffect(() => {
    if (!apiReady) return;
    void refreshProviderModels(provider);
  }, [provider, apiReady]);

  useEffect(() => {
    if (
      !HASH_ROUTING_ENABLED ||
      requestedSection !== undefined ||
      onRouteSection !== undefined
    ) {
      return;
    }
    const syncRoute = () => {
      const next = sectionFromHash(window.location.hash);
      if (next) setActiveSection(next);
    };
    syncRoute();
    window.addEventListener("hashchange", syncRoute);
    return () => window.removeEventListener("hashchange", syncRoute);
  }, [onRouteSection, requestedSection]);

  useEffect(() => {
    if (!activeSessionId) {
      setThreadRuns([]);
      return;
    }
    refreshThreadRuns(activeSessionId).catch(reportError);
  }, [activeSessionId]);

  useEffect(() => {
    if (!activeRun?.run_id) return;
    const runId = activeRun.run_id;
    const sessionId = activeRun.session_id;
    let refreshTimer: number | null = null;
    let closed = false;
    setEvents([]);
    refreshRunDetails(runId).catch(reportError);
    const scheduleAuthoritativeRefresh = () => {
      if (refreshTimer !== null) window.clearTimeout(refreshTimer);
      refreshTimer = window.setTimeout(() => {
        refreshTimer = null;
        if (closed) return;
        void Promise.all([
          refreshChatSummary(sessionId),
          refreshRunDetails(runId)
        ]).catch(reportError);
      }, RUN_EVENT_REFRESH_DEBOUNCE_MS);
    };
    const appendEvent = (parsed: TraceEvent) => {
      setEvents((rows) => [...rows.slice(-120), parsed]);
      if (parsed.type !== "assistant.token") {
        scheduleAuthoritativeRefresh();
      }
    };
    const unsubscribe = subscribeJsonEvents<TraceEvent>(`/api/runs/${runId}/events`, runEventTypes, appendEvent, reportError);
    return () => {
      closed = true;
      if (refreshTimer !== null) window.clearTimeout(refreshTimer);
      unsubscribe();
    };
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

  async function refreshChatSummary(forceThreadSessionId?: string) {
    const [runList, sessionList, pendingApprovalList] = await Promise.all([
      getJson<Run[]>("/api/runs"),
      getJson<Session[]>("/api/sessions"),
      getJson<Approval[]>("/api/approvals?status=pending")
    ]);
    setRuns(runList);
    setSessions(sessionList);
    setApprovals(pendingApprovalList);
    applyRunSessionSelection(runList, sessionList, pendingApprovalList);
    if (forceThreadSessionId && activeSessionIdRef.current === forceThreadSessionId) {
      await refreshThreadRuns(forceThreadSessionId);
    } else {
      await refreshSelectedThreadIfChanged(sessionList);
    }
  }

  async function refreshSettingsInventory() {
    await settingsWorkspace.refreshInventory();
  }

  async function refreshOperatorInventory() {
    const refreshes: Array<Promise<void>> = [
      refreshSettingsInventory(),
    ];
    if (activeSectionRef.current === "advanced") {
      refreshes.push(extendWorkspace.refresh());
    }
    await Promise.all(refreshes);
  }

  async function refreshOperatorSummary() {
    await Promise.all([
      refreshChatSummary(),
      refreshOperatorInventory(),
    ]);
  }

  async function refreshSelectedThreadIfChanged(sessionList: Session[]) {
    const selectedSessionId = activeSessionIdRef.current;
    if (!selectedSessionId) return;
    const summary = sessionList.find((session) => session.session_id === selectedSessionId);
    if (!summary) return;
    const knownRuns = threadRunsRef.current;
    const knownLatest = knownRuns[knownRuns.length - 1];
    const changed =
      summary.run_count !== knownRuns.length ||
      summary.latest_run_id !== knownLatest?.run_id ||
      summary.latest_status !== knownLatest?.status ||
      summary.updated_at !== knownLatest?.updated_at;
    if (changed) await refreshThreadRuns(selectedSessionId);
  }

  async function refreshIdleSummary() {
    if (idleRefreshInFlightRef.current) return;
    idleRefreshInFlightRef.current = true;
    try {
      if (
        activeSectionRef.current === "advanced" ||
        activeSectionRef.current === "settings"
      ) {
        await refreshOperatorSummary();
      } else {
        await refreshChatSummary();
      }
    } finally {
      idleRefreshInFlightRef.current = false;
    }
  }

  async function refreshAll() {
    await refreshChatSummary();
    const [
      runtimeConfig,
      selfSnapshot,
      onboardingSnapshot,
      setupReadinessReport,
      logList
    ] = await Promise.all([
      getJson<RuntimeConfig>("/api/runtime/config"),
      getJson<SelfState>("/api/self"),
      getJson<SelfOnboardingState>("/api/self/onboarding"),
      getJson<SetupReadinessReport>("/api/product/setup").catch((error) => {
        reportError(error);
        return null;
      }),
      getJson<AgentLogEvent[]>("/api/logs?limit=120")
    ]);
    hydrateRuntime(runtimeConfig);
    setSelfState(selfSnapshot);
    setOnboardingState(onboardingSnapshot);
    setSetupReadiness(setupReadinessReport);
    setLogs(logList);
  }

  async function refreshOperatorWorkspace() {
    await refreshAll();
    await Promise.all([
      refreshOperatorInventory(),
      memoryWorkspace.refresh(),
    ]);
  }

  async function refreshAfterCommittedMutation(
    refreshes: Array<() => Promise<void>>,
  ) {
    const results = await Promise.allSettled(
      refreshes.map((refresh) => refresh()),
    );
    const failures = results
      .filter(
        (result): result is PromiseRejectedResult =>
          result.status === "rejected",
      )
      .map((result) => result.reason);
    if (failures.length === 0) return;
    const authFailure = failures.find((value) => value instanceof ApiAuthError);
    if (authFailure) {
      handleAuthRequired();
      return;
    }
    setError(
      `The change was committed, but part of the refreshed view is unavailable: ${failures
        .map((value) => (value instanceof Error ? value.message : String(value)))
        .join("; ")}`,
    );
  }

  async function refreshCoreAfterCommittedMutation({
    runId,
    sessionId,
  }: {
    runId?: string | null;
    sessionId?: string | null;
  } = {}) {
    const refreshes: Array<() => Promise<void>> = [
      () => refreshChatSummary(sessionId ?? undefined),
    ];
    if (runId) {
      refreshes.push(() => refreshRunDetails(runId));
    }
    await refreshAfterCommittedMutation(refreshes);
  }

  async function refreshThreadRuns(sessionId: string) {
    const runList = await getJson<Run[]>(`/api/sessions/${encodeURIComponent(sessionId)}/runs`);
    if (activeSessionIdRef.current === sessionId) {
      threadRunsRef.current = runList;
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

  async function saveToken(event: FormEvent) {
    event.preventDefault();
    setApiToken(apiTokenDraft);
    setAuthPromptOpen(false);
    setError(null);
    try {
      await getJson<Record<string, unknown>>("/api/health");
      if (apiReady) {
        await refreshAll();
      } else {
        setApiReady(true);
      }
    } catch (value) {
      reportError(value);
    }
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

  async function submitConversationMessage(objective: string) {
    await enqueueRun({
      objective,
      sessionId:
        activeSessionIdRef.current ||
        createThreadId(),
      workspace: workspace.trim() || null,
    });
    setNotice("Run queued.");
  }

  async function launchMission(mission: MissionLaunch) {
    await enqueueRun({
      objective: mission.objective,
      sessionId: createThreadId(),
      workspace: mission.project.repository_path,
      projectId: mission.project.project_id,
      missionPlan: mission.plan,
      projectRevision: mission.preflight.project_revision,
      missionTemplateId: mission.templateId,
      missionBinding: mission.preflight.launch_binding
    });
    setNotice("Mission queued.");
  }

  async function enqueueRun({
    objective,
    sessionId: targetSessionId,
    workspace: targetWorkspace,
    projectId,
    missionPlan,
    projectRevision,
    missionTemplateId,
    missionBinding
  }: {
    objective: string;
    sessionId: string;
    workspace: string | null;
    projectId?: string;
    missionPlan?: MissionLaunch["plan"];
    projectRevision?: number;
    missionTemplateId?: string;
    missionBinding?: MissionLaunch["preflight"]["launch_binding"];
  }) {
    if (!objective.trim() || !runtime) return;
    const payload: Record<string, unknown> = {
      message: objective.trim(),
      session_id: targetSessionId,
      autonomy_mode: submissionAutonomyMode(autonomyMode)
    };
    if (targetWorkspace) payload.workspace = targetWorkspace;
    if (projectId) payload.project_id = projectId;
    if (missionPlan) payload.mission_plan = missionPlan;
    if (projectRevision) payload.project_revision = projectRevision;
    if (missionTemplateId) payload.mission_template_id = missionTemplateId;
    if (missionBinding) payload.mission_binding = missionBinding;
    const runtimeProvider = String((runtime as RuntimeConfig | null)?.provider?.name ?? "");
    const runtimeModel = String((runtime as RuntimeConfig | null)?.provider?.model ?? "");
    if (!missionPlan && provider.trim() && provider.trim() !== runtimeProvider) {
      payload.provider = provider.trim();
    }
    if (!missionPlan && model.trim() && model.trim() !== runtimeModel) {
      payload.model = model.trim();
    }
    const run = await postJson<Run>("/api/runs", payload);
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
    await refreshCoreAfterCommittedMutation({
      runId: run.run_id,
      sessionId: run.session_id,
    });
  }

  function createNewThread() {
    const threadId = createThreadId();
    const now = new Date().toISOString();
    selectSessionId(threadId);
    selectRunId(null);
    threadRunsRef.current = [];
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
      const updated = await postJson<Approval>(`/api/approvals/${approval.approval_id}/decision`, {
        approved,
        arguments: approval.arguments
      });
      await refreshCoreAfterCommittedMutation({
        runId: activeRun?.run_id,
        sessionId: activeRun?.session_id,
      });
      const returnedStatus =
        updated && typeof updated === "object" && "status" in updated
          ? String(updated.status)
          : null;
      if (approved && returnedStatus !== "approved") {
        setNotice(
          returnedStatus === "expired"
            ? "Approval expired before the decision was recorded."
            : returnedStatus
              ? `Approval decision returned status: ${returnedStatus}.`
              : "Approval decision did not confirm an approved status.",
        );
        return;
      }
      if (!approved && returnedStatus && returnedStatus !== "denied") {
        setNotice(`Approval decision returned status: ${returnedStatus}.`);
        return;
      }
      setNotice(approved ? "Approval accepted." : "Approval denied.");
    });
  }

  async function approveTask(task: TaskNode) {
    if (!activeRun) return;
    await guarded(async () => {
      await postJson(`/api/runs/${activeRun.run_id}/approve-task`, { task_id: task.task_id });
      await refreshCoreAfterCommittedMutation({
        runId: activeRun.run_id,
        sessionId: activeRun.session_id,
      });
    }, "Task approved.");
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

  const runtimeConfig = runtime as RuntimeConfig | null;
  const runtimeProvider = runtimeConfig?.provider ?? {};
  const runtimeLimits = runtimeConfig?.limits ?? {};
  const runtimePaths = runtimeConfig?.paths ?? {};
  const featureFlags = runtimeConfig?.feature_flags ?? {};
  const selectedProviderOption = providerOptionMap[provider] ?? null;
  const providerDisplayName = selectedProviderOption?.label ?? provider;
  const canonicalDesktopApiKeyEnv =
    selectedProviderOption?.apiKeyEnv ?? "";
  const effectiveApiKeyEnv = desktopRuntime
    ? canonicalDesktopApiKeyEnv
    : apiKeyEnv;
  const selectedProviderCatalog = providerCatalog?.provider === provider ? providerCatalog : null;
  const providerRequiresKey = Boolean(
    selectedProviderOption?.requiresKey ||
      effectiveApiKeyEnv.trim()
  );
  const providerKeyConfigured =
    selectedProviderCatalog?.api_key_configured ??
    (String(runtimeProvider.name ?? "") === provider ? Boolean(runtimeProvider.api_key_configured) : false);
  const providerKeyStatus = providerRequiresKey ? (providerKeyConfigured ? "configured" : "missing") : "not needed";
  const desktopCredentialStorageHint =
    setupReadiness?.credential_storage?.state ===
    "session_only"
      ? "Credentials are stored for this session only."
      : setupReadiness?.credential_storage?.state ===
          "available"
        ? "Credentials use persistent platform storage."
        : "Credential storage needs attention.";
  const activeDeltaCount = memoryWorkspace.activeDeltaCount;
  const totalDeltaCount = memoryWorkspace.totalDeltaCount;
  const pendingApprovalCount = approvals.filter((approval) => approval.status === "pending").length;
  const oracleShadowLabel = `${events.filter((event) => event.type.includes("oracle") || event.type.includes("routing")).length} observations`;
  const onboardingProfile = onboardingState?.profile ?? null;
  const agentDisplayName = String(onboardingProfile?.agent_name || selfState?.identity?.name || "Kestrel");
  const userDisplayName = String(onboardingProfile?.preferred_name || onboardingProfile?.user_name || "");
  const browserAuthPromptOpen = authPromptOpen && !desktopRuntime;
  const simpleStatus: SimpleChatStatus = browserAuthPromptOpen
    ? {
        label: "Locked",
        detail: "Enter the local API token before using this Kestrel."
      }
    : !apiReady || !runtime
      ? {
          label: "Connecting",
          detail: "Loading the authoritative Kestrel runtime configuration."
        }
    : simpleChatStatus(
        activeRun,
        pendingApprovalCount,
        setupReadiness,
        providerDisplayName,
        model
      );
  const chatIntro = userDisplayName
    ? `Ready when you are, ${userDisplayName}.`
    : "Ready when you are.";
  const chatStatusDetail =
    activeRun || simpleStatus.label !== "Ready"
      ? simpleStatus.detail
      : chatIntro;
  const renderConversationInspector = (onClose: () => void) => (
    <aside className="inspector" aria-label="Run details">
      <div className="inspector-head">
        <h2>Run details</h2>
        <button type="button" aria-label="Close panel" onClick={onClose}>
          <X size={15} />
        </button>
      </div>
      {activeRun ? (
        <>
          <section>
            <h3>Current run</h3>
            <StatusBadge value={activeRun.status} />
            <InlineMeta
              items={[
                activeRun.run_id,
                activeRun.session_id,
                activeRun.model,
              ]}
            />
            {activeRun.error && (
              <p className="danger-text">{activeRun.error}</p>
            )}
          </section>
          <section>
            <h3>Plan</h3>
            <TaskList
              title="Needs You"
              tasks={taskGraph?.approval_blocked_tasks ?? []}
              onApprove={approveTask}
            />
            <TaskList
              title="Ready"
              tasks={taskGraph?.ready_tasks ?? []}
              onApprove={approveTask}
            />
          </section>
          {proofOfWork && (
            <section>
              <h3>Validation</h3>
              <SummaryList
                title="Completed"
                values={asStringArray(proofOfWork.completed_steps)}
              />
              <SummaryList
                title="Evidence"
                values={asStringArray(proofOfWork.validation_evidence)}
              />
              <SummaryList
                title="Risks"
                values={asStringArray(proofOfWork.remaining_risks)}
              />
            </section>
          )}
          <section>
            <h3>Activity</h3>
            <div className="trace-list compact-trace">
              {(runTrace?.timeline ?? events).slice(-12).map((event) => (
                <div
                  className="trace-row"
                  key={`${event.id}-${event.type}`}
                >
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
  );

  return (
    <>
      <header className="topbar" ref={topbarRef}>
        <div className="topbar-inner">
          <a
            className="brand"
            href="#workspace"
            onClick={(event) => {
              event.preventDefault();
              routeToSection("mission");
            }}
          >
            <span className="brand-mark" aria-hidden="true">
              <Feather size={22} />
            </span>
            <span>
              <span className="brand-name">{agentDisplayName}</span>
              <span className="brand-tag">{onboardingProfile?.persona_name ?? "Local-first agent"}</span>
            </span>
          </a>
          <nav className="primary-nav" aria-label="Primary">
            <button type="button" className={activeSection === "mission" ? "active" : ""} onClick={() => routeToSection("mission")}>Workbench</button>
            <button type="button" className={activeSection === "chat" ? "active" : ""} onClick={() => routeToSection("chat")}>History</button>
            <button type="button" className={activeSection === "outcomes" ? "active" : ""} onClick={() => routeToSection("outcomes")}>Outcomes</button>
            <button type="button" className={activeSection === "advanced" ? "active" : ""} onClick={() => routeToSection("advanced")}>Advanced</button>
          </nav>
          <div className="topbar-meta">
            <button type="button" className="setup-button" onClick={openSetupCenter}>
              <Sparkles size={14} /> Setup
            </button>
            <span className="status-pill"><span className="status-dot"></span>{simpleStatus.label}</span>
          </div>
        </div>
      </header>
      {browserAuthPromptOpen ? (
        <div className="conversation">
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
        </div>
      ) : !apiReady || !runtime ? (
        <div className="conversation">
          <section className="settings-grid" aria-label="Kestrel connection status">
            <Panel title={`Connecting to ${agentDisplayName}`} icon={<Activity size={19} />}>
              <p>{error || "Loading the authoritative runtime configuration before enabling the workbench."}</p>
              {error && (
                <button
                  type="button"
                  onClick={() => {
                    setError(null);
                    getJson<Record<string, unknown>>("/api/health")
                      .then(() => {
                        setApiReady(true);
                        return refreshAll();
                      })
                      .catch(reportError);
                  }}
                >
                  <RefreshCw size={15} /> Retry
                </button>
              )}
            </Panel>
          </section>
        </div>
      ) : activeSection === "mission" ? (
        <ProjectsWorkspace
          runs={runs}
          activeRun={activeRun}
          taskGraph={taskGraph}
          approvals={approvals}
          events={activeRunEvents}
          onLaunch={launchMission}
          onOpenRun={(run) => {
            void selectRun(run.run_id);
          }}
          onOpenHistory={() => routeToSection("chat")}
          onOpenAdvanced={() => routeToSection("advanced")}
          onOpenDiagnostics={() => jumpToAdvanced("observability")}
          onPrepareTool={(name, args) => {
            setToolName(name);
            setToolArgs(JSON.stringify(args, null, 2));
            setPreparedToolPreview({ name, args });
            jumpToAdvanced("tools");
          }}
          onDecideApproval={decideApproval}
          onContinueConversation={submitConversationMessage}
          onAuthRequired={handleAuthRequired}
        />
      ) : activeSection === "outcomes" ? (
        <OutcomesDashboard onBack={() => routeToSection("mission")} />
      ) : (
      <div className="chat-shell" data-active-section={activeSection}>
      <a
        className="skip-link"
        href="#workspace"
        onClick={(event) => {
          event.preventDefault();
          const workspaceElement = document.getElementById("workspace");
          workspaceElement?.focus();
          if (typeof workspaceElement?.scrollIntoView === "function") {
            workspaceElement.scrollIntoView({
              block: "start",
              behavior: "smooth",
            });
          }
        }}
      >
        Skip to workspace
      </a>
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
        <div className="thread-list" role="region" aria-label="Conversation threads">
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

      {activeSection === "chat" ? (
        <ConversationPanel
          agentDisplayName={agentDisplayName}
          hasActiveThread={Boolean(activeThread)}
          chatStatusDetail={chatStatusDetail}
          status={simpleStatus}
          activeRun={activeRun}
          runs={sortedThreadRuns}
          events={activeRunEvents}
          streamedAssistant={streamedAssistant}
          approvals={activeApprovals}
          autonomyMode={autonomyMode}
          autonomyOptions={autonomyOptions}
          autonomousSchedulerEnabled={Boolean(
            (runtime as RuntimeConfig | null)?.feature_flags
              ?.enable_autonomous_scheduler,
          )}
          notice={notice}
          error={error}
          onAutonomyModeChange={setAutonomyMode}
          onOpenSetup={openSetupCenter}
          onOpenSettings={() => routeToSection("settings")}
          onRefresh={refreshAll}
          onSubmitMessage={submitConversationMessage}
          onError={reportError}
          onDismissError={() => setError(null)}
          onDecideApproval={decideApproval}
          onContainer={(element) => {
            conversationRef.current = element;
          }}
          renderInspector={renderConversationInspector}
        />
      ) : (
      <div
        className="conversation"
        id="legacy-workspace"
        ref={(element) => {
          conversationRef.current = element;
        }}
      >
        {activeSection === "memory" && (
          <section
            className="shell page-shell advanced-page"
            data-section="memory"
            aria-label="Memory workspace"
          >
            <header className="page-head">
              <div>
                <p className="page-eyebrow">Nested learning</p>
                <h1 className="page-title">Memory<em>.</em></h1>
                <p className="page-subtitle">
                  Inspect layer health, bounded recall, evidence packing, and
                  gated learning without granting policy authority.
                </p>
              </div>
              <div className="page-actions">
                <button
                  className="btn subtle"
                  type="button"
                  onClick={() => memoryWorkspace.refresh()}
                >
                  <RefreshCw size={15} /> Refresh
                </button>
              </div>
            </header>
            {error && (
              <ActionError message={error} onDismiss={() => setError(null)} />
            )}
            <MemoryWorkspace controller={memoryWorkspace} />
          </section>
        )}
        {activeSection === "routines" && (
          <AutomateWorkspace
            onAuthRequired={handleAuthRequired}
            channelsSlice={{
              channels,
              onEditChannel: (channel) => {
                loadChannel(channel);
                jumpToAdvanced("channels");
              }
            }}
          />
        )}
        {activeSection === "routing" && (
          <section
            id="routing-workbench"
            className="shell page-shell advanced-page"
            ref={(element) => {
              conversationRef.current = element;
            }}
            tabIndex={0}
            aria-label="Adaptive Flock routing workbench"
          >
            <header className="page-header advanced-header">
              <div>
                <span className="eyebrow">Adaptive execution</span>
                <h1>Adaptive Flock Routing</h1>
                <p>Configure provider pools, inspect route policies, and preview why Kestrel selects a worker.</p>
              </div>
              <button type="button" className="secondary-button" onClick={() => routeToSection("chat")}>
                Back to chat
              </button>
            </header>
            {error && <div className="banner error">{error}</div>}
            {notice && <div className="banner success">{notice}</div>}
            <FlockWorkspace
              subroute={requestedSubroute ?? "routing"}
              activeRunId={activeRun?.run_id ?? null}
              activeTaskId={
                taskGraph?.tasks.find((task) => ["running", "blocked", "pending"].includes(task.status))?.task_id ??
                null
              }
              onError={setError}
              onNotice={setNotice}
            />
          </section>
        )}
        {activeSection === "advanced" && (
          <ExtendWorkspace
            controller={extendWorkspace}
            error={error}
            onDismissError={() => setError(null)}
            onNavigate={routeToSection}
            onRefresh={refreshOperatorWorkspace}
          >
            {{
              capabilities: (
                <>
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
                <button
                  className="tag ghost"
                  type="button"
                  key={id}
                  onClick={() =>
                    id === "memory"
                      ? routeToSection("memory")
                      : scrollToElement(id)
                  }
                >
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
                <textarea value={operatorMessage} onChange={(event) => setOperatorMessage(event.target.value)} rows={5} />
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
                <button type="submit" disabled={!operatorMessage.trim()}>
                  <Send size={15} /> Queue Run
                </button>
                {activeRun?.status === "running" && (
                  <button
                    type="button"
                    className="btn danger"
                    onClick={() => guarded(async () => {
                      await postJson(`/api/runs/${activeRun.run_id}/cancel`);
                      await refreshCoreAfterCommittedMutation({
                        runId: activeRun.run_id,
                        sessionId: activeRun.session_id,
                      });
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
            <RepairReviewPanel
              tasks={taskGraph?.tasks ?? []}
              allowedPaths={[]}
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

        <section id="tools" className="section">
          <Panel id="connected-tools" title="Connected Tools" icon={<Wrench size={19} />}>
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
            {desktopRuntime ? (
              <EmptyState>
                Provider credentials are added from Provider
                settings through the isolated Desktop dialog.
              </EmptyState>
            ) : (
              <BrowserSecretMutationForm
                variant="advanced"
                onStored={acceptBrowserSecret}
                onError={reportError}
              />
            )}
            <div className="list separated">
              {secrets.length === 0 ? (
                <EmptyState>No brokered secrets configured.</EmptyState>
              ) : (
                secrets.map((secret) => (
                  <div className="data-row" key={secret.id}>
                    <strong>{secret.name}</strong>
                    <InlineMeta items={[secret.secret_ref, secret.configured ? "configured" : "missing", secret.validated ? "validated" : "unvalidated"]} />
                    {secret.purpose && <p>{secret.purpose}</p>}
                    {!desktopRuntime && (
                      <div className="page-actions">
                        <button type="button" onClick={() => validateSecret(secret)}>Validate</button>
                        <button type="button" className="btn danger" onClick={() => deleteSecret(secret)}>Delete</button>
                      </div>
                    )}
                  </div>
                ))
              )}
            </div>
            {secretResult && <JsonBlock value={secretResult} maxHeight="220px" />}
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
                </>
              )
            }}
      </ExtendWorkspace>
      )}
        {activeSection === "settings" && (
          <SettingsWorkspace
            controller={settingsWorkspace}
            error={error}
            notice={notice}
            onDismissError={() => setError(null)}
            onOpenAdvanced={() => jumpToAdvanced("runtime")}
            onOpenSetup={openSetupCenter}
            onRefresh={refreshOperatorWorkspace}
            subroute={requestedSubroute}
            setupCenter={
              <SetupCenter
                navigation={{
                  openGeneralSettings: () => openSettingsSection(),
                  openProviderSettings: () =>
                    openSettingsSection("provider"),
                  openSafetySettings: () =>
                    openSettingsSection("permissions"),
                  openCapabilitiesSettings: () =>
                    openSettingsSection("capabilities"),
                  openMemorySettings: () =>
                    openSettingsSection("memory-storage-recovery"),
                  openApiAccessSettings: () =>
                    openSettingsSection("api-access"),
                  openMission: openMissionCommand,
                }}
              />
            }
          >

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
                      value={effectiveApiKeyEnv}
                      placeholder={providerRequiresKey ? "API_KEY_ENV" : "not required"}
                      readOnly={desktopRuntime}
                      onChange={(event) => {
                        if (!desktopRuntime) {
                          setApiKeyEnv(event.target.value);
                        }
                      }}
                    />
                  </label>
                  {desktopRuntime ? (
                    <div>
                      <span>Provider credential</span>
                      <div className="model-picker">
                        <button
                          type="button"
                          className="btn"
                          disabled={
                            !desktopCredentialProviders.has(
                              provider
                            )
                          }
                          onClick={() => {
                            void storeDesktopProviderKey();
                          }}
                        >
                          Store provider key
                        </button>
                      </div>
                      <span className="model-picker-meta">
                        {desktopCredentialProviders.has(provider)
                          ? providerSecretResult?.secret_ref ??
                            desktopCredentialStorageHint
                          : "Credential entry is unavailable for this provider"}
                      </span>
                    </div>
                  ) : (
                    <BrowserProviderCredentialForm
                      key={`${provider}:${effectiveApiKeyEnv}`}
                      apiKeyEnv={effectiveApiKeyEnv}
                      providerDisplayName={providerDisplayName}
                      providerRequiresKey={providerRequiresKey}
                      providerSecretResult={providerSecretResult}
                      onStored={acceptBrowserProviderSecret}
                      onError={reportError}
                    />
                  )}
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
                {desktopRuntime && (
                  <div className="row">
                    <div className="row-label">
                      <strong>Credential storage</strong>
                      <p>
                        {setupReadiness?.credential_storage
                          ?.remediation ??
                          "Credential storage readiness is unavailable. Refresh Settings or restart Kestrel."}
                      </p>
                    </div>
                    <div className="row-control">
                      <StatusBadge
                        value={
                          setupReadiness?.credential_storage
                            ?.state ?? "unavailable"
                        }
                      />
                      <InlineMeta
                        items={[
                          setupReadiness?.credential_storage
                            ?.backend,
                          setupReadiness?.credential_storage
                            ?.persistence
                        ]}
                      />
                    </div>
                  </div>
                )}
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
                {desktopRuntime && (
                  <div className="row stacked">
                    <Notice
                      id="memory-storage-recovery"
                      className="settings-storage-recovery"
                      tabIndex={-1}
                      variant="caution"
                      title="Launch-controlled storage recovery"
                    >
                      <p>
                        Kestrel Desktop selected the active memory writer
                        path before starting the local API. This page can
                        inspect that authority, but cannot move live memory
                        or replace it while the writer is running.
                      </p>
                      <p>
                        <strong>Current launch-owned path:</strong>{" "}
                        <code className="mono">
                          {String(runtimePaths.memory_dir ?? ".nest/memory")}
                        </code>
                      </p>
                      <ol>
                        <li>Quit Kestrel Desktop completely so no memory writer remains active.</li>
                        <li>
                          Restore this folder&apos;s availability, owner-only
                          permissions, and free space. Do not move or delete
                          live <code className="mono">.mv2</code> files.
                        </li>
                        <li>
                          Reopen Kestrel. The Desktop launcher rechecks the
                          private directory and each Memvid layer before
                          starting the local API.
                        </li>
                        <li>Return to Setup Center and choose Check again.</li>
                      </ol>
                      <p>
                        Changing the memory location is not available in this
                        build. Kestrel will not claim a path change that the
                        Desktop launcher did not accept.
                      </p>
                    </Notice>
                  </div>
                )}
                <div className="layer-grid settings-layer-grid">
                  {memoryWorkspace.memoryLayers.map((layer) => (
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
                    capabilityKindOrder().map((kind) => {
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
                  <Metric
                    label="MCP servers"
                    value={
                      capabilities.filter(
                        (capability) =>
                          capability.kind === "mcp_server",
                      ).length
                    }
                  />
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
                {desktopRuntime ? (
                  <div className="row">
                    <div className="row-label">
                      <strong>
                        Provider credentials are added from
                        Provider settings.
                      </strong>
                      <p>
                        Desktop uses an isolated credential
                        dialog and does not expose generic secret
                        mutation in this renderer.
                      </p>
                    </div>
                  </div>
                ) : (
                  <BrowserSecretMutationForm
                    variant="settings"
                    onStored={acceptBrowserSecret}
                    onError={reportError}
                  />
                )}
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
                        {!desktopRuntime && (
                          <button className="btn" type="button" onClick={() => validateSecret(secret)}>Validate</button>
                        )}
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
                {desktopRuntime ? (
                  <div className="row">
                    <div className="row-label">
                      <strong>Desktop API authentication</strong>
                      <p>
                        Authentication is managed by the Kestrel Desktop main process. No API token is available to this page.
                      </p>
                    </div>
                    <div className="row-control">
                      <StatusBadge value="main-managed" />
                    </div>
                  </div>
                ) : (
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
                )}
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
          </SettingsWorkspace>
        )}
      </div>
      )}
    </div>
      )}
  </>
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
      <a
        className="btn subtle"
        href="#tools"
        onClick={(event) => {
          event.preventDefault();
          scrollToElement("tools");
        }}
      >
        Review prepared request in tool form
      </a>
      <JsonBlock value={preview.args} maxHeight="180px" />
    </section>
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

function MarkdownMessage({ text }: { text: string }) {
  return (
    <div className="markdown-message">
      <ReactMarkdown remarkPlugins={markdownPlugins} components={markdownComponents}>
        {text}
      </ReactMarkdown>
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
  setupReadiness: SetupReadinessReport | null,
  providerName: string,
  modelName: string
): SimpleChatStatus {
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
  const setupIssue = firstNonProviderSetupIssue(setupReadiness);
  if (setupReadiness && !setupReadiness.ready && setupIssue) {
    return {
      label: "Needs setup",
      detail:
        setupReadiness.next_action ||
        setupIssue.recovery ||
        "Finish setup before relying on this Kestrel.",
      action: "setup"
    };
  }
  if (setupReadiness?.experience_mode === "demo") {
    return {
      label: "Demo",
      detail: "Demo uses deterministic responses; no live model connected."
    };
  }
  if (setupReadiness?.experience_mode === "model_not_connected") {
    const providerModel = [providerName, modelName].filter(Boolean).join(" / ");
    return {
      label: "Model not connected",
      detail: `${providerModel || "The configured provider and model"} has no verified live model connection. Open Settings to configure or test it.`,
      action: "model-settings"
    };
  }
  if (setupReadiness?.experience_mode === "connected" && setupReadiness.ready) {
    return {
      label: "Ready",
      detail: activeRun ? "Kestrel is ready for the next message." : "Start a chat to begin."
    };
  }
  if (!setupReadiness) {
    return {
      label: "Checking setup",
      detail: "Kestrel could not verify setup readiness. Refresh before relying on this connection."
    };
  }
  return {
    label: "Needs setup",
    detail:
      setupReadiness.next_action ||
      "Kestrel received an unrecognized setup state. Review setup before relying on this connection.",
    action: "setup"
  };
}

function firstNonProviderSetupIssue(
  setupReadiness: SetupReadinessReport | null
) {
  if (!setupReadiness || !Array.isArray(setupReadiness.checks)) return null;
  return setupReadiness.checks.find(
    (check) =>
      check.status !== "pass" &&
      check.check_id !== "provider_configuration" &&
      check.check_id !== "provider_operational"
  ) ?? null;
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

function uniqueStrings(values: string[]): string[] {
  return Array.from(new Set(values.filter((value) => value.trim()).sort()));
}

function stringArray(value: unknown): string[] {
  return Array.isArray(value) ? value.map((item) => String(item)).filter(Boolean) : [];
}

function createThreadId(): string {
  return `thread_${crypto.randomUUID()}`;
}

function sectionFromHash(hash: string): LegacyWorkbenchSection | null {
  const normalized = hash.replace(/^#/, "").toLowerCase();
  return normalized === "mission" ||
    normalized === "chat" ||
    normalized === "outcomes" ||
    normalized === "routines" ||
    normalized === "routing" ||
    normalized === "advanced" ||
    normalized === "settings"
    ? normalized
    : null;
}

function scrollToElement(id: string) {
  const target = document.getElementById(id);
  if (typeof target?.scrollIntoView === "function") {
    target.scrollIntoView({ block: "start", behavior: "smooth" });
  }
  if (
    target instanceof HTMLElement &&
    target.hasAttribute("tabindex")
  ) {
    target.focus({ preventScroll: true });
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
  return catalogModels.length ? catalogModels : (deterministicModelDefaults[provider] ?? []);
}

function isKnownProviderModel(provider: string, model: string, catalogs: Record<string, ProviderModelCatalog>): boolean {
  return modelsForProvider(provider, catalogs).includes(model);
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
