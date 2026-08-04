import {
  Brain,
  Check,
  MessageCircle,
  PanelRightOpen,
  RefreshCw,
  Send,
  Settings,
  Sparkles,
  Wrench,
  X,
} from "lucide-react";
import {
  type FormEvent,
  type ReactNode,
  useEffect,
  useRef,
  useState,
} from "react";
import ReactMarkdown, { type Components } from "react-markdown";
import remarkGfm from "remark-gfm";
import {
  ActionError,
  InlineMeta,
  JsonBlock,
  StatusBadge,
} from "../components";
import {
  activityItemsForEvents,
  assistantTextForRun,
  riskLabel,
  summarizeArguments,
  type LiveActivityItem,
} from "../runActivity";
import type { Approval, Run, TraceEvent } from "../types";

export type ConversationStatus = {
  label: string;
  detail: string;
  action?: "setup" | "model-settings";
};

export type ConversationPanelProps = {
  agentDisplayName: string;
  hasActiveThread: boolean;
  chatStatusDetail: string;
  status: ConversationStatus;
  activeRun: Run | null;
  runs: Run[];
  events: TraceEvent[];
  streamedAssistant: string;
  approvals: Approval[];
  autonomyMode: string;
  autonomyOptions: Array<{ value: string; label: string }>;
  autonomousSchedulerEnabled: boolean;
  notice: string | null;
  error: string | null;
  onAutonomyModeChange: (mode: string) => void;
  onOpenSetup: () => void;
  onOpenSettings: () => void;
  onRefresh: () => Promise<void>;
  onSubmitMessage: (message: string) => Promise<void>;
  onError: (value: unknown) => void;
  onDismissError: () => void;
  onDecideApproval: (
    approval: Approval,
    approved: boolean,
  ) => void;
  onContainer: (element: HTMLElement | null) => void;
  renderInspector: (onClose: () => void) => ReactNode;
};

const markdownComponents: Components = {
  a({ node: _node, ...props }) {
    return <a {...props} target="_blank" rel="noreferrer" />;
  },
};
const markdownPlugins = [remarkGfm];

export function ConversationPanel({
  agentDisplayName,
  hasActiveThread,
  chatStatusDetail,
  status,
  activeRun,
  runs,
  events,
  streamedAssistant,
  approvals,
  autonomyMode,
  autonomyOptions,
  autonomousSchedulerEnabled,
  notice,
  error,
  onAutonomyModeChange,
  onOpenSetup,
  onOpenSettings,
  onRefresh,
  onSubmitMessage,
  onError,
  onDismissError,
  onDecideApproval,
  onContainer,
  renderInspector,
}: ConversationPanelProps) {
  const [message, setMessage] = useState("");
  const [inspectorOpen, setInspectorOpen] = useState(false);
  const transcriptRef = useRef<HTMLDivElement | null>(null);
  const followTranscriptRef = useRef(true);

  useEffect(() => {
    const transcript = transcriptRef.current;
    if (!transcript || !followTranscriptRef.current) return;
    transcript.scrollTop = transcript.scrollHeight;
  }, [runs.length, activeRun?.status, events.length, streamedAssistant]);

  async function submitMessage(event: FormEvent) {
    event.preventDefault();
    const objective = message.trim();
    if (!objective) return;
    try {
      await onSubmitMessage(objective);
      setMessage("");
    } catch (value) {
      onError(value);
    }
  }

  return (
    <>
      <div
        className="conversation"
        id="legacy-workspace"
        ref={onContainer}
      >
        <header className="conv-head simple-conv-head" data-section="chat">
          <div>
            <h1>Ask {agentDisplayName}</h1>
            <div className="conv-meta simple-meta">
              <span>{hasActiveThread ? "Current chat" : "New chat"}</span>
              <span className="sep">·</span>
              <span>{chatStatusDetail}</span>
            </div>
          </div>
          <div className="conv-tools simple-conv-tools">
            <StatusBadge value={status.label} />
            {status.action === "setup" && (
              <button type="button" onClick={onOpenSetup}>
                <Sparkles size={15} /> Setup
              </button>
            )}
            {status.action === "model-settings" && (
              <button type="button" onClick={onOpenSettings}>
                <Settings size={15} /> Open model settings
              </button>
            )}
            {activeRun && (
              <button
                type="button"
                onClick={() => setInspectorOpen((open) => !open)}
              >
                <PanelRightOpen size={15} /> Details
              </button>
            )}
            <button
              type="button"
              onClick={() => void onRefresh().catch(onError)}
            >
              <RefreshCw size={15} /> Refresh
            </button>
          </div>
        </header>

        <div className="announcer" aria-live="polite">
          {notice}
        </div>
        {error && (
          <ActionError message={error} onDismiss={onDismissError} />
        )}

        <section
          className={`conversation-layout ${
            inspectorOpen ? "with-inspector" : ""
          }`}
          data-section="chat"
        >
          <div className="transcript-inner">
            <div
              className="transcript"
              role="region"
              aria-label="Conversation transcript"
              tabIndex={0}
              ref={transcriptRef}
              onScroll={(event) => {
                const transcript = event.currentTarget;
                const distanceFromBottom =
                  transcript.scrollHeight -
                  transcript.scrollTop -
                  transcript.clientHeight;
                followTranscriptRef.current = distanceFromBottom < 96;
              }}
            >
              {runs.length === 0 ? (
                <div className="empty-state">
                  <MessageCircle size={28} />
                  <h2>Tell {agentDisplayName} what to do.</h2>
                  <p>
                    Start with a build, fix, research, inspection, or
                    continuation request. {agentDisplayName} will keep the work
                    in this thread.
                  </p>
                </div>
              ) : (
                runs.map((run) => (
                  <div className="turn" key={run.run_id}>
                    <article className="msg user">
                      <strong>You</strong>
                      <p>{run.message}</p>
                    </article>
                    <article className="msg kestrel">
                      <strong>Kestrel</strong>
                      <MarkdownMessage
                        text={assistantTextForRun(
                          run,
                          activeRun?.run_id,
                          streamedAssistant,
                        )}
                      />
                      {run.run_id === activeRun?.run_id && (
                        <LiveRunActivity run={run} events={events} />
                      )}
                    </article>
                  </div>
                ))
              )}
              {approvals.map((approval) => (
                <ApprovalCardInline
                  key={approval.approval_id}
                  approval={approval}
                  onApprove={onDecideApproval}
                />
              ))}
            </div>
            <form className="composer" onSubmit={submitMessage}>
              <label className="composer-field">
                <span>Ask {agentDisplayName}</span>
                <textarea
                  value={message}
                  onChange={(event) => setMessage(event.target.value)}
                  placeholder={`Ask ${agentDisplayName} to build, fix, research, inspect, or continue something...`}
                  rows={3}
                />
              </label>
              <div className="composer-bar">
                <label className="mode-select">
                  <span>Mode</span>
                  <select
                    value={autonomyMode}
                    onChange={(event) =>
                      onAutonomyModeChange(event.target.value)
                    }
                  >
                    {autonomyOptions
                      .filter(
                        (option) =>
                          option.value !== "autonomous" ||
                          autonomousSchedulerEnabled,
                      )
                      .map((option) => (
                        <option key={option.value} value={option.value}>
                          {option.label}
                        </option>
                      ))}
                  </select>
                </label>
                <button type="submit" disabled={!message.trim()}>
                  <Send size={15} /> Send
                </button>
              </div>
            </form>
          </div>
        </section>
      </div>
      {inspectorOpen ? renderInspector(() => setInspectorOpen(false)) : null}
    </>
  );
}

