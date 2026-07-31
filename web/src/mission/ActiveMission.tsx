import {
  Activity,
  Check,
  Circle,
  MessageCircle,
  Plus,
  Send,
  Users,
  XCircle,
} from "lucide-react";
import {
  useMemo,
  useState,
  type FormEvent,
  type ReactNode,
} from "react";
import {
  Button,
  Card,
  Notice,
  StatusPill,
} from "../components";
import { activityItemsForEvents } from "../runActivity";
import type {
  Approval,
  Run,
  TaskGraph,
  TraceEvent,
} from "../types";
import { ApprovalQueue } from "./ApprovalQueue";
import { EvidenceDrawer } from "./EvidenceDrawer";
import type { MissionState } from "./types";

export function ActiveMission({
  missionState,
  run,
  taskGraph,
  approvals,
  events,
  onDecision,
  onContinue,
  onNewMission,
  onOpenHistory,
  onAuthRequired,
  children,
}: {
  missionState: MissionState;
  run: Run;
  taskGraph: TaskGraph | null;
  approvals: Approval[];
  events: TraceEvent[];
  onDecision: (
    approval: Approval,
    approved: boolean,
  ) => void | Promise<void>;
  onContinue: (message: string) => Promise<void>;
  onNewMission: () => void;
  onOpenHistory: () => void;
  onAuthRequired?: () => void;
  children?: ReactNode;
}) {
  const [followUp, setFollowUp] = useState("");
  const [sending, setSending] = useState(false);
  const [followUpError, setFollowUpError] = useState<
    string | null
  >(null);
  const [pendingApprovalId, setPendingApprovalId] = useState<
    string | null
  >(null);
  const activity = useMemo(
    () => activityItemsForEvents(events),
    [events],
  );
  const runApprovals = approvals.filter(
    (approval) => approval.run_id === run.run_id,
  );

  async function submit(event: FormEvent) {
    event.preventDefault();
    const message = followUp.trim();
    if (!message || sending) return;
    setSending(true);
    setFollowUpError(null);
    try {
      await onContinue(message);
      setFollowUp("");
    } catch (error) {
      if (isAuthError(error)) {
        onAuthRequired?.();
      }
      // Preserve the owner draft so it can be retried unchanged.
      setFollowUpError(
        `Follow-up could not be sent: ${errorMessage(error)}`,
      );
    } finally {
      setSending(false);
    }
  }

  async function decideApproval(
    approval: Approval,
    approved: boolean,
  ) {
    if (pendingApprovalId) return;
    setPendingApprovalId(approval.approval_id);
    try {
      await onDecision(approval, approved);
    } finally {
      setPendingApprovalId(null);
    }
  }

  return (
    <section
      className={`active-mission is-${missionState}`}
      data-mission-state={missionState}
      aria-labelledby="active-mission-heading"
    >
      <header className="active-mission-head">
        <div>
          <p className="page-eyebrow">
            {stateEyebrow(missionState)}
          </p>
          <h1 id="active-mission-heading">
            {stateHeading(missionState)}
          </h1>
          <p>{run.message}</p>
        </div>
        <div className="active-mission-actions">
          <StatusPill state={runStatusState(run.status)}>
            {run.status.replaceAll("_", " ")}
          </StatusPill>
          <Button
            variant="quiet"
            size="small"
            onClick={onOpenHistory}
          >
            History
          </Button>
          <Button
            variant="secondary"
            size="small"
            onClick={onNewMission}
          >
            <Plus size={14} aria-hidden="true" />
            New mission
          </Button>
        </div>
      </header>

      <Card
        className="mission-conversation-card"
        title="Mission conversation"
        headingLevel={2}
        icon={<MessageCircle size={18} />}
      >
        <div className="mission-conversation-transcript">
          <article className="is-owner">
            <strong>You</strong>
            <p>{run.message}</p>
          </article>
          <article className="is-kestrel">
            <strong>Kestrel</strong>
            <p>
              {run.assistant_message ||
                activeStatusCopy(run.status)}
            </p>
          </article>
        </div>
        <form
          className="mission-follow-up"
          onSubmit={(event) => void submit(event)}
        >
          <label>
            <span>Continue mission conversation</span>
            <textarea
              aria-label="Continue mission conversation"
              value={followUp}
              rows={2}
              placeholder="Clarify the acceptance criteria or answer Kestrel"
              onChange={(event) =>
                setFollowUp(event.currentTarget.value)
              }
            />
          </label>
          <Button
            variant="primary"
            pending={sending}
            disabled={!followUp.trim()}
            type="submit"
          >
            <Send size={14} aria-hidden="true" />
            Send follow-up
          </Button>
          {followUpError ? (
            <Notice
              variant="danger"
              title="Follow-up could not be sent"
            >
              {followUpError}
            </Notice>
          ) : null}
        </form>
      </Card>

      <section
        className="mission-worker-board"
        aria-labelledby="mission-worker-heading"
      >
        <header>
          <Users size={18} aria-hidden="true" />
          <div>
            <h2 id="mission-worker-heading">
              Plan and worker activity
            </h2>
            <p>
              Current task and subagent states from the durable task
              graph.
            </p>
          </div>
        </header>
        <div className="mission-worker-grid">
          {(taskGraph?.tasks ?? []).map((task) => (
            <article key={task.task_id}>
              <div>
                <strong>{task.title}</strong>
                <StatusPill state={taskStatusState(task.status)}>
                  {task.status}
                </StatusPill>
              </div>
              <p>{task.goal}</p>
              <small>
                {task.profile} ·{" "}
                {task.acceptance_criteria?.join("; ") ||
                  "evidence pending"}
              </small>
            </article>
          ))}
          {(taskGraph?.subagents ?? []).map((worker) => (
            <article key={worker.subagent_id}>
              <div>
                <strong>{worker.goal}</strong>
                <StatusPill
                  state={taskStatusState(worker.status)}
                >
                  {worker.status}
                </StatusPill>
              </div>
              <p>{worker.profile} · {worker.status}</p>
              <small>
                {worker.task_id
                  ? `Task ${worker.task_id}`
                  : "Run-level worker"}
              </small>
            </article>
          ))}
          {!taskGraph?.tasks.length &&
          !taskGraph?.subagents.length ? (
            <p className="mission-empty-copy">
              The durable task graph has not been published yet.
            </p>
          ) : null}
        </div>
      </section>

      <ApprovalQueue
        approvals={runApprovals}
        pendingApprovalId={pendingApprovalId}
        onDecision={decideApproval}
      />

      <section
        className="mission-timeline"
        aria-labelledby="mission-timeline-heading"
      >
        <header className="mission-section-rule">
          <div>
            <p className="page-eyebrow">Durable activity</p>
            <h2 id="mission-timeline-heading">Run timeline</h2>
          </div>
        </header>
        <ol className="mission-timeline-list">
          {activity.map((item) => (
            <li key={item.id} className={item.status}>
              <span>
                {item.status === "failed" ? (
                  <XCircle size={15} />
                ) : item.status === "completed" ? (
                  <Check size={15} />
                ) : (
                  <Activity size={15} />
                )}
              </span>
              <div>
                <strong>{item.label}</strong>
                <p>
                  {item.detail ||
                    item.meta ||
                    "Durable event recorded."}
                </p>
              </div>
              <small>{item.status}</small>
            </li>
          ))}
          <li
            className={
              run.status === "completed" ? "completed" : "info"
            }
          >
            <span>
              {run.status === "completed" ? (
                <Check size={15} />
              ) : (
                <Circle size={15} />
              )}
            </span>
            <div>
              <strong>
                {run.status === "completed"
                  ? "Final review"
                  : "Proof and review"}
              </strong>
              <p>
                {taskGraph?.tasks.length
                  ? `${taskGraph.tasks.filter((task) => task.status === "completed").length} of ${taskGraph.tasks.length} planned tasks completed.`
                  : "Validation evidence will appear here."}
              </p>
            </div>
            <small>{run.status}</small>
          </li>
        </ol>
      </section>

      {children}

      <EvidenceDrawer
        title="Mission evidence"
        records={[
          { label: "Run", value: run },
          ...(taskGraph
            ? [{ label: "Task graph", value: taskGraph }]
            : []),
          ...(events.length
            ? [{ label: "Trace events", value: events }]
            : []),
        ]}
      />
    </section>
  );
}

