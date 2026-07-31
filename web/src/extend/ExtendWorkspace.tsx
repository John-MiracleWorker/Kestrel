import {
  type FormEvent,
  type ReactNode,
  useCallback,
  useEffect,
  useMemo,
  useState,
} from "react";
import {
  CalendarClock,
  LineChart,
  RefreshCw,
  Route,
  Settings,
  X,
} from "lucide-react";
import {
  deleteJson,
  getJson,
  postJson,
  putJson,
} from "../api";
import { ActionError } from "../components";
import {
  capabilityForMcpServer,
  capabilityForMcpTool,
  capabilityForSkill,
  formatCapabilityBlocker,
  isToolEffectivelyEnabled,
  readJson,
  replaceCapability,
  uniqueStrings,
} from "./extendUtils";
import {
  type AdvancedRunRequest,
  useAdvancedOperations,
} from "./advancedOperations";
import type {
  Approval,
  Capability,
  CapabilityKind,
  CapabilityMutationResult,
  CapabilitySnapshot,
  McpServer,
  Plugin,
  PluginReviewReport,
  Run,
  Skill,
  SkillDiscoveryReport,
  Tool,
} from "../types";

export type ExtendToolPermissions = Record<string, boolean>;

type PreparedToolPreview = {
  name: string;
  args: Record<string, unknown>;
};

const emptyCapabilitySnapshot: CapabilitySnapshot = {
  items: [],
  counts: {
    total: 0,
    configured_enabled: 0,
    effective_enabled: 0,
    blocked: 0,
  },
};

export type ExtendWorkspaceOptions = {
  enabled: boolean;
  activeRun: Pick<Run, "run_id" | "session_id"> | null;
  activeSessionId: string | null;
  workspace: string;
  toolPermissions: ExtendToolPermissions;
  enqueueRun: (request: AdvancedRunRequest) => Promise<void>;
  onError: (error: unknown) => void;
  onNotice: (notice: string) => void;
  refreshCore: () => Promise<void>;
  refreshRunDetails: (runId: string) => Promise<void>;
  refreshAll: () => Promise<void>;
  createSessionId: () => string;
};

