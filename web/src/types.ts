export type Run = {
  run_id: string;
  status: string;
  message: string;
  session_id: string;
  workspace: string;
  provider?: string;
  model: string;
  assistant_message: string;
  tool_count: number;
  context_chars: number;
  stop_reason: string;
  error?: string | null;
  created_at: string;
  updated_at: string;
  approvals?: Approval[];
};

export type Session = {
  session_id: string;
  run_count: number;
  status_counts: Record<string, number>;
  latest_run_id: string;
  latest_status: string;
  latest_message: string;
  created_at: string;
  updated_at: string;
};

export type Approval = {
  approval_id: string;
  run_id: string;
  tool_call_id: string;
  tool_name: string;
  arguments: Record<string, unknown>;
  risk: string;
  status: string;
  decision?: Record<string, unknown> | null;
  result?: Record<string, unknown> | null;
  created_at: string;
  updated_at: string;
};

export type Tool = {
  name: string;
  description: string;
  parameters?: Record<string, unknown>;
  risk: string;
  requires_approval: boolean;
  source: string;
  server_id?: string | null;
  skill_id?: string | null;
  capabilities?: string[];
};

export type McpTool = Tool & {
  remote_name?: string;
};

export type McpServer = {
  id: string;
  name: string;
  transport: string;
  command?: string | null;
  args?: string[];
  env?: Record<string, string>;
  secret_env?: Record<string, string>;
  url?: string | null;
  enabled: boolean;
  tools: McpTool[];
  status: string;
  error?: string | null;
  session_state?: string;
  last_synced_at?: string | null;
  last_seen_at?: string | null;
  last_call_at?: string | null;
  last_error_at?: string | null;
  tool_count?: number;
  capabilities?: string[];
  risk_policy?: string;
  failure_count?: number;
  last_latency_ms?: number | null;
  vetting?: Record<string, unknown>;
  updated_at?: string;
};

export type Skill = {
  id: string;
  name: string;
  description: string;
  path?: string;
  manifest?: Record<string, unknown>;
  enabled: boolean;
  updated_at?: string;
};

export type Plugin = {
  id: string;
  name: string;
  description: string;
  source_url: string;
  source_ref?: string | null;
  commit_sha: string;
  install_path: string;
  manifest: Record<string, unknown>;
  capabilities: string[];
  enabled: boolean;
  risk_report: Record<string, unknown>;
  install_status: string;
  format: string;
  created_at: string;
  updated_at: string;
};

export type Channel = {
  id: string;
  provider: string;
  enabled: boolean;
  send_enabled: boolean;
  auto_reply: boolean;
  token_env?: string | null;
  webhook_url_env?: string | null;
  settings: Record<string, unknown>;
  env_status?: Record<string, unknown>;
};

export type MemoryHit = {
  layer: string;
  kind: string;
  title: string;
  score: number;
  snippet: string;
  record_id?: string;
};

export type MemoryLayerStatus = {
  layer: string;
  path: string;
  exists: boolean;
  ok: boolean;
  backend: string;
};

export type ContextPackResult = {
  packed_prompt?: string;
  token_estimate?: number;
  selected_item_count?: number;
  selected_layers?: string[];
  conflict_warnings?: string[];
  evidence_refs?: string[];
  telemetry?: Record<string, unknown>;
  success?: boolean;
  content?: string;
  error?: string | null;
};

export type TraceEvent = {
  id: number;
  run_id: string;
  type: string;
  payload: Record<string, unknown>;
  created_at: string;
};

export type RunTrace = {
  run: Run;
  summary: {
    event_count: number;
    span_count?: number;
    first_event_at: string | null;
    last_event_at: string | null;
    trace_counts: Record<string, number>;
    span_counts?: Record<string, number>;
  };
  timeline: TraceEvent[];
  spans?: Array<Record<string, unknown>>;
  traces: Record<string, TraceEvent[]>;
};

export type AgentLogEvent = {
  id: string;
  type: string;
  payload: Record<string, unknown>;
  created_at: string;
};

export type TaskNode = {
  task_id: string;
  title: string;
  goal: string;
  profile: string;
  status: string;
  approved: boolean;
  plan?: Record<string, unknown> | null;
  result?: Record<string, unknown> | null;
  dependencies?: string[];
  required_tools?: string[];
  risk?: string;
  acceptance_criteria?: string[];
  attempt_count?: number;
  failure_reason?: string;
  diagnosis?: Record<string, unknown> | null;
  retry_strategy?: Record<string, unknown> | null;
  scheduler_reason?: string;
};

export type SubagentRun = {
  subagent_id: string;
  run_id: string;
  profile: string;
  goal: string;
  status: string;
  task_id?: string | null;
  result: string;
  error?: string | null;
};

export type TaskGraph = {
  tasks: TaskNode[];
  ready_tasks: TaskNode[];
  approval_blocked_tasks: TaskNode[];
  subagents: SubagentRun[];
};

export type RuntimeConfig = {
  name: string;
  version?: string | null;
  schema_version: number;
  provider: Record<string, unknown>;
  feature_flags: Record<string, boolean>;
  limits: Record<string, number>;
  paths: Record<string, string>;
  validation_commands: string[];
};

export type ApiResult = Record<string, unknown>;
