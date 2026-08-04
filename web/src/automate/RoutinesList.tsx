import { CalendarClock, Pencil, Trash2 } from "lucide-react";
import { EmptyState, Panel, StatusBadge } from "../components";
import type { Routine } from "../types";

export type RoutinesListProps = {
  routines: Routine[];
  loading: boolean;
  selectedRoutineId: string | null;
  uncertainRoutineIds: Set<string>;
  mutationPending: boolean;
  onChoose: (routine: Routine) => void;
  onToggle: (routine: Routine) => void;
  onEdit: (routine: Routine) => void;
  onDelete: (routine: Routine) => void;
  scheduleLabel: (routine: Routine) => string;
};

export function RoutinesList({
  routines,
  loading,
  selectedRoutineId,
  uncertainRoutineIds,
  mutationPending,
  onChoose,
  onToggle,
  onEdit,
  onDelete,
  scheduleLabel
}: RoutinesListProps) {
  return (
    <Panel
      id="routine-definitions"
      title="Routines list"
      icon={<CalendarClock size={19} />}
      actions={<StatusBadge value={loading ? "loading" : `${routines.length} total`} />}
    >
      <div className="routine-list">
        {routines.map((routine) => (
          <article
            className={`routine-card ${routine.routine_id === selectedRoutineId ? "selected" : ""}`}
            key={routine.routine_id}
          >
            <button type="button" className="routine-select" onClick={() => onChoose(routine)}>
              <span>
                <strong>{routine.name}</strong>
                <small>{scheduleLabel(routine)}</small>
              </span>
              <StatusBadge value={routine.enabled ? "enabled" : "paused"} />
            </button>
            <div className="routine-card-actions">
              <button
                type="button"
                aria-label={`${routine.enabled ? "Pause" : "Enable"} ${routine.name}`}
                onClick={() => onToggle(routine)}
                disabled={mutationPending || uncertainRoutineIds.has(routine.routine_id)}
              >
                {routine.enabled ? "Pause" : "Enable"}
              </button>
              <button
                type="button"
                aria-label={`Edit ${routine.name}`}
                onClick={() => onEdit(routine)}
                disabled={mutationPending || uncertainRoutineIds.has(routine.routine_id)}
              >
                <Pencil size={14} /> Edit
              </button>
              <button
                type="button"
                className="btn danger"
                aria-label={`Delete ${routine.name}`}
                onClick={() => onDelete(routine)}
                disabled={mutationPending || uncertainRoutineIds.has(routine.routine_id)}
              >
                <Trash2 size={14} /> Delete
              </button>
            </div>
          </article>
        ))}
        {!loading && routines.length === 0 && (
          <EmptyState>No routines yet. Create one to start with a disabled, reviewable definition.</EmptyState>
        )}
      </div>
    </Panel>
  );
}
