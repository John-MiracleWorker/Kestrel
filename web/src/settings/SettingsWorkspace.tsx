import { Check, RefreshCw, Sprout } from "lucide-react";
import type { ReactNode } from "react";
import { ActionError } from "../components";
import type { SettingsWorkspaceController } from "./settingsController";

export function SettingsWorkspace({
  controller,
  error,
  notice,
  onDismissError,
  onOpenAdvanced,
  onOpenSetup,
  onRefresh,
  subroute,
  setupCenter,
  children,
}: {
  controller: SettingsWorkspaceController;
  error: string | null;
  notice: string | null;
  onDismissError: () => void;
  onOpenAdvanced: () => void;
  onOpenSetup: () => void;
  onRefresh: () => Promise<void>;
  subroute?: string;
  setupCenter: ReactNode;
  children: ReactNode;
}) {
  if (subroute === "setup") {
    return (
      <section
        id="settings"
        className="shell page-shell settings-page settings-setup-page"
        data-section="settings"
        aria-label="Settings"
      >
        {setupCenter}
      </section>
    );
  }

  return (
    <section
      id="settings"
      className="shell page-shell settings-page"
      data-section="settings"
      aria-label="Settings"
    >
      <header className="page-head">
        <div>
          <p className="page-eyebrow">Configuration</p>
          <h1 className="page-title">
            Settings<em>.</em>
          </h1>
          <p className="page-subtitle">
            The everyday surface for Kestrel: identity, provider, memory,
            channels, secrets, and permissions. Deep runtime controls stay
            one click away in Advanced.
          </p>
        </div>
        <div className="page-actions">
          <button
            className="btn subtle"
            type="button"
            onClick={onOpenSetup}
          >
            <Sprout size={15} /> Setup Center
          </button>
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
            onClick={() => {
              void controller.saveRuntimeSettings();
            }}
          >
            <Check size={15} /> Save Settings
          </button>
          <button
            className="btn subtle"
            type="button"
            onClick={onOpenAdvanced}
          >
            Open Advanced
          </button>
        </div>
      </header>
      {notice && (
        <div className="announcer page-notice" aria-live="polite">
          {notice}
        </div>
      )}
      {error && (
        <ActionError message={error} onDismiss={onDismissError} />
      )}
      {children}
    </section>
  );
}
