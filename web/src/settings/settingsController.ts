import {
  type FormEvent,
  useCallback,
  useEffect,
  useMemo,
  useState,
} from "react";
import {
  deleteJson,
  getJson,
  postJson,
  putJson,
  queryString,
} from "../api";
import { readDesktopBridge } from "../platform/desktopBridge";
import type {
  Capability,
  CapabilityKind,
  CapabilityMutationResult,
  CapabilitySnapshot,
  Channel,
  ProviderModelCatalog,
  RuntimeConfig,
  SecretRef,
  Tool,
} from "../types";
import {
  formatCapabilityBlocker,
  isToolEffectivelyEnabled,
  replaceCapability,
} from "../extend/extendUtils";

export const toolPermissionDefinitions = [
  {
    key: "allow_shell",
    label: "Command tools",
    description:
      "shell.run, test.run, lint.run, and shell-backed validation.",
    risk: "high risk",
  },
  {
    key: "allow_file_write",
    label: "File-write tools",
    description:
      "file.write, patch.apply, repairs, and skill materialization.",
    risk: "high risk",
  },
  {
    key: "allow_codex_cli",
    label: "Codex CLI",
    description: "codex.exec delegation through the local Codex CLI.",
    risk: "high risk",
  },
  {
    key: "allow_web",
    label: "Web context",
    description: "web.search and web.fetch read-only outside context.",
    risk: "medium risk",
  },
  {
    key: "allow_plugin_install",
    label: "Plugin install",
    description: "plugin.install from approved Kestrel manifests.",
    risk: "high risk",
  },
  {
    key: "allow_memory_import",
    label: "Memory import",
    description:
      "memory.import with provenance and validation metadata.",
    risk: "high risk",
  },
  {
    key: "allow_executable_skills",
    label: "Executable skills",
    description: "Skill-provided executable tool adapters.",
    risk: "high risk",
  },
  {
    key: "allow_git_commit",
    label: "Git commit",
    description: "git.commit under exact-call approval.",
    risk: "high risk",
  },
  {
    key: "allow_self_modification",
    label: "Self proposals",
    description: "self.propose_change through the repair gate.",
    risk: "critical risk",
  },
] as const;

export type ToolPermissionKey =
  (typeof toolPermissionDefinitions)[number]["key"];
export type ToolPermissionDraft = Record<ToolPermissionKey, boolean>;

export const defaultToolPermissions = Object.fromEntries(
  toolPermissionDefinitions.map((permission) => [permission.key, false]),
) as ToolPermissionDraft;

type ProviderMetadata = {
  baseUrl?: string;
  apiKeyEnv?: string;
};

const providerMetadata: Record<string, ProviderMetadata> = {
  "lm-studio": { baseUrl: "http://localhost:1234/v1" },
  ollama: { baseUrl: "http://localhost:11434/v1" },
  openai: { apiKeyEnv: "OPENAI_API_KEY" },
  anthropic: { apiKeyEnv: "ANTHROPIC_API_KEY" },
  grok: {
    baseUrl: "https://api.x.ai/v1",
    apiKeyEnv: "XAI_API_KEY",
  },
  gemini: { apiKeyEnv: "GEMINI_API_KEY" },
  "ollama-cloud": {
    baseUrl: "https://ollama.com/api",
    apiKeyEnv: "OLLAMA_API_KEY",
  },
  openrouter: {
    baseUrl: "https://openrouter.ai/api/v1",
    apiKeyEnv: "OPENROUTER_API_KEY",
  },
  deepseek: {
    baseUrl: "https://api.deepseek.com",
    apiKeyEnv: "DEEPSEEK_API_KEY",
  },
  kimi: {
    baseUrl: "https://api.moonshot.ai/v1",
    apiKeyEnv: "MOONSHOT_API_KEY",
  },
};

