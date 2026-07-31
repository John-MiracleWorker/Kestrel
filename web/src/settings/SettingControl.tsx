import { FolderOpen } from "lucide-react";
import { useState, type FormEvent } from "react";
import { Disclosure, InlineMeta, JsonBlock, Notice } from "../components";
import { readDesktopBridge } from "../platform/desktopBridge";
import { commitSettingValue } from "./api";
import {
  formatApplies,
  formatBlocker,
  isSettingConflict,
  type ProjectedSetting,
} from "./types";

type SaveState =
  | { kind: "idle" }
  | { kind: "saving" }
  | { kind: "conflict" }
  | { kind: "error"; message: string };

export function SettingControl({
  setting,
  onCommitted,
}: {
  setting: ProjectedSetting;
  onCommitted: (setting: ProjectedSetting, revokedApprovals: number) => void;
}) {
  // The renderer is never an authority source: the displayed value is the
  // last server-committed projection. Optimistic UI may show "Saving…"
  // but the control state only changes when the server returns truth.
  const [current, setCurrent] = useState<ProjectedSetting>(setting);
  const [state, setState] = useState<SaveState>({ kind: "idle" });
  const blocked = current.blockers.length > 0;
  const saving = state.kind === "saving";
  const configuredBoolean = current.configured_value === true;
  const effectiveBoolean = current.effective_value === true;

  async function save(value: unknown) {
    setState({ kind: "saving" });
    try {
      const result = await commitSettingValue(current, value);
      setState({ kind: "idle" });
      setCurrent(result.setting);
      onCommitted(result.setting, result.revoked_approvals);
    } catch (error) {
      const conflict = isSettingConflict(error);
      if (conflict) {
        setState({ kind: "conflict" });
        // Recover with server truth: the committed projection replaces the
        // renderer's copy; no optimistic value survives the conflict.
        setCurrent(conflict.current);
        onCommitted(conflict.current, 0);
        return;
      }
      setState({
        kind: "error",
        message: error instanceof Error ? error.message : String(error),
      });
    }
  }

  return (
    <article
      className="setting-control"
      data-setting-id={current.id}
      data-blocked={blocked ? "true" : "false"}
    >
      <header className="setting-control-head">
        <div>
          <strong>{setting.id}</strong>
          <InlineMeta
            items={[
              current.category,
              formatApplies(current),
              `Source: ${current.provenance}`,
            ]}
          />
        </div>
        {!current.writable && (
          <span className="setting-readonly-badge">Read-only evidence</span>
        )}
      </header>

      <div className="setting-control-editor">
        <SettingEditor
          setting={current}
          disabled={saving || !current.writable}
          onSave={save}
        />
      </div>

      {current.type === "boolean" && (
        <p className="setting-truth">
          Configured {configuredBoolean ? "on" : "off"}
          {blocked
            ? " · effective off"
            : effectiveBoolean !== configuredBoolean
              ? ` · effective ${effectiveBoolean ? "on" : "off"}`
              : ""}
          {saving ? " · Saving…" : ""}
        </p>
      )}

      {blocked && (
        <Notice variant="caution" title="Currently blocked">
          <ul className="setting-blockers">
            {current.blockers.map((blocker) => (
              <li key={blocker}>{formatBlocker(blocker)}</li>
            ))}
          </ul>
        </Notice>
      )}

      {state.kind === "conflict" && (
        <Notice variant="caution" title="Revision conflict">
          Changed elsewhere; review the current value
        </Notice>
      )}
      {state.kind === "error" && (
        <Notice variant="danger" title="Save failed">
          {state.message}
        </Notice>
      )}

      <footer className="setting-control-meta">
        <InlineMeta
          items={[
            current.authority_impact === "grants_authority"
              ? "Grants authority"
              : "No authority impact",
            current.privacy_impact !== "none"
              ? `Privacy: ${current.privacy_impact.replace(/_/g, " ")}`
              : null,
            current.requires_approval
              ? "Invocations still require approval"
              : null,
            current.undo_available ? "Undo available" : "No undo",
          ]}
        />
        <Disclosure title="Setting evidence">
          <JsonBlock value={current} maxHeight="200px" />
        </Disclosure>
      </footer>
    </article>
  );
}