function stateEyebrow(state: MissionState): string {
  if (state === "needs-owner") return "Owner checkpoint";
  if (state === "completed") return "Mission complete";
  if (state === "blocked") return "Recovery needed";
  if (state === "reviewing") return "Reviewing evidence";
  return "Mission in flight";
}

function stateHeading(state: MissionState): string {
  if (state === "needs-owner") {
    return "Mission needs your decision";
  }
  if (state === "completed") return "Mission completed";
  if (state === "blocked") return "Mission is blocked";
  if (state === "reviewing") return "Mission is under review";
  return "Mission is in progress";
}

function activeStatusCopy(status: string): string {
  if (status === "queued") return "The mission is queued.";
  if (status === "running") return "Kestrel is working.";
  if (status === "blocked") {
    return "Kestrel needs an owner decision or recovery action.";
  }
  if (status === "failed") return "The mission failed.";
  if (status === "completed") {
    return "The mission completed with recorded evidence.";
  }
  return "Current mission state is recorded above.";
}

function runStatusState(status: string) {
  if (status === "completed") return "healthy" as const;
  if (status === "blocked" || status === "failed") {
    return "blocked" as const;
  }
  if (status === "running" || status === "queued") {
    return "waiting" as const;
  }
  return "inactive" as const;
}

function taskStatusState(status: string) {
  if (status === "completed") return "healthy" as const;
  if (status === "blocked" || status === "failed") {
    return "blocked" as const;
  }
  if (
    status === "running" ||
    status === "queued" ||
    status === "waiting"
  ) {
    return "waiting" as const;
  }
  return "inactive" as const;
}

function errorMessage(value: unknown): string {
  return value instanceof Error ? value.message : String(value);
}

function isAuthError(value: unknown): boolean {
  return value instanceof Error && value.name === "ApiAuthError";
}