function ApprovalCardInline({
  approval,
  onApprove,
}: {
  approval: Approval;
  onApprove: (approval: Approval, approved: boolean) => void;
}) {
  return (
    <div
      className="approval-card inline-approval"
      role="group"
      aria-label={`Approval for ${approval.tool_name}`}
    >
      <div>
        <span className="progress-chip">Needs approval</span>
        <strong>{approval.tool_name}</strong>
        <InlineMeta
          items={[
            riskLabel(approval.risk),
            summarizeArguments(approval.arguments),
          ]}
        />
      </div>
      <details>
        <summary>View raw JSON</summary>
        <JsonBlock value={approval.arguments} maxHeight="160px" />
      </details>
      <div className="page-actions">
        <button type="button" onClick={() => onApprove(approval, true)}>
          <Check size={15} /> Approve
        </button>
        <button
          type="button"
          className="btn danger"
          onClick={() => onApprove(approval, false)}
        >
          <X size={15} /> Deny
        </button>
      </div>
    </div>
  );
}

function MarkdownMessage({ text }: { text: string }) {
  return (
    <div className="markdown-message">
      <ReactMarkdown
        remarkPlugins={markdownPlugins}
        components={markdownComponents}
      >
        {text}
      </ReactMarkdown>
    </div>
  );
}

function LiveRunActivity({
  run,
  events,
}: {
  run: Run;
  events: TraceEvent[];
}) {
  const items = activityItemsForEvents(events);
  const isRunning = run.status === "queued" || run.status === "running";
  if (items.length === 0 && !isRunning) return null;
  return (
    <div
      className="activity"
      role="status"
      aria-label="Live run activity"
      aria-live="polite"
    >
      <div className="act-heading">
        <Brain size={15} />
        <strong>Thinking</strong>
      </div>
      {items.map((item) => (
        <div
          className={`act-row ${
            item.status === "completed"
              ? "done"
              : item.status === "running"
                ? "run"
                : item.status === "failed"
                  ? "fail"
                  : "info"
          }`}
          key={item.id}
        >
          <span className="act-icon" aria-hidden="true">
            {activityIcon(item)}
          </span>
          <span className="text">
            <strong>{item.label}</strong>
            {item.meta && <code>{item.meta}</code>}
            {item.detail && <span className="detail">{item.detail}</span>}
          </span>
        </div>
      ))}
      {isRunning && <TypingIndicator />}
    </div>
  );
}

function TypingIndicator() {
  return (
    <div className="typing" aria-label="Kestrel is responding">
      <span>Working</span>
      <span className="dots" aria-hidden="true">
        <span />
        <span />
        <span />
      </span>
    </div>
  );
}

function activityIcon(item: LiveActivityItem) {
  if (item.status === "completed") return <Check size={14} />;
  if (item.status === "failed") return <X size={14} />;
  if (item.kind === "tool") return <Wrench size={14} />;
  return <Sparkles size={14} />;
}
