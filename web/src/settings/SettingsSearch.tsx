import { Search } from "lucide-react";
import { InlineMeta } from "../components";
import { formatBlocker, type ProjectedSetting } from "./types";

export function filterSettings(
  settings: readonly ProjectedSetting[],
  query: string,
): ProjectedSetting[] {
  const normalized = query.trim().toLocaleLowerCase().replace(/[_.]/g, " ");
  if (!normalized) return [...settings];
  return settings.filter((setting) =>
    [
      setting.id,
      setting.key ?? "",
      setting.category,
      setting.type,
      ...setting.blockers.map(formatBlocker),
    ]
      .join(" ")
      .toLocaleLowerCase()
      .replace(/[_.]/g, " ")
      .includes(normalized),
  );
}

export function SettingsSearch({
  query,
  onQueryChange,
  results,
  onSelect,
}: {
  query: string;
  onQueryChange: (value: string) => void;
  results: ProjectedSetting[];
  onSelect: (settingId: string) => void;
}) {
  return (
    <div className="settings-search">
      <label className="settings-search-field">
        <Search size={15} aria-hidden="true" />
        <span className="sr-only">Search settings</span>
        <input
          type="search"
          value={query}
          placeholder="Search settings or feature surfaces…"
          onChange={(event) => onQueryChange(event.target.value)}
        />
      </label>
      <div className="settings-search-results" aria-live="polite">
        {results.length === 0 && query.trim() !== "" ? (
          <p className="settings-search-empty" role="status">
            No settings match “{query.trim()}”.
          </p>
        ) : (
          results.map((setting) => (
            <button
              key={setting.id}
              type="button"
              className="settings-search-hit"
              onClick={() => onSelect(setting.id)}
            >
              <strong>{setting.id}</strong>
              <InlineMeta
                items={[
                  setting.category,
                  setting.blockers.length > 0 ? "Blocked" : null,
                  setting.writable ? null : "Read-only",
                ]}
              />
            </button>
          ))
        )}
      </div>
    </div>
  );
}
