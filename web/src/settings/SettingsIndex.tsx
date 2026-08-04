import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { ActionError, Skeleton } from "../components";
import { fetchSettingsProjection } from "./api";
import { SettingControl } from "./SettingControl";
import { filterSettings, SettingsSearch } from "./SettingsSearch";
import type { ProjectedSetting, SettingsProjection } from "./types";
import "./settings.css";

export function SettingsIndex() {
  const [projection, setProjection] = useState<SettingsProjection | null>(
    null,
  );
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [query, setQuery] = useState("");
  const requestRef = useRef(0);

  const load = useCallback(async () => {
    const generation = requestRef.current + 1;
    requestRef.current = generation;
    setLoading(true);
    setError(null);
    try {
      const next = await fetchSettingsProjection();
      if (requestRef.current === generation) {
        setProjection(next);
      }
    } catch (loadError) {
      if (requestRef.current === generation) {
        setError(
          loadError instanceof Error
            ? loadError.message
            : String(loadError),
        );
      }
    } finally {
      if (requestRef.current === generation) {
        setLoading(false);
      }
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const items = useMemo(() => projection?.items ?? [], [projection]);
  const matches = useMemo(
    () => filterSettings(items, query),
    [items, query],
  );
  const grouped = useMemo(() => {
    const groups = new Map<string, ProjectedSetting[]>();
    const categoryOrder = projection?.categories ?? [];
    for (const category of categoryOrder) {
      groups.set(category, []);
    }
    for (const setting of matches) {
      const bucket = groups.get(setting.category) ?? [];
      bucket.push(setting);
      groups.set(setting.category, bucket);
    }
    return [...groups.entries()].filter(([, settings]) => settings.length);
  }, [matches, projection]);

  const replaceSetting = useCallback(
    (committed: ProjectedSetting, revokedApprovals: number) => {
      setProjection((current) => {
        if (!current) return current;
        const items = current.items.map((item) =>
          item.id === committed.id ? committed : item,
        );
        return {
          ...current,
          revision: committed.revision ?? current.revision,
          items,
          items_by_id: {
            ...current.items_by_id,
            [committed.id]: committed,
          },
        };
      });
      void revokedApprovals;
    },
    [],
  );

  const focusSetting = useCallback((settingId: string) => {
    const target = document.querySelector(
      `[data-setting-id="${CSS.escape(settingId)}"]`,
    );
    if (target instanceof HTMLElement) {
      target.scrollIntoView({ block: "center" });
      const control = target.querySelector<HTMLElement>(
        "button, input, select, [tabindex]",
      );
      control?.focus();
    }
  }, []);

  if (loading && !projection) {
    return (
      <section className="settings-index" aria-label="Effective settings">
        <Skeleton lines={4} />
      </section>
    );
  }

  if (error && !projection) {
    return (
      <section className="settings-index" aria-label="Effective settings">
        <ActionError message={error} onDismiss={() => void load()} />
      </section>
    );
  }

  return (
    <section className="settings-index" aria-label="Effective settings">
      <SettingsSearch
        query={query}
        onQueryChange={setQuery}
        results={query.trim() ? matches : []}
        onSelect={focusSetting}
      />
      {projection && (
        <p className="settings-index-summary">
          {projection.counts.total} settings ·{" "}
          {projection.counts.blocked} blocked ·{" "}
          {projection.counts.restart_required} need restart
        </p>
      )}
      {grouped.map(([category, settings]) => (
        <section
          key={category}
          className="settings-category"
          aria-label={category}
        >
          <h3 className="settings-category-title">{category}</h3>
          {settings.map((setting) => (
            <SettingControl
              key={setting.id}
              setting={setting}
              onCommitted={replaceSetting}
            />
          ))}
        </section>
      ))}
      {matches.length === 0 && query.trim() !== "" && (
        <p className="settings-search-empty" role="status">
          No settings match “{query.trim()}”.
        </p>
      )}
    </section>
  );
}