export function useExtendWorkspace({
  enabled,
  activeRun,
  activeSessionId,
  workspace,
  toolPermissions,
  enqueueRun,
  onError,
  onNotice,
  refreshCore,
  refreshRunDetails,
  refreshAll,
  createSessionId,
}: ExtendWorkspaceOptions) {
  const advancedOperations = useAdvancedOperations({
    activeRun,
    activeSessionId,
    workspace,
    enqueueRun,
    refreshCore,
    refreshRunDetails,
    refreshAll,
    createSessionId,
    onError,
    onNotice,
  });
  const [tools, setTools] = useState<Tool[]>([]);
  const [capabilitySnapshot, setCapabilitySnapshot] =
    useState<CapabilitySnapshot>(emptyCapabilitySnapshot);
  const [capabilityPending, setCapabilityPending] = useState<Set<string>>(
    () => new Set(),
  );
  const [capabilitySearch, setCapabilitySearch] = useState("");
  const [capabilityKindFilter, setCapabilityKindFilter] =
    useState<"all" | CapabilityKind>("all");
  const [capabilityStateFilter, setCapabilityStateFilter] = useState("all");
  const [allApprovals, setAllApprovals] = useState<Approval[]>([]);
  const [mcpServers, setMcpServers] = useState<McpServer[]>([]);
  const [skills, setSkills] = useState<Skill[]>([]);
  const [plugins, setPlugins] = useState<Plugin[]>([]);

  const [toolName, setToolName] = useState("");
  const [toolArgs, setToolArgs] = useState("{}");
  const [preparedToolPreview, setPreparedToolPreview] =
    useState<PreparedToolPreview | null>(null);
  const [toolResult, setToolResult] =
    useState<Record<string, unknown> | null>(null);
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
  const [mcpRiskPolicy, setMcpRiskPolicy] =
    useState("approval_by_default");
  const [mcpEditingServerId, setMcpEditingServerId] =
    useState<string | null>(null);
  const [mcpArgsTouched, setMcpArgsTouched] = useState(false);
  const [mcpEnvTouched, setMcpEnvTouched] = useState(false);
  const [mcpSecretEnvTouched, setMcpSecretEnvTouched] = useState(false);
  const [mcpToolSelection, setMcpToolSelection] = useState("");
  const [mcpToolArgs, setMcpToolArgs] = useState("{}");
  const [mcpResult, setMcpResult] =
    useState<Record<string, unknown> | null>(null);

  const [skillTask, setSkillTask] = useState("");
  const [skillSelection, setSkillSelection] = useState("");
  const [skillManifest, setSkillManifest] = useState(
    '{\n  "id": "local-skill",\n  "name": "Local Skill",\n  "description": "Describe what this skill does.",\n  "risk": "medium"\n}',
  );
  const [skillInstructions, setSkillInstructions] = useState("");
  const [skillResult, setSkillResult] =
    useState<Record<string, unknown> | null>(null);
  const [skillDiscovery, setSkillDiscovery] =
    useState<SkillDiscoveryReport | null>(null);
  const [skillDiscovering, setSkillDiscovering] = useState(false);

  const [pluginSource, setPluginSource] = useState("");
  const [pluginRef, setPluginRef] = useState("");
  const [pluginEnable, setPluginEnable] = useState(false);
  const [pluginResult, setPluginResult] =
    useState<Record<string, unknown> | null>(null);
  const [pluginReview, setPluginReview] =
    useState<PluginReviewReport | null>(null);
  const [pluginReviewSource, setPluginReviewSource] = useState("");
  const [pluginReviewRef, setPluginReviewRef] =
    useState<string | null>(null);
  const [pluginUpdateReviews, setPluginUpdateReviews] =
    useState<Record<string, string>>({});

  const refresh = useCallback(async () => {
    const [
      toolList,
      capabilityReport,
      approvalList,
      mcpList,
      skillList,
      pluginList,
    ] = await Promise.all([
      getJson<Tool[]>("/api/tools"),
      getJson<CapabilitySnapshot>("/api/capabilities"),
      getJson<Approval[]>("/api/approvals"),
      getJson<McpServer[]>("/api/mcp/servers"),
      getJson<Skill[]>("/api/skills"),
      getJson<Plugin[]>("/api/plugins"),
    ]);
    setTools(toolList);
    setCapabilitySnapshot(capabilityReport);
    setAllApprovals(approvalList);
    setMcpServers(mcpList);
    setSkills(skillList);
    setPlugins(pluginList);
  }, []);

  useEffect(() => {
    if (!enabled) return;
    void refresh().catch(onError);
  }, [enabled, onError, refresh]);

  const guarded = useCallback(
    async (action: () => Promise<void>, success?: string) => {
      try {
        await action();
        if (success) onNotice(success);
      } catch (error) {
        onError(error);
      }
    },
    [onError, onNotice],
  );

  const refreshAfterMutation = useCallback(async () => {
    const results = await Promise.allSettled([refresh(), refreshCore()]);
    const failure = results.find(
      (result): result is PromiseRejectedResult =>
        result.status === "rejected",
    );
    if (failure) onError(failure.reason);
  }, [onError, refresh, refreshCore]);

  const capabilities = capabilitySnapshot.items;
  const mcpToolOptions = useMemo(
    () =>
      mcpServers.flatMap((server) => {
        const serverCapability = capabilityForMcpServer(
          capabilities,
          server.id,
        );
        const serverEnabled =
          serverCapability?.effective_enabled ?? server.enabled;
        if (!serverEnabled) return [];
        return server.tools.flatMap((tool) => {
          const toolCapability = capabilityForMcpTool(
            capabilities,
            server.id,
            tool,
          );
          const toolEnabled =
            toolCapability?.effective_enabled ?? tool.enabled ?? true;
          return toolEnabled
            ? [
                {
                  server,
                  tool,
                  value: `${server.id}::${tool.remote_name ?? tool.name}`,
                },
              ]
            : [];
        });
      }),
    [capabilities, mcpServers],
  );
  const enabledSkills = useMemo(
    () =>
      skills.filter((skill) => {
        const capability = capabilityForSkill(capabilities, skill.id);
        return capability?.effective_enabled ?? skill.enabled;
      }),
    [capabilities, skills],
  );
  const filteredCapabilities = useMemo(() => {
    const query = capabilitySearch.trim().toLowerCase();
    return [...capabilities]
      .filter((capability) => {
        if (
          capabilityKindFilter !== "all" &&
          capability.kind !== capabilityKindFilter
        ) {
          return false;
        }
        if (
          capabilityStateFilter === "active" &&
          !capability.effective_enabled
        ) {
          return false;
        }
        if (
          capabilityStateFilter === "off" &&
          capability.configured_enabled
        ) {
          return false;
        }
        if (
          capabilityStateFilter === "blocked" &&
          capability.blocked_by.length === 0
        ) {
          return false;
        }
        if (!query) return true;
        return [
          capability.name,
          capability.id,
          capability.description,
          capability.source,
          capability.parent_key ?? "",
        ]
          .join(" ")
          .toLowerCase()
          .includes(query);
      })
      .sort((left, right) => left.name.localeCompare(right.name));
  }, [
    capabilities,
    capabilityKindFilter,
    capabilitySearch,
    capabilityStateFilter,
  ]);
  const selectedTool = useMemo(
    () => tools.find((tool) => tool.name === toolName) ?? null,
    [toolName, tools],
  );
  const selectedToolEnabled = Boolean(
    selectedTool &&
      isToolEffectivelyEnabled(
        selectedTool,
        toolPermissions,
        capabilities,
      ),
  );
  const selectedMcpToolEnabled = mcpToolOptions.some(
    (option) => option.value === mcpToolSelection,
  );
  const selectedSkillEnabled = enabledSkills.some(
    (skill) => skill.id === skillSelection,
  );
  const loadedMcpServer = mcpEditingServerId
    ? mcpServers.find((server) => server.id === mcpEditingServerId) ?? null
    : null;
  const enabledToolCount = useMemo(
    () =>
      tools.filter((tool) =>
        isToolEffectivelyEnabled(
          tool,
          toolPermissions,
          capabilities,
        ),
      ).length,
    [capabilities, toolPermissions, tools],
  );
  const filteredTools = useMemo(
    () =>
      tools.filter((tool) => {
        const toolEnabled = isToolEffectivelyEnabled(
          tool,
          toolPermissions,
          capabilities,
        );
        const query = toolFilter.trim().toLowerCase();
        const haystack = [
          tool.name,
          tool.description,
          tool.source,
          tool.risk,
          ...(tool.capabilities ?? []),
        ]
          .join(" ")
          .toLowerCase();
        if (query && !haystack.includes(query)) return false;
        if (
          toolSourceFilter !== "all" &&
          tool.source !== toolSourceFilter
        ) {
          return false;
        }
        if (toolRiskFilter !== "all" && tool.risk !== toolRiskFilter) {
          return false;
        }
        if (toolEnabledFilter === "enabled" && !toolEnabled) return false;
        if (toolEnabledFilter === "disabled" && toolEnabled) return false;
        return true;
      }),
    [
      capabilities,
      toolEnabledFilter,
      toolFilter,
      toolPermissions,
      toolRiskFilter,
      tools,
      toolSourceFilter,
    ],
  );
  const toolSources = useMemo(
    () => uniqueStrings(tools.map((tool) => tool.source)),
    [tools],
  );
  const toolRisks = useMemo(
    () => uniqueStrings(tools.map((tool) => tool.risk)),
    [tools],
  );
  const pluginSourceValue = pluginSource.trim();
  const pluginRefValue = pluginRef.trim() || null;
  const reviewedCurrentPlugin =
    Boolean(pluginReview) &&
    pluginReviewSource === pluginSourceValue &&
    pluginReviewRef === pluginRefValue;
  const pluginEnableBlockers = reviewedCurrentPlugin
    ? pluginReview?.enable_blockers ?? []
    : [];

  async function invokeTool(event: FormEvent) {
    event.preventDefault();
    await guarded(async () => {
      if (!selectedTool || !selectedToolEnabled) {
        throw new Error(
          "This tool is disabled. Enable it in Settings before invoking it.",
        );
      }
      const args = readJson<Record<string, unknown>>(toolArgs, {});
      setPreparedToolPreview(null);
      const result = await postJson<Record<string, unknown>>(
        `/api/tools/${encodeURIComponent(toolName)}/invoke`,
        {
          arguments: args,
          session_id: activeRun?.session_id ?? "manual",
          run_id: activeRun?.run_id ?? null,
        },
      );
      setToolResult(result);
      await refreshCore();
    });
  }

  async function setCapabilityEnabled(
    capability: Capability,
    nextEnabled: boolean,
  ) {
    if (
      nextEnabled &&
      ["high", "critical"].includes(capability.risk.toLowerCase()) &&
      !window.confirm(
        `Enable ${capability.name}? This ${capability.risk}-risk capability${
          capability.requires_approval
            ? " will still require approval when invoked"
            : " can be invoked without per-call approval"
        }.`,
      )
    ) {
      return;
    }
    setCapabilityPending((pending) =>
      new Set(pending).add(capability.key),
    );
    try {
      const result = await putJson<CapabilityMutationResult>(
        `/api/capabilities/${capability.kind}/${encodeURIComponent(
          capability.id,
        )}`,
        {
          enabled: nextEnabled,
          expected_revision: capability.revision,
        },
      );
      setCapabilitySnapshot((snapshot) =>
        replaceCapability(snapshot, result.capability),
      );
      await refreshAfterMutation();
      const revoked = result.revoked_approvals
        ? ` ${result.revoked_approvals} pending approval${
            result.revoked_approvals === 1 ? " was" : "s were"
          } revoked.`
        : "";
      const capabilityState =
        nextEnabled && !result.capability.effective_enabled
          ? `configured on but blocked by ${result.capability.blocked_by
              .map(formatCapabilityBlocker)
              .join(", ")}`
          : nextEnabled
            ? "enabled"
            : "disabled";
      onNotice(
        `${result.capability.name} ${capabilityState} for future invocations.${revoked}`,
      );
    } catch (error) {
      onError(error);
      await refresh().catch(() => undefined);
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
    setMcpEndpoint(
      server.transport === "stdio" ? server.command ?? "" : server.url ?? "",
    );
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
        risk_policy: mcpRiskPolicy,
      };
      if (mcpArgsTouched) {
        payload.args = readJson<string[]>(mcpArgs, []);
      }
      if (mcpEnvTouched) {
        payload.env = readJson<Record<string, string>>(mcpEnv, {});
      }
      if (mcpSecretEnvTouched) {
        payload.secret_env = readJson<Record<string, string>>(
          mcpSecretEnv,
          {},
        );
      }
      const path = mcpServers.some((server) => server.id === mcpId)
        ? `/api/mcp/servers/${encodeURIComponent(mcpId)}`
        : "/api/mcp/servers";
      const saved =
        path === "/api/mcp/servers"
          ? await postJson<McpServer>(path, payload)
          : await putJson<McpServer>(path, payload);
      setMcpId(saved.id);
      setMcpEditingServerId(saved.id);
      setMcpArgsTouched(false);
      setMcpEnvTouched(false);
      setMcpSecretEnvTouched(false);
      await refreshAfterMutation();
    }, "MCP server saved.");
  }

  async function controlMcp(
    server: McpServer,
    action: "connect" | "disconnect" | "restart" | "sync" | "test",
  ) {
    await guarded(async () => {
      const result = await postJson<Record<string, unknown>>(
        `/api/mcp/servers/${encodeURIComponent(server.id)}/${action}`,
      );
      setMcpResult(result);
      await refreshAfterMutation();
    });
  }

  async function deleteMcp(server: McpServer) {
    await guarded(async () => {
      await deleteJson(`/api/mcp/servers/${encodeURIComponent(server.id)}`);
      await refreshAfterMutation();
    }, "MCP server removed.");
  }

  async function invokeMcp(event: FormEvent) {
    event.preventDefault();
    await guarded(async () => {
      if (!selectedMcpToolEnabled) {
        throw new Error(
          "This MCP tool is disabled. Enable its server and tool before invoking it.",
        );
      }
      const [serverId, remoteName] = mcpToolSelection.split("::");
      const result = await postJson<Record<string, unknown>>(
        `/api/mcp/servers/${encodeURIComponent(
          serverId,
        )}/tools/${encodeURIComponent(remoteName)}/invoke`,
        {
          arguments: readJson<Record<string, unknown>>(mcpToolArgs, {}),
        },
      );
      setMcpResult(result);
      await refreshAfterMutation();
    });
  }

  async function toggleSkill(skill: Skill) {
    await guarded(async () => {
      await postJson(
        `/api/skills/${encodeURIComponent(skill.id)}/${
          skill.enabled ? "disable" : "enable"
        }`,
      );
      await refreshAfterMutation();
    });
  }

  async function discoverSkills() {
    await guarded(async () => {
      setSkillDiscovering(true);
      try {
        const result =
          await postJson<SkillDiscoveryReport>("/api/skills/discover");
        setSkillDiscovery(result);
        setSkillResult(result as unknown as Record<string, unknown>);
        setSkills(result.skills);
        await refreshAfterMutation();
        onNotice(result.message);
      } finally {
        setSkillDiscovering(false);
      }
    });
  }

  async function installSkill(event: FormEvent) {
    event.preventDefault();
    await guarded(async () => {
      const result = await postJson<Record<string, unknown>>(
        "/api/skills/install",
        {
          manifest: readJson<Record<string, unknown>>(skillManifest, {}),
          instructions: skillInstructions,
          overwrite: true,
          dry_run: false,
        },
      );
      setSkillResult(result);
      await refreshAfterMutation();
    });
  }

  async function runSkill(event: FormEvent) {
    event.preventDefault();
    await guarded(async () => {
      if (!selectedSkillEnabled) {
        throw new Error(
          "This skill is disabled. Enable it in Settings before running it.",
        );
      }
      const result = await postJson<Record<string, unknown>>(
        `/api/skills/${encodeURIComponent(skillSelection)}/run`,
        {
          arguments: {
            task: skillTask,
            context: { active_run_id: activeRun?.run_id ?? null },
          },
          session_id: activeRun?.session_id ?? "manual",
          run_id: activeRun?.run_id ?? null,
        },
      );
      setSkillResult(result);
      await refreshAfterMutation();
    });
  }

  async function reviewPlugin(event: FormEvent) {
    event.preventDefault();
    await guarded(async () => {
      const source = pluginSource.trim();
      const ref = pluginRef.trim() || null;
      const result = await postJson<PluginReviewReport>(
        "/api/plugins/review",
        { source, ref },
      );
      setPluginReview(result);
      setPluginReviewSource(source);
      setPluginReviewRef(ref);
      setPluginResult(result as unknown as Record<string, unknown>);
      if (result.enable_blockers.length > 0) setPluginEnable(false);
      onNotice(
        result.enable_blockers.length
          ? "Plugin review found enable blockers."
          : "Plugin review complete.",
      );
    });
  }

  async function installPlugin() {
    await guarded(async () => {
      const result = await postJson<Record<string, unknown>>(
        "/api/plugins/install",
        {
          source: pluginSource,
          ref: pluginRef || null,
          enable: pluginEnable,
          overwrite: true,
        },
      );
      setPluginResult(result);
      await refreshAfterMutation();
    });
  }

  async function pluginAction(
    plugin: Plugin,
    action: "enable" | "disable" | "update" | "remove",
  ) {
    await guarded(async () => {
      const path = `/api/plugins/${encodeURIComponent(plugin.id)}`;
      if (action === "update" && !pluginUpdateReviews[plugin.id]) {
        const review = await postJson<PluginReviewReport>(
          `${path}/review-update`,
          { ref: plugin.source_ref },
        );
        setPluginUpdateReviews((current) => ({
          ...current,
          [plugin.id]: review.commit_sha,
        }));
        setPluginResult(review as unknown as Record<string, unknown>);
        onNotice(
          review.authority_delta?.expands_authority
            ? "Update review found added authority. Disable the plugin before applying it."
            : "Update reviewed. Apply only after inspecting provenance, compatibility, and authority delta.",
        );
        return;
      }
      const result =
        action === "remove"
          ? await deleteJson<Record<string, unknown>>(path)
          : await postJson<Record<string, unknown>>(
              `${path}/${action}`,
              action === "update" ? { ref: plugin.source_ref } : {},
            );
      if (action === "update" || action === "remove") {
        setPluginUpdateReviews((current) => {
          const next = { ...current };
          delete next[plugin.id];
          return next;
        });
      }
      setPluginResult(result);
      await refreshAfterMutation();
    });
  }

  return {
    ...advancedOperations,
    tools,
    capabilitySnapshot,
    capabilityPending,
    capabilitySearch,
    setCapabilitySearch,
    capabilityKindFilter,
    setCapabilityKindFilter,
    capabilityStateFilter,
    setCapabilityStateFilter,
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
    capabilities,
    mcpToolOptions,
    enabledSkills,
    filteredCapabilities,
    selectedTool,
    selectedToolEnabled,
    selectedMcpToolEnabled,
    selectedSkillEnabled,
    loadedMcpServer,
    enabledToolCount,
    filteredTools,
    toolSources,
    toolRisks,
    reviewedCurrentPlugin,
    pluginEnableBlockers,
    refresh,
    invokeTool,
    setCapabilityEnabled,
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
  };
}

