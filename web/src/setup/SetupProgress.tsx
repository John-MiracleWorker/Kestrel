import {
  Check,
  Circle,
  CircleAlert,
  CircleMinus,
} from "lucide-react";
import type {
  SetupStageId,
  SetupStageState,
} from "./types";

export type SetupProgressItem = {
  id: SetupStageId;
  label: string;
  state: SetupStageState;
};

export function SetupProgress({
  items,
  onSelect,
}: {
  items: SetupProgressItem[];
  onSelect: (stage: SetupStageId) => void;
}) {
  return (
    <nav className="setup-progress" aria-label="Setup progress">
      <ol>
        {items.map((item, index) => {
          const disabled = item.state === "upcoming";
          const Icon =
            item.state === "complete"
              ? Check
              : item.state === "skipped"
                ? CircleMinus
              : item.state === "attention"
                ? CircleAlert
                : Circle;
          return (
            <li
              key={item.id}
              className={`setup-progress-item is-${item.state}`}
            >
              <button
                type="button"
                aria-current={
                  item.state === "current" ||
                  item.state === "attention"
                    ? "step"
                    : undefined
                }
                disabled={disabled}
                onClick={() => onSelect(item.id)}
              >
                <span className="setup-progress-marker" aria-hidden="true">
                  <Icon size={16} />
                </span>
                <span className="setup-progress-copy">
                  <small>
                    Step {index + 1}
                    {item.state === "skipped" ? " · skipped" : ""}
                  </small>
                  <strong>{item.label}</strong>
                </span>
              </button>
            </li>
          );
        })}
      </ol>
    </nav>
  );
}