const desktopCredentialProviders = new Set([
  "openai",
  "openrouter",
  "deepseek",
  "kimi",
  "ollama-cloud",
  "anthropic",
  "grok",
  "gemini",
]);
const deterministicModelDefaults: Record<string, string[]> = {
  mock: ["mock"],
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

export type SettingsWorkspaceOptions = {
  enabled: boolean;
  includeCapabilities: boolean;
  desktopRuntime: boolean;
  onError: (error: unknown) => void;
  onNotice: (notice: string) => void;
  refreshCore: () => Promise<void>;
};

export function useSettingsWorkspace({
  enabled,
  includeCapabilities,
  desktopRuntime,
  onError,
  onNotice,
  refreshCore,
}: SettingsWorkspaceOptions) {
  const [runtime, setRuntime] = useState<RuntimeConfig | null>(null);
  const [runtimeSettingsResult, setRuntimeSettingsResult] =
    useState<Record<string, unknown> | null>(null);
  const [workspace, setWorkspace] = useState("");
  const [provider, setProvider] = useState("mock");
  const [model, setModel] = useState("mock");
  const [baseUrl, setBaseUrl] = useState("");
  const [apiKeyEnv, setApiKeyEnv] = useState("");
  const [providerSecretResult, setProviderSecretResult] =
    useState<SecretRef | null>(null);
  const [temperature, setTemperature] = useState("0.2");
  const [maxToolRounds, setMaxToolRounds] = useState("6");
  const [modelCatalogs, setModelCatalogs] =
    useState<Record<string, ProviderModelCatalog>>({});
  const [modelCatalogLoading, setModelCatalogLoading] = useState(false);
  const [autonomyMode, setAutonomyMode] = useState("background");
  const [streamResponses, setStreamResponses] = useState(false);
  const [memoryBackendDraft, setMemoryBackendDraft] =
    useState<"In-memory" | "Memvid">("In-memory");
  const [apiAuthRequired, setApiAuthRequired] = useState(false);
  const [toolPermissions, setToolPermissions] =
    useState<ToolPermissionDraft>(defaultToolPermissions);
  const [tools, setTools] = useState<Tool[]>([]);
  const [capabilitySnapshot, setCapabilitySnapshot] =
    useState<CapabilitySnapshot>(emptyCapabilitySnapshot);
  const [capabilityPending, setCapabilityPending] = useState<Set<string>>(
    () => new Set(),
  );
  const [capabilitySearch, setCapabilitySearch] = useState("");
  const [capabilityKindFilter, setCapabilityKindFilter] =
    useState<"all" | CapabilityKind>("all");
  const [capabilityStateFilter, setCapabilityStateFilter] =
    useState("all");

  const [channels, setChannels] = useState<Channel[]>([]);
  const [secrets, setSecrets] = useState<SecretRef[]>([]);
  const [channelId, setChannelId] = useState("webhook");
  const [channelProvider, setChannelProvider] = useState("webhook");
  const [channelTokenEnv, setChannelTokenEnv] = useState("");
  const [channelWebhookEnv, setChannelWebhookEnv] = useState(
    "NEST_AGENT_CHANNEL_WEBHOOK_URL",
  );
  const [channelEnabled, setChannelEnabled] = useState(true);
  const [channelSendEnabled, setChannelSendEnabled] = useState(false);
  const [channelAutoReply, setChannelAutoReply] = useState(false);
  const [channelSettings, setChannelSettings] = useState("{}");
  const [channelPayload, setChannelPayload] = useState(
    '{\n  "conversation_id": "local-thread",\n  "text": "hello from the UI"\n}',
  );
  const [channelResult, setChannelResult] =
    useState<Record<string, unknown> | null>(null);
  const [telegramWebhookUrl, setTelegramWebhookUrl] = useState("");
  const [telegramActionResult, setTelegramActionResult] =
    useState<Record<string, unknown> | null>(null);
  const [secretResult, setSecretResult] = useState<SecretRef | null>(null);

  const refreshInventory = useCallback(async () => {
    const [channelList, secretList, capabilityInventory] =
      await Promise.all([
      getJson<Channel[]>("/api/channels"),
      getJson<SecretRef[]>("/api/secrets"),
        includeCapabilities
          ? Promise.all([
              getJson<Tool[]>("/api/tools"),
              getJson<CapabilitySnapshot>("/api/capabilities"),
            ])
          : Promise.resolve(null),
      ]);
    setChannels(channelList);
    setSecrets(secretList);
    if (capabilityInventory) {
      setTools(capabilityInventory[0]);
      setCapabilitySnapshot(capabilityInventory[1]);
    }
  }, [includeCapabilities]);

  useEffect(() => {
    if (!enabled) return;
    void refreshInventory().catch(onError);
  }, [enabled, onError, refreshInventory]);

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
    const results = await Promise.allSettled([
      refreshInventory(),
      refreshCore(),
    ]);
    const failure = results.find(
      (result): result is PromiseRejectedResult =>
        result.status === "rejected",
    );
    if (failure) onError(failure.reason);
  }, [onError, refreshCore, refreshInventory]);

  const hydrateRuntime = useCallback((config: RuntimeConfig) => {
    setRuntime(config);
    const savedSettings = runtimeSettingsFrom(config);
    const nextProvider = String(
      savedSettings.provider ?? config.provider?.name ?? "mock",
    );
    const metadata = providerMetadata[nextProvider];
    setProvider(nextProvider);
    setModel(
      String(savedSettings.model ?? config.provider?.model ?? "mock"),
    );
    setBaseUrl(
      String(savedSettings.base_url ?? metadata?.baseUrl ?? ""),
    );
    setApiKeyEnv(
      String(
        savedSettings.api_key_env ??
          config.provider?.api_key_env ??
          metadata?.apiKeyEnv ??
          "",
      ),
    );
    setProviderSecretResult(null);
    setTemperature(
      formatTemperature(
        savedSettings.temperature ??
          config.provider?.temperature ??
          0.2,
      ),
    );
    setMaxToolRounds(
      formatToolRounds(
        savedSettings.max_tool_rounds ??
          config.limits?.max_tool_rounds ??
          6,
      ),
    );
    setWorkspace(
      String(savedSettings.workspace ?? config.paths?.workspace ?? ""),
    );
    setAutonomyMode(
      validAutonomyMode(savedSettings.autonomy_mode, "background"),
    );
    setMemoryBackendDraft(
      String(savedSettings.backend ?? "").toLowerCase() === "memvid"
        ? "Memvid"
        : "In-memory",
    );
    setStreamResponses(
      Boolean(savedSettings.stream ?? config.provider?.stream),
    );
    setApiAuthRequired(
      Boolean(
        savedSettings.require_api_auth ??
          config.feature_flags?.require_api_auth,
      ),
    );
    setToolPermissions(toolPermissionsFromRuntime(config));
  }, []);

  async function refreshRuntime() {
    const config = await getJson<RuntimeConfig>("/api/runtime/config");
    hydrateRuntime(config);
  }

  const providerCatalog = modelCatalogs[provider] ?? null;
  const modelSuggestions = providerCatalog?.models?.length
    ? providerCatalog.models
    : deterministicModelDefaults[provider] ?? [];
  const modelCatalogLabel = modelCatalogLoading
    ? "loading"
    : providerCatalog?.ok
      ? providerCatalog.source === "provider"
        ? `${providerCatalog.models.length} provider models`
        : `${providerCatalog.models.length} discovered models`
      : providerCatalog?.error
        ? "catalog unavailable"
        : "not discovered";
  const effectiveApiKeyEnv =
    apiKeyEnv.trim() ||
    providerCatalog?.api_key_env ||
    providerMetadata[provider]?.apiKeyEnv ||
    "";
  const capabilities = capabilitySnapshot.items;
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
      const state =
        nextEnabled && !result.capability.effective_enabled
          ? `configured on but blocked by ${result.capability.blocked_by
              .map(formatCapabilityBlocker)
              .join(", ")}`
          : nextEnabled
            ? "enabled"
            : "disabled";
      onNotice(
        `${result.capability.name} ${state} for future invocations.${revoked}`,
      );
    } catch (error) {
      onError(error);
      await refreshInventory().catch(() => undefined);
    } finally {
      setCapabilityPending((pending) => {
        const next = new Set(pending);
        next.delete(capability.key);
        return next;
      });
    }
  }

  function chooseProvider(nextProvider: string) {
    const metadata = providerMetadata[nextProvider];
    setProvider(nextProvider);
    setBaseUrl(metadata?.baseUrl ?? "");
    setApiKeyEnv(metadata?.apiKeyEnv ?? "");
    setProviderSecretResult(null);
    const suggestions = modelsForProvider(nextProvider, modelCatalogs);
    setModel((current) => {
      if (
        !current.trim() ||
        !isKnownProviderModel(nextProvider, current, modelCatalogs)
      ) {
        return suggestions[0] ?? "";
      }
      return current;
    });
  }

  async function refreshProviderModels(nextProvider = provider) {
    setModelCatalogLoading(true);
    try {
      const catalog = await getJson<ProviderModelCatalog>(
        `/api/runtime/models${queryString({ provider: nextProvider })}`,
      );
      setModelCatalogs((catalogs) => ({
        ...catalogs,
        [catalog.provider]: catalog,
      }));
      setApiKeyEnv(
        (current) =>
          current.trim() ||
          catalog.api_key_env ||
          providerMetadata[catalog.provider]?.apiKeyEnv ||
          "",
      );
      setModel((current) => {
        if (!catalog.models.length || !catalog.ok) return current;
        return current.trim() ? current : catalog.models[0] ?? current;
      });
    } catch {
      const fallback = deterministicModelDefaults[nextProvider] ?? [];
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
          api_key_configured: false,
        },
      }));
    } finally {
      setModelCatalogLoading(false);
    }
  }

  async function saveRuntimeSettings() {
    if (!runtime) return;
    await guarded(async () => {
      const savedSettings = runtimeSettingsFrom(runtime);
      const result = await putJson<Record<string, unknown>>(
        "/api/runtime/settings",
        {
          expected_revision: String(savedSettings.revision ?? ""),
          provider,
          model: model.trim() || "mock",
          base_url: baseUrl.trim() || null,
          api_key_env: effectiveApiKeyEnv.trim() || null,
          temperature: coerceTemperature(temperature),
          max_tool_rounds: coerceToolRounds(maxToolRounds),
          backend:
            memoryBackendDraft === "Memvid" ? "memvid" : "memory",
          memory_dir: String(
            savedSettings.memory_dir ??
              runtime.paths?.memory_dir ??
              ".nest/memory",
          ),
          workspace:
            workspace.trim() || String(runtime.paths?.workspace ?? "."),
          stream: streamResponses,
          autonomy_mode: autonomyMode,
          ...toolPermissions,
        },
      );
      setRuntimeSettingsResult(result);
      await refreshAfterMutation();
      await refreshRuntime().catch(onError);
    }, "Settings saved and applied to new runs.");
  }

  async function storeDesktopProviderKey() {
    if (
      !desktopRuntime ||
      !desktopCredentialProviders.has(provider)
    ) {
      return;
    }
    await guarded(async () => {
      const bridge = readDesktopBridge();
      if (bridge === null) {
        throw new Error("desktop_bridge_unavailable");
      }
      const result = desktopCredentialDialogResult(
        await bridge.openCredentialDialog({
          providerId: provider,
          purpose: "provider_api_key",
        }),
      );
      if (result.status === "cancelled") {
        onNotice("Credential entry cancelled.");
        return;
      }
      const canonicalName =
        providerMetadata[provider]?.apiKeyEnv ?? "";
      setProviderSecretResult({
        id: result.secretRef.slice("secret://".length),
        name: canonicalName,
        purpose: "Provider API key",
        secret_ref: result.secretRef,
        configured: true,
        validated: result.validation === "valid",
        fingerprint: result.fingerprint,
        source: "desktop_keyring",
      });
      onNotice("Provider key stored.");
      await refreshProviderModels(provider);
      await refreshAfterMutation();
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
        settings: readJson<Record<string, unknown>>(channelSettings, {}),
      };
      const path = channels.some((channel) => channel.id === channelId)
        ? `/api/channels/${encodeURIComponent(channelId)}`
        : "/api/channels";
      const saved =
        path === "/api/channels"
          ? await postJson<Channel>(path, payload)
          : await putJson<Channel>(path, payload);
      setChannelId(saved.id);
      await refreshAfterMutation();
    }, "Channel saved.");
  }

  async function deleteChannel(channel: Channel) {
    await guarded(async () => {
      await deleteJson(`/api/channels/${encodeURIComponent(channel.id)}`);
      await refreshAfterMutation();
    }, "Channel removed.");
  }

  async function ingestChannel(event: FormEvent) {
    event.preventDefault();
    await guarded(async () => {
      const result = await postJson<Record<string, unknown>>(
        "/api/channels/ingest",
        {
          provider: channelProvider,
          channel_id: channelId,
          payload: readJson<Record<string, unknown>>(channelPayload, {}),
          send: false,
        },
      );
      setChannelResult(result);
      await refreshCore();
    });
  }

  async function telegramWebhookInfo(channel: Channel) {
    await guarded(async () => {
      const result = await getJson<Record<string, unknown>>(
        `/api/channels/${encodeURIComponent(
          channel.id,
        )}/telegram/webhook-info`,
      );
      setTelegramActionResult(result);
    }, "Telegram webhook info loaded.");
  }

  async function telegramSetWebhook(channel: Channel) {
    await guarded(async () => {
      const result = await postJson<Record<string, unknown>>(
        `/api/channels/${encodeURIComponent(
          channel.id,
        )}/telegram/set-webhook`,
        { url: telegramWebhookUrl, drop_pending_updates: false },
      );
      setTelegramActionResult(result);
    }, "Telegram webhook updated.");
  }

  async function telegramDeleteWebhook(channel: Channel) {
    await guarded(async () => {
      const result = await postJson<Record<string, unknown>>(
        `/api/channels/${encodeURIComponent(
          channel.id,
        )}/telegram/delete-webhook`,
        { drop_pending_updates: false },
      );
      setTelegramActionResult(result);
    }, "Telegram webhook removed.");
  }

  async function acceptBrowserSecret(saved: SecretRef) {
    setSecretResult(saved);
    onNotice("Secret stored.");
    await refreshAfterMutation();
  }

  async function acceptBrowserProviderSecret(saved: SecretRef) {
    setProviderSecretResult(saved);
    onNotice("Provider key stored.");
    await refreshProviderModels(provider);
    await refreshAfterMutation();
  }

  async function validateSecret(secret: SecretRef) {
    if (desktopRuntime) {
      throw new Error("desktop_generic_secret_mutation_unavailable");
    }
    await guarded(async () => {
      const result = await postJson<SecretRef>(
        `/api/secrets/${encodeURIComponent(secret.id)}/validate`,
      );
      setSecretResult(result);
      await refreshAfterMutation();
    }, "Secret validated.");
  }

  async function deleteSecret(secret: SecretRef) {
    if (desktopRuntime) {
      throw new Error("desktop_generic_secret_mutation_unavailable");
    }
    await guarded(async () => {
      await deleteJson(`/api/secrets/${encodeURIComponent(secret.id)}`);
      setSecretResult(null);
      await refreshAfterMutation();
    }, "Secret removed.");
  }

  return {
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
    effectiveApiKeyEnv,
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
    refreshInventory,
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
  };
}

