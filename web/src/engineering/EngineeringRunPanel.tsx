import {
  GitPullRequest,
  Image,
  Layers3,
  RefreshCw,
  ShieldCheck,
  Workflow
} from "lucide-react";
import { useCallback, useEffect, useMemo, useState } from "react";
import type { ReactNode } from "react";
import { getJson, postJson } from "../api";
import { InlineMeta, StatusBadge } from "../components";
import { EvidenceDrawer } from "../mission/EvidenceDrawer";
import type { TaskNode } from "../types";
import "./engineering.css";

type ApprovalPacketCall = {
  tool_call_id: string;
  tool_name: string;
  arguments: Record<string, unknown>;
  call_digest: string;
  risk: string;
  capability_revision: number;
  resource_digest: string;
  reason: string;
  resource_scope: string;
  expected_side_effect: string;
  rollback: string;
  status: string;
};

type ApprovalPacket = {
  packet_id: string;
  objective: string;
  checkpoint: string;
  packet_digest: string;
  status: string;
  authorization_record_count: number;
  calls: ApprovalPacketCall[];
};

type GraphAmendment = {
  amendment_id: string;
  operation: string;
  status: string;
  base_graph_digest: string;
  requires_approval: boolean;
  approval_reasons: string[];
  evidence_refs: string[];
};

type Candidate = {
  candidate_id: string;
  branch: string;
  status: string;
  validation_id: string | null;
  validation_passed: boolean | null;
  reviewer_identities: string[];
  changed_file_count: number | null;
  changed_line_count: number | null;
  risk_notes: string[];
  actual_cost_usd: number | null;
  latency_seconds: number | null;
};

type CandidateFanout = {
  fanout_id: string;
  source_task_id: string;
  status: string;
  selected_candidate_id: string | null;
  estimated_budget_delta_usd: number;
  candidates: Candidate[];
};

type BrowserValidation = {
  validation_id: string;
  task_id: string;
  candidate_id: string | null;
  candidate_digest: string;
  target_url: string;
  status: string;
  failure_codes: string[];
  report: {
    screenshot?: {
      data_url?: string;
      sha256?: string;
      width?: number;
      height?: number;
      bytes?: number;
    };
    dom_summary?: Record<string, unknown>;
    accessibility?: Record<string, unknown>;
    console_errors?: unknown[];
    network_errors?: unknown[];
  };
};

type GitHubChangeRequest = {
  request_id: string;
  review_id: string;
  title: string;
  base_branch: string;
  head_branch: string;
  status: string;
  request_digest: string;
  external_number: number | null;
  external_url: string | null;
  publish_tool_request: {
    tool_name: string;
    arguments: Record<string, unknown>;
  };
  sync_tool_request: {
    tool_name: string;
    arguments: Record<string, unknown>;
  };
  feedback: Array<{ event_id: string; kind: string; status: string }>;
};

type EngineeringRunPanelProps = {
  runId: string | null;
  refreshToken: string;
  tasks: TaskNode[];
  defaultBranch: string;
  onPrepareTool: (name: string, args: Record<string, unknown>) => void;
};