function SettingEditor({
  setting,
  disabled,
  onSave,
}: {
  setting: ProjectedSetting;
  disabled: boolean;
  onSave: (value: unknown) => Promise<void>;
}) {
  const configured = setting.configured_value;

  if (setting.type === "boolean") {
    const checked = configured === true;
    return (
      <button
        type="button"
        role="switch"
        aria-checked={checked}
        aria-label={`${setting.id} (${setting.category})`}
        className="setting-switch"
        disabled={disabled}
        onClick={() => {
          void onSave(!checked);
        }}
      >
        <span className="setting-switch-thumb" aria-hidden="true" />
      </button>
    );
  }

  if (setting.type === "enum" && setting.allowed_values) {
    return (
      <label className="setting-field">
        <span className="sr-only">{`${setting.id} (${setting.category})`}</span>
        <select
          aria-label={`${setting.id} (${setting.category})`}
          value={String(configured ?? "")}
          disabled={disabled}
          onChange={(event) => {
            void onSave(event.target.value);
          }}
        >
          {setting.allowed_values.map((value) => (
            <option key={value} value={value}>
              {value}
            </option>
          ))}
        </select>
      </label>
    );
  }

  if (setting.type === "number" || setting.type === "duration") {
    const range = setting.allowed_range;
    return (
      <NumberEditor
        setting={setting}
        disabled={disabled}
        onSave={onSave}
        min={range?.[0]}
        max={range?.[1]}
        suffix={setting.type === "duration" ? "seconds" : null}
      />
    );
  }

  if (setting.type === "path") {
    return (
      <PathEditor setting={setting} disabled={disabled} onSave={onSave} />
    );
  }

  // string and any read-only evidence types
  if (!setting.writable) {
    return (
      <output
        aria-label={`${setting.id} (${setting.category})`}
        className="setting-evidence"
      >
        {String(configured ?? "—")}
      </output>
    );
  }
  return (
    <TextEditor setting={setting} disabled={disabled} onSave={onSave} />
  );
}

function NumberEditor({
  setting,
  disabled,
  onSave,
  min,
  max,
  suffix,
}: {
  setting: ProjectedSetting;
  disabled: boolean;
  onSave: (value: unknown) => Promise<void>;
  min?: number;
  max?: number;
  suffix: string | null;
}) {
  const [draft, setDraft] = useState(String(setting.configured_value ?? ""));
  return (
    <form
      className="setting-inline-form"
      onSubmit={(event: FormEvent) => {
        event.preventDefault();
        const numeric = Number(draft);
        if (!Number.isFinite(numeric)) return;
        void onSave(numeric);
      }}
    >
      <input
        type="number"
        aria-label={`${setting.id} (${setting.category})`}
        value={draft}
        min={min}
        max={max}
        step="any"
        disabled={disabled}
        onChange={(event) => setDraft(event.target.value)}
      />
      {suffix && <span className="setting-suffix">{suffix}</span>}
      <button type="submit" className="btn subtle" disabled={disabled}>
        Save
      </button>
    </form>
  );
}

function TextEditor({
  setting,
  disabled,
  onSave,
}: {
  setting: ProjectedSetting;
  disabled: boolean;
  onSave: (value: unknown) => Promise<void>;
}) {
  const [draft, setDraft] = useState(String(setting.configured_value ?? ""));
  return (
    <form
      className="setting-inline-form"
      onSubmit={(event: FormEvent) => {
        event.preventDefault();
        if (!draft.trim()) return;
        void onSave(draft.trim());
      }}
    >
      <input
        type="text"
        aria-label={`${setting.id} (${setting.category})`}
        value={draft}
        disabled={disabled}
        onChange={(event) => setDraft(event.target.value)}
      />
      <button type="submit" className="btn subtle" disabled={disabled}>
        Save
      </button>
    </form>
  );
}

function PathEditor({
  setting,
  disabled,
  onSave,
}: {
  setting: ProjectedSetting;
  disabled: boolean;
  onSave: (value: unknown) => Promise<void>;
}) {
  const [draft, setDraft] = useState(String(setting.configured_value ?? ""));
  const desktop =
    typeof globalThis !== "undefined" &&
    Object.prototype.hasOwnProperty.call(globalThis, "kestrelDesktop");

  async function chooseFolder() {
    const bridge = readDesktopBridge();
    if (!bridge) return;
    const result = (await bridge.chooseProjectFolder()) as {
      path?: unknown;
    } | null;
    if (result && typeof result.path === "string" && result.path.trim()) {
      setDraft(result.path);
    }
  }

  return (
    <form
      className="setting-inline-form"
      onSubmit={(event: FormEvent) => {
        event.preventDefault();
        if (!draft.trim()) return;
        void onSave(draft.trim());
      }}
    >
      <input
        type="text"
        aria-label={`${setting.id} (${setting.category})`}
        value={draft}
        disabled={disabled}
        onChange={(event) => setDraft(event.target.value)}
      />
      {desktop && (
        <button
          type="button"
          className="btn subtle"
          disabled={disabled}
          aria-label={`Choose folder for ${setting.id}`}
          onClick={() => {
            void chooseFolder().catch(() => undefined);
          }}
        >
          <FolderOpen size={14} aria-hidden="true" /> Browse
        </button>
      )}
      <button type="submit" className="btn subtle" disabled={disabled}>
        Save
      </button>
    </form>
  );
}