export type SettingsWorkspaceController = ReturnType<
  typeof useSettingsWorkspace
>;

function runtimeSettingsFrom(
  config: RuntimeConfig | null,
): Record<string, unknown> {
  const runtimeSettings = config?.settings?.runtime;
  return runtimeSettings &&
    typeof runtimeSettings === "object" &&
    !Array.isArray(runtimeSettings)
    ? (runtimeSettings as Record<string, unknown>)
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

function modelsForProvider(
  provider: string,
  catalogs: Record<string, ProviderModelCatalog>,
): string[] {
  const catalogModels = catalogs[provider]?.models ?? [];
  return catalogModels.length
    ? catalogModels
    : deterministicModelDefaults[provider] ?? [];
}

function isKnownProviderModel(
  provider: string,
  model: string,
  catalogs: Record<string, ProviderModelCatalog>,
): boolean {
  return modelsForProvider(provider, catalogs).includes(model);
}

function toolPermissionsFromRuntime(
  config: RuntimeConfig,
): ToolPermissionDraft {
  const savedSettings = runtimeSettingsFrom(config);
  const featureFlags = config.feature_flags ?? {};
  return Object.fromEntries(
    toolPermissionDefinitions.map((permission) => [
      permission.key,
      Boolean(
        savedSettings[permission.key] ?? featureFlags[permission.key],
      ),
    ]),
  ) as ToolPermissionDraft;
}

function validAutonomyMode(value: unknown, fallback: string): string {
  const mode = String(value ?? "");
  return mode === "background" ||
    mode === "manual" ||
    mode === "autonomous"
    ? mode
    : fallback;
}

function readJson<T>(text: string, fallback: T): T {
  if (!text.trim()) return fallback;
  return JSON.parse(text) as T;
}

type DesktopCredentialDialogResult =
  | {
      status: "stored";
      secretRef: string;
      validation: "unverified" | "valid" | "invalid";
      fingerprint: string;
    }
  | { status: "cancelled" };

function desktopCredentialDialogResult(
  value: unknown,
): DesktopCredentialDialogResult {
  try {
    if (
      typeof value !== "object" ||
      value === null ||
      ![Object.prototype, null].includes(Object.getPrototypeOf(value))
    ) {
      throw new Error("invalid");
    }
    const keys = Reflect.ownKeys(value);
    const descriptors = Object.getOwnPropertyDescriptors(value);
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
      "fingerprint",
    ]);
    const secretRef = descriptors.secretRef?.value;
    const validation = descriptors.validation?.value;
    const fingerprint = descriptors.fingerprint?.value;
    if (
      status !== "stored" ||
      keys.length !== expectedKeys.size ||
      keys.some(
        (key) =>
          typeof key !== "string" || !expectedKeys.has(key),
      ) ||
      typeof secretRef !== "string" ||
      secretRef.length > 256 ||
      !/^secret:\/\/[A-Za-z0-9._/-]+$/.test(secretRef) ||
      !["unverified", "valid", "invalid"].includes(validation) ||
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
      fingerprint,
    } as DesktopCredentialDialogResult;
  } catch {
    throw new Error("desktop_credential_result_invalid");
  }
}