export function EngineeringRunPanel({
  runId,
  refreshToken,
  tasks,
  defaultBranch,
  onPrepareTool
}: EngineeringRunPanelProps) {
  const [packets, setPackets] = useState<ApprovalPacket[]>([]);
  const [amendments, setAmendments] = useState<GraphAmendment[]>([]);
  const [fanouts, setFanouts] = useState<CandidateFanout[]>([]);
  const [browserValidations, setBrowserValidations] = useState<BrowserValidation[]>([]);
  const [githubRequests, setGitHubRequests] = useState<GitHubChangeRequest[]>([]);
  const [pending, setPending] = useState(false);
  const [actionPending, setActionPending] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [prTitle, setPrTitle] = useState("");

  const review = useMemo(() => repairReviewProjection(tasks), [tasks]);

  const refresh = useCallback(async (signal?: AbortSignal) => {
    if (!runId) {
      setPackets([]);
      setAmendments([]);
      setFanouts([]);
      setBrowserValidations([]);
      setGitHubRequests([]);
      return;
    }
    setPending(true);
    setError(null);
    const encodedRun = encodeURIComponent(runId);
    try {
      const [packetResult, amendmentResult, fanoutResult, browserResult, githubResult] =
        await Promise.all([
          getJson<{ items: ApprovalPacket[] }>(`/api/runs/${encodedRun}/approval-packets`, { signal }),
          getJson<{ items: GraphAmendment[] }>(`/api/runs/${encodedRun}/graph/amendments`, { signal }),
          getJson<{ items: CandidateFanout[] }>(`/api/runs/${encodedRun}/candidate-fanouts`, { signal }),
          getJson<{ items: BrowserValidation[] }>(`/api/runs/${encodedRun}/browser-validations`, { signal }),
          getJson<{ items: GitHubChangeRequest[] }>(`/api/runs/${encodedRun}/github-change-requests`, { signal })
        ]);
      setPackets(Array.isArray(packetResult.items) ? packetResult.items : []);
      setAmendments(Array.isArray(amendmentResult.items) ? amendmentResult.items : []);
      setFanouts(Array.isArray(fanoutResult.items) ? fanoutResult.items : []);
      setBrowserValidations(Array.isArray(browserResult.items) ? browserResult.items : []);
      setGitHubRequests(Array.isArray(githubResult.items) ? githubResult.items : []);
    } catch (value) {
      if (!signal?.aborted) setError(messageFor(value));
    } finally {
      if (!signal?.aborted) setPending(false);
    }
  }, [runId]);

  useEffect(() => {
    const controller = new AbortController();
    void refresh(controller.signal);
    return () => controller.abort();
  }, [refresh, refreshToken]);

  async function decidePacket(
    packet: ApprovalPacket,
    decisions: Record<string, boolean>
  ) {
    if (!runId) return;
    const actionId = `packet:${packet.packet_id}`;
    setActionPending(actionId);
    setError(null);
    try {
      const updated = await postJson<ApprovalPacket>(
        `/api/runs/${encodeURIComponent(runId)}/approval-packets/${encodeURIComponent(packet.packet_id)}/decision`,
        {
          expected_packet_digest: packet.packet_digest,
          decisions
        }
      );
      setPackets((current) => current.map((item) => (
        item.packet_id === updated.packet_id ? updated : item
      )));
    } catch (value) {
      setError(messageFor(value));
    } finally {
      setActionPending("");
    }
  }

  async function decideAmendment(amendment: GraphAmendment, approved: boolean) {
    if (!runId) return;
    const actionId = `amendment:${amendment.amendment_id}`;
    setActionPending(actionId);
    setError(null);
    try {
      const updated = await postJson<GraphAmendment>(
        `/api/runs/${encodeURIComponent(runId)}/graph/amendments/${encodeURIComponent(amendment.amendment_id)}/decision`,
        {
          approved,
          expected_base_graph_digest: amendment.base_graph_digest
        }
      );
      setAmendments((current) => current.map((item) => (
        item.amendment_id === updated.amendment_id ? updated : item
      )));
    } catch (value) {
      setError(messageFor(value));
    } finally {
      setActionPending("");
    }
  }

  async function selectCandidate(fanout: CandidateFanout) {
    if (!runId) return;
    const actionId = `fanout:${fanout.fanout_id}`;
    setActionPending(actionId);
    setError(null);
    try {
      await postJson(
        `/api/runs/${encodeURIComponent(runId)}/candidate-fanouts/${encodeURIComponent(fanout.fanout_id)}/select`
      );
      await refresh();
    } catch (value) {
      setError(messageFor(value));
    } finally {
      setActionPending("");
    }
  }

  async function preparePullRequest() {
    if (!runId || !review.reviewId) return;
    setActionPending("github:prepare");
    setError(null);
    try {
      const request = await postJson<GitHubChangeRequest>(
        `/api/runs/${encodeURIComponent(runId)}/github-change-requests`,
        {
          request_id: `github_request_${crypto.randomUUID().replaceAll("-", "")}`,
          review_id: review.reviewId,
          title: prTitle.trim() || `Kestrel repair ${review.reviewId}`,
          base_branch: defaultBranch,
          head_branch: review.branch
        }
      );
      setGitHubRequests((current) => [...current, request]);
      setPrTitle("");
    } catch (value) {
      setError(messageFor(value));
    } finally {
      setActionPending("");
    }
  }

  async function recoverGithub(request: GitHubChangeRequest) {
    setActionPending(`github:${request.request_id}`);
    setError(null);
    try {
      await postJson(
        `/api/github-change-requests/${encodeURIComponent(request.request_id)}/actions/recover`
      );
      await refresh();
    } catch (value) {
      setError(messageFor(value));
    } finally {
      setActionPending("");
    }
  }

  if (!runId) return null;

  const evidenceCount = packets.length
    + amendments.length
    + fanouts.length
    + browserValidations.length
    + githubRequests.length;

  return (
    <section className="engineering-run-panel" aria-label="Engineering evidence and shipping">
      <header>
        <div>
          <span className="eyebrow">Bounded execution</span>
          <h3>Evidence, decisions, and shipping</h3>
          <p>Every action below remains bound to the displayed run, digest, and review evidence.</p>
        </div>
        <button
          type="button"
          className="btn subtle"
          onClick={() => void refresh()}
          disabled={pending}
        >
          <RefreshCw className={pending ? "spin" : ""} size={14} />
          Refresh
        </button>
      </header>

      {error ? <div className="engineering-error" role="alert">{error}</div> : null}
      {evidenceCount === 0 && !pending ? (
        <p className="engineering-empty">No approval packets, candidate comparisons, browser proof, graph changes, or shipping records yet.</p>
      ) : null}

      {packets.length > 0 ? (
        <EvidenceGroup icon={<ShieldCheck size={16} />} title="Approval packets" count={packets.length}>
          {packets.map((packet) => (
            <ApprovalPacketCard
              key={packet.packet_id}
              packet={packet}
              pending={actionPending === `packet:${packet.packet_id}`}
              onDecision={decidePacket}
            />
          ))}
        </EvidenceGroup>
      ) : null}

      {amendments.length > 0 ? (
        <EvidenceGroup icon={<Workflow size={16} />} title="Plan changes" count={amendments.length}>
          {amendments.map((amendment) => (
            <article className="engineering-card" key={amendment.amendment_id}>
              <div className="engineering-card-title">
                <strong>{label(amendment.operation)}</strong>
                <StatusBadge value={amendment.status} />
              </div>
              <InlineMeta items={[
                amendment.amendment_id,
                amendment.requires_approval ? "scope approval required" : "within approved bounds"
              ]} />
              {amendment.approval_reasons.length > 0 ? (
                <p>{amendment.approval_reasons.join(" · ")}</p>
              ) : null}
              {amendment.status === "pending_approval" ? (
                <div className="engineering-actions">
                  <button
                    type="button"
                    disabled={actionPending === `amendment:${amendment.amendment_id}`}
                    onClick={() => void decideAmendment(amendment, false)}
                  >
                    Reject
                  </button>
                  <button
                    type="button"
                    disabled={actionPending === `amendment:${amendment.amendment_id}`}
                    onClick={() => void decideAmendment(amendment, true)}
                  >
                    Approve displayed change
                  </button>
                </div>
              ) : null}
            </article>
          ))}
        </EvidenceGroup>
      ) : null}

      {fanouts.length > 0 ? (
        <EvidenceGroup icon={<Layers3 size={16} />} title="Candidate comparison" count={fanouts.length}>
          {fanouts.map((fanout) => (
            <article className="engineering-card" key={fanout.fanout_id}>
              <div className="engineering-card-title">
                <strong>{fanout.source_task_id}</strong>
                <StatusBadge value={fanout.status} />
              </div>
              <InlineMeta items={[
                `${fanout.candidates.length} isolated candidates`,
                `$${fanout.estimated_budget_delta_usd.toFixed(2)} projected`
              ]} />
              <div className="candidate-grid">
                {fanout.candidates.map((candidate) => (
                  <div
                    className={candidate.candidate_id === fanout.selected_candidate_id ? "candidate selected" : "candidate"}
                    key={candidate.candidate_id}
                  >
                    <strong>{candidate.branch}</strong>
                    <StatusBadge value={candidate.status} />
                    <small>
                      {candidate.validation_passed === true
                        ? `Validated · ${candidate.reviewer_identities.length} reviewer(s)`
                        : candidate.validation_passed === false
                          ? "Validation failed"
                          : "Evidence pending"}
                    </small>
                    <small>
                      {candidate.changed_file_count ?? "?"} files · {candidate.changed_line_count ?? "?"} lines
                      {candidate.actual_cost_usd === null ? "" : ` · $${candidate.actual_cost_usd.toFixed(4)}`}
                    </small>
                    {candidate.risk_notes.length > 0 ? <p>{candidate.risk_notes.join(" · ")}</p> : null}
                  </div>
                ))}
              </div>
              {fanout.status === "running" ? (
                <button
                  type="button"
                  onClick={() => void selectCandidate(fanout)}
                  disabled={actionPending === `fanout:${fanout.fanout_id}`}
                >
                  Select from trusted evidence
                </button>
              ) : null}
            </article>
          ))}
        </EvidenceGroup>
      ) : null}

      {browserValidations.length > 0 ? (
        <EvidenceGroup icon={<Image size={16} />} title="Browser and visual proof" count={browserValidations.length}>
          {browserValidations.map((validation) => {
            const screenshot = validation.report.screenshot;
            return (
              <article className="engineering-card browser-evidence-card" key={validation.validation_id}>
                <div className="engineering-card-title">
                  <strong>{validation.target_url}</strong>
                  <StatusBadge value={validation.status} />
                </div>
                <InlineMeta items={[
                  validation.task_id,
                  validation.candidate_id ?? "run workspace",
                  validation.candidate_digest.slice(0, 12)
                ]} />
                {screenshot?.data_url ? (
                  <img
                    src={screenshot.data_url}
                    alt={`Browser validation for ${validation.target_url}`}
                    width={screenshot.width}
                    height={screenshot.height}
                  />
                ) : null}
                {validation.failure_codes.length > 0 ? (
                  <p className="engineering-failure">{validation.failure_codes.join(" · ")}</p>
                ) : (
                  <p>Rendered route, interaction, console/network collection, and accessibility evidence passed.</p>
                )}
              </article>
            );
          })}
        </EvidenceGroup>
      ) : null}

      <EvidenceGroup icon={<GitPullRequest size={16} />} title="GitHub shipping" count={githubRequests.length}>
        {review.reviewId && githubRequests.length === 0 ? (
          <div className="engineering-pr-form">
            <input
              aria-label="Pull request title"
              value={prTitle}
              placeholder={`Kestrel repair ${review.reviewId}`}
              onChange={(event) => setPrTitle(event.target.value)}
            />
            <button
              type="button"
              onClick={() => void preparePullRequest()}
              disabled={actionPending === "github:prepare"}
            >
              Prepare review-bound pull request
            </button>
          </div>
        ) : null}
        {!review.reviewId && githubRequests.length === 0 ? (
          <p className="engineering-empty">A current signed repair review is required before a pull request can be prepared.</p>
        ) : null}
        {githubRequests.map((request) => (
          <article className="engineering-card" key={request.request_id}>
            <div className="engineering-card-title">
              <strong>{request.title}</strong>
              <StatusBadge value={request.status} />
            </div>
            <InlineMeta items={[
              `${request.head_branch} → ${request.base_branch}`,
              request.external_number ? `PR #${request.external_number}` : "not published"
            ]} />
            {request.external_url ? (
              <a href={request.external_url} target="_blank" rel="noreferrer">Open pull request</a>
            ) : null}
            <div className="engineering-actions">
              <button
                type="button"
                onClick={() => onPrepareTool(
                  request.publish_tool_request.tool_name,
                  request.publish_tool_request.arguments
                )}
              >
                Prepare exact-call publish
              </button>
              <button
                type="button"
                onClick={() => onPrepareTool(
                  request.sync_tool_request.tool_name,
                  request.sync_tool_request.arguments
                )}
              >
                Sync checks and review
              </button>
              {request.status === "ci_failed" || request.status === "changes_requested" ? (
                <button
                  type="button"
                  disabled={actionPending === `github:${request.request_id}`}
                  onClick={() => void recoverGithub(request)}
                >
                  Start bounded recovery
                </button>
              ) : null}
            </div>
          </article>
        ))}
      </EvidenceGroup>
    </section>
  );
}