export type ExtendWorkspaceController = ReturnType<
  typeof useExtendWorkspace
>;

export function ExtendWorkspace({
  controller,
  error,
  onDismissError,
  onNavigate,
  onRefresh,
  children,
}: {
  controller: ExtendWorkspaceController;
  error: string | null;
  onDismissError: () => void;
  onNavigate: (
    destination: "routines" | "routing" | "outcomes" | "settings" | "chat",
  ) => void;
  onRefresh: () => Promise<void>;
  children: ReactNode;
}) {
  return (
    <section
      id="advanced"
      className="shell page-shell advanced-page"
      data-section="advanced"
      aria-label="Advanced Operator Console"
    >
      <header className="page-head">
        <div>
          <p className="page-eyebrow">Operator Console</p>
          <h1 className="page-title">
            Advanced<em>.</em>
          </h1>
          <p className="page-subtitle">
            Tuning surfaces for the runtime that powers Kestrel: runs,
            approvals, memory, tools, MCP, plugins, channels, traces, and
            gated capabilities. Defaults stay conservative.
          </p>
        </div>
        <div className="page-actions">
          <button
            className="btn subtle"
            type="button"
            onClick={() => {
              void onRefresh();
            }}
          >
            <RefreshCw size={15} /> Refresh
          </button>
          <button
            className="btn primary"
            type="button"
            onClick={() => onNavigate("chat")}
          >
            <X size={15} /> Close
          </button>
        </div>
      </header>
      <nav
        className="advanced-surface-nav"
        aria-label="Advanced workspaces"
      >
        <button
          type="button"
          onClick={() => onNavigate("routines")}
        >
          <CalendarClock size={15} /> Routines
        </button>
        <button
          type="button"
          onClick={() => onNavigate("routing")}
        >
          <Route size={15} /> Routing
        </button>
        <button
          type="button"
          onClick={() => onNavigate("outcomes")}
        >
          <LineChart size={15} /> Outcomes
        </button>
        <button
          type="button"
          onClick={() => onNavigate("settings")}
        >
          <Settings size={15} /> Settings
        </button>
      </nav>
      {error && (
        <ActionError message={error} onDismiss={onDismissError} />
      )}
      <span className="sr-only" aria-live="polite">
        {controller.tools.length} tools and {controller.plugins.length} plugins
        loaded.
      </span>
      {children}
    </section>
  );
}
