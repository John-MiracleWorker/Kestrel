import {
  Cloud,
  Cpu,
  KeyRound,
  WifiOff,
} from "lucide-react";
import {
  Button,
  Card,
  EmptyState,
  Field,
  Notice,
  StatusPill,
} from "../../components";
import type {
  ProviderModelCatalog,
  SecretRef,
} from "../../types";
import type {
  IntelligenceSelection,
  SetupRuntimeSettings,
} from "../types";

const providerBaseUrls: Record<string, string> = {
  "lm-studio": "http://localhost:1234/v1",
  ollama: "http://localhost:11434/v1",
  grok: "https://api.x.ai/v1",
  "ollama-cloud": "https://ollama.com/api",
  openrouter: "https://openrouter.ai/api/v1",
  deepseek: "https://api.deepseek.com",
  kimi: "https://api.moonshot.ai/v1",
};

export function IntelligenceStage({
  catalogs,
  secrets,
  runtime,
  provider,
  model,
  pending,
  error,
  nativeCredentialEntry,
  onProviderChange,
  onModelChange,
  onContinueDemo,
  onUseSelection,
  onStoreCredential,
  onOpenProviderSettings,
}: {
  catalogs: ProviderModelCatalog[];
  secrets: SecretRef[];
  runtime: SetupRuntimeSettings;
  provider: string;
  model: string;
  pending: boolean;
  error: string | null;
  nativeCredentialEntry: boolean;
  onProviderChange: (provider: string) => void;
  onModelChange: (model: string) => void;
  onContinueDemo: (selection: IntelligenceSelection) => void;
  onUseSelection: (selection: IntelligenceSelection) => void;
  onStoreCredential: (provider: string) => void;
  onOpenProviderSettings: () => void;
}) {
  const liveCatalogs = catalogs.filter(
    (catalog) => catalog.provider !== "mock",
  );
  const selected =
    catalogs.find((catalog) => catalog.provider === provider) ??
    liveCatalogs[0] ??
    null;
  const models = selected?.models.length
    ? selected.models
    : selected?.fallback_models ?? [];
  const apiKeyConfigured = Boolean(
    selected?.api_key_configured ||
      secrets.some(
        (secret) =>
          selected?.api_key_env &&
          secret.name === selected.api_key_env &&
          secret.configured,
      ),
  );
  const needsCredential = Boolean(
    selected?.api_key_env && !apiKeyConfigured,
  );
  const canUseSelection = Boolean(
    selected &&
      selected.provider !== "mock" &&
      selected.ok &&
      model.trim() &&
      !needsCredential,
  );

  return (
    <div className="setup-stage">
      <header className="setup-stage-heading">
        <p className="page-eyebrow">Intelligence</p>
        <h2 tabIndex={-1} data-setup-stage-heading>
          Choose intelligence
        </h2>
        <p>
          Start with bundled deterministic Demo responses, or connect a
          model already visible to Kestrel. Provider keys remain in the
          Secret Broker.
        </p>
      </header>

      {error ? (
        <Notice variant="danger" title="Intelligence could not be saved">
          {error}
        </Notice>
      ) : null}

      <Card
        title="Offline Demo"
        icon={<WifiOff size={18} />}
        headingLevel={3}
        className="setup-demo-card"
        actions={<StatusPill state="healthy">Bundled</StatusPill>}
      >
        <p>
          Deterministic, private, and available without a network or API
          key. Demo is ideal for learning the workbench and verifying the
          installation.
        </p>
        <Button
          variant="primary"
          pending={pending}
          onClick={() =>
            onContinueDemo({
              expectedRevision: runtime.expectedRevision,
              provider: "mock",
              model: "mock",
              baseUrl: null,
              apiKeyEnv: null,
            })
          }
        >
          Continue with Demo
        </Button>
      </Card>

      <section className="setup-live-models" aria-labelledby="live-models-title">
        <div className="setup-subhead">
          <div>
            <h3 id="live-models-title">Connected models</h3>
            <p>
              Catalog status comes from the local Kestrel runtime, not from
              browser inference.
            </p>
          </div>
          <span className="setup-secret-count">
            <KeyRound size={14} aria-hidden="true" />
            {secrets.filter((secret) => secret.configured).length} secret
            handle
            {secrets.filter((secret) => secret.configured).length === 1
              ? ""
              : "s"}
          </span>
        </div>

        {liveCatalogs.length ? (
          <div className="setup-model-form">
            <Field label="Provider">
              <select
                value={selected?.provider ?? ""}
                onChange={(event) =>
                  onProviderChange(event.currentTarget.value)
                }
              >
                {liveCatalogs.map((catalog) => (
                  <option
                    value={catalog.provider}
                    key={catalog.provider}
                  >
                    {providerLabel(catalog.provider)}
                  </option>
                ))}
              </select>
            </Field>
            <Field
              label="Model"
              hint={
                selected?.error ??
                `${selected?.source ?? "runtime"} catalog`
              }
            >
              <select
                value={model}
                disabled={!models.length}
                onChange={(event) =>
                  onModelChange(event.currentTarget.value)
                }
              >
                {models.length ? (
                  models.map((candidate) => (
                    <option value={candidate} key={candidate}>
                      {candidate}
                    </option>
                  ))
                ) : (
                  <option value="">No models reported</option>
                )}
              </select>
            </Field>

            {selected ? (
              <div className="setup-provider-evidence">
                <StatusPill
                  state={selected.ok ? "healthy" : "blocked"}
                >
                  {selected.ok ? "Catalog ready" : "Unavailable"}
                </StatusPill>
                <span>
                  {isLocalProvider(selected.provider) ? (
                    <Cpu size={15} aria-hidden="true" />
                  ) : (
                    <Cloud size={15} aria-hidden="true" />
                  )}
                  {isLocalProvider(selected.provider)
                    ? "Local endpoint"
                    : "External provider"}
                </span>
                <span>
                  {needsCredential
                    ? `Credential ${selected.api_key_env} is missing`
                    : selected.api_key_env
                      ? "Secret Broker handle available"
                      : "No provider key required"}
                </span>
              </div>
            ) : null}

            <div className="setup-stage-actions">
              {needsCredential ? (
                <Button
                  variant="secondary"
                  onClick={
                    nativeCredentialEntry
                      ? () => onStoreCredential(selected!.provider)
                      : onOpenProviderSettings
                  }
                >
                  <KeyRound size={16} aria-hidden="true" />
                  {nativeCredentialEntry
                    ? "Add provider credential"
                    : "Open Provider settings"}
                </Button>
              ) : null}
              <Button
                variant="primary"
                pending={pending}
                disabled={!canUseSelection}
                onClick={() =>
                  selected &&
                  onUseSelection({
                    expectedRevision: runtime.expectedRevision,
                    provider: selected.provider,
                    model,
                    baseUrl:
                      selected.provider === runtime.provider
                        ? runtime.baseUrl
                        : providerBaseUrls[selected.provider] ?? null,
                    apiKeyEnv: selected.api_key_env ?? null,
                  })
                }
              >
                Use selected model
              </Button>
            </div>
          </div>
        ) : (
          <EmptyState
            title="No live model catalogs are available"
            icon={<Cpu size={22} />}
            headingLevel={3}
            actions={
              <Button
                variant="secondary"
                onClick={onOpenProviderSettings}
              >
                Open Provider settings
              </Button>
            }
          >
            Demo remains available. Local and LAN models will appear when
            the runtime reports verified candidates.
          </EmptyState>
        )}
      </section>
    </div>
  );
}

function isLocalProvider(provider: string): boolean {
  return (
    provider === "ollama" ||
    provider === "lm-studio" ||
    provider === "openai-compatible"
  );
}

function providerLabel(provider: string): string {
  return provider
    .split("-")
    .map((part) => part.charAt(0).toUpperCase() + part.slice(1))
    .join(" ");
}