function ApprovalPacketCard({
  packet,
  pending,
  onDecision
}: {
  packet: ApprovalPacket;
  pending: boolean;
  onDecision: (packet: ApprovalPacket, decisions: Record<string, boolean>) => Promise<void>;
}) {
  const [decisions, setDecisions] = useState<Record<string, boolean | null>>(() =>
    Object.fromEntries(packet.calls.map((call) => [call.tool_call_id, null]))
  );
  const complete = packet.calls.every((call) => typeof decisions[call.tool_call_id] === "boolean");
  const submit = () => {
    if (!complete) return;
    void onDecision(
      packet,
      Object.fromEntries(
        packet.calls.map((call) => [call.tool_call_id, decisions[call.tool_call_id] === true])
      )
    );
  };
  return (
    <article className="engineering-card approval-packet-card">
      <div className="engineering-card-title">
        <strong>{packet.objective}</strong>
        <StatusBadge value={packet.status} />
      </div>
      <InlineMeta items={[
        `${packet.authorization_record_count} exact calls`,
        packet.checkpoint || "plan checkpoint",
        packet.packet_digest.slice(0, 12)
      ]} />
      {packet.calls.map((call) => (
        <div className="approval-packet-call" key={call.tool_call_id}>
          {(() => {
            const bindingComplete = completeCallBinding(call);
            return (
              <>
          <div>
            <strong>{call.tool_name}</strong>
            <StatusBadge value={call.risk} />
          </div>
          <p>{call.reason}</p>
          {!bindingComplete ? (
            <p className="engineering-failure">
              Immutable call evidence is incomplete. Approval is disabled;
              deny this call and request a fresh packet.
            </p>
          ) : null}
          <dl className="approval-packet-facts">
            <div>
              <dt>Exact call</dt>
              <dd>
                <code>{call.tool_call_id}</code>
                <code>{call.call_digest}</code>
              </dd>
            </div>
            <div>
              <dt>Capability</dt>
              <dd>
                <code>{`tool:${call.tool_name}`}</code>
                <span>{`revision ${call.capability_revision}`}</span>
              </dd>
            </div>
            <div>
              <dt>Target resource</dt>
              <dd>
                <span>{call.resource_scope}</span>
                <code>{call.resource_digest}</code>
              </dd>
            </div>
            <div>
              <dt>Validity</dt>
              <dd>Valid until decision or binding change</dd>
            </div>
            <div>
              <dt>Consequence</dt>
              <dd>{call.expected_side_effect}</dd>
            </div>
            <div>
              <dt>Rollback</dt>
              <dd>{call.rollback}</dd>
            </div>
          </dl>
          <EvidenceDrawer
            title={`Arguments for ${call.tool_name}`}
            records={[
              {
                label: `${call.tool_name} exact arguments`,
                value: call.arguments,
              },
            ]}
          />
          {packet.status === "pending" ? (
            <div role="group" aria-label={`Decision for ${call.tool_name}`}>
              <button
                type="button"
                className={decisions[call.tool_call_id] === false ? "active danger" : ""}
                aria-pressed={decisions[call.tool_call_id] === false}
                onClick={() => setDecisions((current) => ({
                  ...current,
                  [call.tool_call_id]: false
                }))}
              >
                Deny
              </button>
              <button
                type="button"
                className={decisions[call.tool_call_id] === true ? "active" : ""}
                aria-pressed={decisions[call.tool_call_id] === true}
                disabled={!bindingComplete}
                onClick={() => setDecisions((current) => ({
                  ...current,
                  [call.tool_call_id]: true
                }))}
              >
                Approve
              </button>
            </div>
          ) : null}
              </>
            );
          })()}
        </div>
      ))}
      {packet.status === "pending" ? (
        <button type="button" disabled={!complete || pending} onClick={submit}>
          Submit individual decisions
        </button>
      ) : null}
    </article>
  );
}

function completeCallBinding(call: ApprovalPacketCall): boolean {
  return Boolean(
    call.tool_call_id &&
      call.tool_name &&
      call.arguments &&
      typeof call.arguments === "object" &&
      !Array.isArray(call.arguments) &&
      /^[a-f0-9]{64}$/i.test(call.call_digest) &&
      Number.isSafeInteger(call.capability_revision) &&
      call.capability_revision >= 0 &&
      /^[a-f0-9]{64}$/i.test(call.resource_digest),
  );
}

function EvidenceGroup({
  icon,
  title,
  count,
  children
}: {
  icon: ReactNode;
  title: string;
  count: number;
  children: ReactNode;
}) {
  return (
    <section className="engineering-evidence-group">
      <h4>{icon}{title}<span>{count}</span></h4>
      <div>{children}</div>
    </section>
  );
}

function repairReviewProjection(tasks: TaskNode[]): {
  reviewId: string | null;
  branch: string | null;
} {
  for (const task of tasks) {
    const result = record(task.result);
    const artifact = record(result?.repair_artifact);
    if (artifact?.tool !== "repair.review") continue;
    const reviewId = typeof artifact.review_id === "string" ? artifact.review_id : null;
    const snapshot = record(artifact.repair_snapshot);
    const branch = typeof snapshot?.branch === "string" ? snapshot.branch : null;
    if (reviewId && branch) return { reviewId, branch };
  }
  return { reviewId: null, branch: null };
}

function record(value: unknown): Record<string, unknown> | null {
  return value !== null && typeof value === "object" && !Array.isArray(value)
    ? value as Record<string, unknown>
    : null;
}

function label(value: string): string {
  return value.replaceAll("_", " ").replace(/\b\w/g, (character) => character.toUpperCase());
}

function messageFor(value: unknown): string {
  return value instanceof Error ? value.message : String(value);
}
