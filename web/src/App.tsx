import {
  Activity,
  Brain,
  Check,
  Database,
  PlugZap,
  Search,
  ShieldCheck,
  Sparkles,
  Square,
  Wrench,
  X
} from "lucide-react";
import { FormEvent, useEffect, useMemo, useState } from "react";

type Run = {
  run_id: string;
  status: string;
  message: string;
  session_id: string;
  assistant_message: string;
  tool_count: number;
  context_chars: number;
  stop_reason: string;
  error?: string | null;
  approvals?: Approval[];
};

type Approval = {
  approval_id: string;
  run_id: string;
  tool_name: string;
  arguments: Record<string, unknown>;
  risk: string;
  status: string;
};

type Tool = {
  name: string;
  description: string;
  parameters?: Record<string, unknown>;
  risk: string;
  requires_approval: boolean;
  source: string;
  server_id?: string | null;
};

type McpTool = Tool & {
  remote_name?: string;
  capabilities?: string[];
};

type MemoryHit = {
  layer: string;
  kind: string;
  title: string;
  score: number;
  snippet: string;
};

type McpServer = {
  id: string;
  name: string;
  transport: string;
  status: string;
  session_state?: string;
  enabled: boolean;
  tools: McpTool[];
  tool_count?: number;
  last_seen_at?: string | null;
  last_call_at?: string | null;
  last_error_at?: string | null;
  failure_count?: number;
  last_latency_ms?: number | null;
  risk_policy?: string;
  error?: string | null;
};

type Skill = {
  id: string;
  name: string;
  description: string;
  enabled: boolean;
};

type EventRow = {
  id: number;
  type: string;
  payload: Record<string, unknown>;
};

type TaskGraph = {
  tasks: Array<{ task_id: string; title: string; goal: string; profile: string; status: string; approved: boolean }>;
  subagents: Array<{ subagent_id: string; profile: string; goal: string; status: string; result: string; error?: string | null }>;
};

const api = {
  async get<T>(path: string): Promise<T> {
    const response = await fetch(path);
    if (!response.ok) throw new Error(await response.text());
    return response.json();
  },
  async post<T>(path: string, body: unknown = {}): Promise<T> {
    const response = await fetch(path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body)
    });
    if (!response.ok) throw new Error(await response.text());
    return response.json();
  },
  async delete<T>(path: string): Promise<T> {
    const response = await fetch(path, { method: "DELETE" });
    if (!response.ok) throw new Error(await response.text());
    return response.json();
  }
};

export function App() {
  const [message, setMessage] = useState("");
  const [runs, setRuns] = useState<Run[]>([]);
  const [activeRunId, setActiveRunId] = useState<string | null>(null);
  const [events, setEvents] = useState<EventRow[]>([]);
  const [tools, setTools] = useState<Tool[]>([]);
  const [approvals, setApprovals] = useState<Approval[]>([]);
  const [memoryQuery, setMemoryQuery] = useState("");
  const [memoryHits, setMemoryHits] = useState<MemoryHit[]>([]);
  const [learningTitle, setLearningTitle] = useState("");
  const [learningContent, setLearningContent] = useState("");
  const [learningKind, setLearningKind] = useState("observation");
  const [learningValidation, setLearningValidation] = useState("0.78");
  const [learningRepeat, setLearningRepeat] = useState("1");
  const [learningExplicit, setLearningExplicit] = useState(false);
  const [learningResult, setLearningResult] = useState<Record<string, unknown> | null>(null);
  const [mcpServers, setMcpServers] = useState<McpServer[]>([]);
  const [mcpId, setMcpId] = useState("");
  const [mcpCommand, setMcpCommand] = useState("");
  const [mcpTransport, setMcpTransport] = useState("stdio");
  const [mcpToolSelection, setMcpToolSelection] = useState("");
  const [mcpArguments, setMcpArguments] = useState("{}");
  const [mcpInvokeResult, setMcpInvokeResult] = useState<Record<string, unknown> | null>(null);
  const [skills, setSkills] = useState<Skill[]>([]);
  const [taskGraph, setTaskGraph] = useState<TaskGraph | null>(null);
  const [subagentProfile, setSubagentProfile] = useState("worker");
  const [subagentGoal, setSubagentGoal] = useState("");
  const activeRun = useMemo(() => runs.find((run) => run.run_id === activeRunId) ?? runs[0], [runs, activeRunId]);
  const streamedAssistant = useMemo(
    () =>
      events
        .filter((event) => event.type === "assistant.token")
        .map((event) => String(event.payload.content ?? ""))
        .join(""),
    [events]
  );
  const mcpToolOptions = useMemo(
    () =>
      mcpServers.flatMap((server) =>
        server.tools.map((tool) => ({
          server,
          tool,
          value: `${server.id}::${tool.remote_name ?? tool.name}`
        }))
      ),
    [mcpServers]
  );

  async function refresh() {
    const [runList, toolList, approvalList, mcpList, skillList] = await Promise.all([
      api.get<Run[]>("/api/runs"),
      api.get<Tool[]>("/api/tools"),
      api.get<Approval[]>("/api/approvals?status=pending"),
      api.get<McpServer[]>("/api/mcp/servers"),
      api.get<Skill[]>("/api/skills")
    ]);
    setRuns(runList);
    setTools(toolList);
    setApprovals(approvalList);
    setMcpServers(mcpList);
    setSkills(skillList);
    if (!activeRunId && runList.length > 0) setActiveRunId(runList[0].run_id);
  }

  useEffect(() => {
    refresh().catch(console.error);
    const timer = window.setInterval(() => refresh().catch(console.error), 3000);
    return () => window.clearInterval(timer);
  }, []);

  useEffect(() => {
    if (!activeRun?.run_id) return;
    setEvents([]);
    const source = new EventSource(`/api/runs/${activeRun.run_id}/events`);
    source.onmessage = (event) => {
      const parsed = JSON.parse(event.data);
      setEvents((rows) => [...rows.slice(-80), parsed]);
      refresh().catch(console.error);
    };
    [
      "run.started",
      "run.completed",
      "run.blocked",
      "run.failed",
      "approval.requested",
      "tool.executed",
      "tool.started",
      "tool.completed",
      "tool.failed",
      "assistant.token",
      "assistant.tool_call",
      "assistant.usage",
      "assistant.provider_error",
      "task.approved",
      "subagent.queued",
      "subagent.started",
      "subagent.completed",
      "subagent.failed"
    ].forEach((type) => {
      source.addEventListener(type, (event) => {
        const parsed = JSON.parse((event as MessageEvent).data);
        setEvents((rows) => [...rows.slice(-80), parsed]);
        if (type !== "assistant.token") refresh().catch(console.error);
      });
    });
    return () => source.close();
  }, [activeRun?.run_id]);

  async function submitRun(event: FormEvent) {
    event.preventDefault();
    if (!message.trim()) return;
    const run = await api.post<Run>("/api/runs", { message });
    setMessage("");
    setActiveRunId(run.run_id);
    await refresh();
    const graph = await api.get<TaskGraph>(`/api/runs/${run.run_id}/task-graph`);
    setTaskGraph(graph);
  }

  async function decide(approval: Approval, approved: boolean) {
    await api.post(`/api/approvals/${approval.approval_id}/decision`, {
      approved,
      arguments: approval.arguments
    });
    await refresh();
  }

  async function searchMemory(event: FormEvent) {
    event.preventDefault();
    if (!memoryQuery.trim()) return;
    const hits = await api.post<MemoryHit[]>("/api/memory/search", { query: memoryQuery, k: 8 });
    setMemoryHits(hits);
  }

  async function submitLearning(event: FormEvent) {
    event.preventDefault();
    if (!learningTitle.trim() || !learningContent.trim()) return;
    const result = await api.post<Record<string, unknown>>("/api/memory/learn", {
      title: learningTitle,
      content: learningContent,
      kind: learningKind,
      validation_score: Number(learningValidation),
      repeat_count: Number(learningRepeat),
      explicit_instruction: learningExplicit
    });
    setLearningResult(result);
    await refresh();
  }

  async function discoverSkills() {
    await api.post("/api/skills/discover");
    await refresh();
  }

  async function refreshTaskGraph() {
    if (!activeRun?.run_id) return;
    const graph = await api.get<TaskGraph>(`/api/runs/${activeRun.run_id}/task-graph`);
    setTaskGraph(graph);
  }

  async function submitSubagent(event: FormEvent) {
    event.preventDefault();
    if (!activeRun?.run_id || !subagentGoal.trim()) return;
    await api.post("/api/subagents", {
      run_id: activeRun.run_id,
      profile: subagentProfile,
      goal: subagentGoal
    });
    setSubagentGoal("");
    await refreshTaskGraph();
  }

  async function submitMcp(event: FormEvent) {
    event.preventDefault();
    if (!mcpId.trim()) return;
    await api.post("/api/mcp/servers", {
      id: mcpId.trim(),
      name: mcpId.trim(),
      transport: mcpTransport,
      command: mcpTransport === "stdio" ? mcpCommand.trim() || null : null,
      url: mcpTransport === "stdio" ? null : mcpCommand.trim() || null,
      args: [],
      tools: [],
      risk_policy: "approval_by_default"
    });
    setMcpId("");
    setMcpCommand("");
    await refresh();
  }

  async function controlMcp(server: McpServer, action: "connect" | "disconnect" | "restart") {
    await api.post(`/api/mcp/servers/${server.id}/${action}`);
    await refresh();
  }

  async function syncMcp(server: McpServer) {
    await api.post(`/api/mcp/servers/${server.id}/sync`);
    await refresh();
  }

  async function testMcp(server: McpServer) {
    await api.post(`/api/mcp/servers/${server.id}/test`);
    await refresh();
  }

  async function healthMcp(server: McpServer) {
    await api.get(`/api/mcp/servers/${server.id}/health`);
    await refresh();
  }

  async function deleteMcp(server: McpServer) {
    await api.delete(`/api/mcp/servers/${server.id}`);
    await refresh();
  }

  async function invokeMcp(event: FormEvent) {
    event.preventDefault();
    if (!mcpToolSelection) return;
    const [serverId, toolName] = mcpToolSelection.split("::");
    try {
      const parsed = JSON.parse(mcpArguments || "{}");
      const result = await api.post<Record<string, unknown>>(
        `/api/mcp/servers/${serverId}/tools/${encodeURIComponent(toolName)}/invoke`,
        { arguments: parsed }
      );
      setMcpInvokeResult(result);
    } catch (error) {
      setMcpInvokeResult({ success: false, error: error instanceof Error ? error.message : String(error) });
    }
    await refresh();
  }

  async function toggleSkill(skill: Skill) {
    await api.post(`/api/skills/${skill.id}/${skill.enabled ? "disable" : "enable"}`);
    await refresh();
  }

  return (
    <div className="shell">
      <aside className="sidebar">
        <div className="brand">
          <Brain size={28} />
          <div>
            <strong>Nested MV2 Agent</strong>
            <span>Local control plane</span>
          </div>
        </div>
        <nav>
          <a href="#chat"><Sparkles size={18} /> Chat</a>
          <a href="#approvals"><ShieldCheck size={18} /> Approvals</a>
          <a href="#subagents"><Brain size={18} /> Subagents</a>
          <a href="#tools"><Wrench size={18} /> Tools</a>
          <a href="#memory"><Database size={18} /> Memory</a>
          <a href="#mcp"><PlugZap size={18} /> MCP</a>
        </nav>
      </aside>

      <main className="workspace">
        <section id="chat" className="chat-band">
          <div className="run-list">
            <div className="section-title"><Activity size={18} /> Runs</div>
            {runs.map((run) => (
              <button
                key={run.run_id}
                className={run.run_id === activeRun?.run_id ? "run selected" : "run"}
                onClick={() => setActiveRunId(run.run_id)}
              >
                <span>{run.message || run.run_id}</span>
                <small>{run.status}</small>
              </button>
            ))}
          </div>

          <div className="conversation">
            <div className="run-header">
              <div>
                <h1>{activeRun?.status ?? "Ready"}</h1>
                <p>{activeRun?.session_id ?? "Start a background run"}</p>
              </div>
              {activeRun?.status === "running" && <Square className="pulse" size={20} />}
            </div>

            <div className="transcript">
              {activeRun ? (
                <>
                  <div className="bubble user">{activeRun.message}</div>
                  <div className="bubble agent">{activeRun.assistant_message || streamedAssistant || activeRun.stop_reason || "Working..."}</div>
                </>
              ) : (
                <div className="empty">No runs yet.</div>
              )}
            </div>

            <form className="composer" onSubmit={submitRun}>
              <input value={message} onChange={(event) => setMessage(event.target.value)} placeholder="Ask the agent to do real work..." />
              <button type="submit">Run</button>
            </form>
          </div>

          <div className="timeline">
            <div className="section-title"><Activity size={18} /> Timeline</div>
            {events.map((event) => (
              <div className="event" key={event.id}>
                <span>{event.type}</span>
                <code>{JSON.stringify(event.payload).slice(0, 220)}</code>
              </div>
            ))}
          </div>
        </section>

        <section id="subagents" className="band two-col">
          <form onSubmit={submitSubagent} className="memory-search">
            <div className="section-title"><Brain size={18} /> Subagents</div>
            <select value={subagentProfile} onChange={(event) => setSubagentProfile(event.target.value)}>
              <option value="worker">Worker</option>
              <option value="planner">Planner</option>
              <option value="reviewer">Reviewer</option>
            </select>
            <textarea value={subagentGoal} onChange={(event) => setSubagentGoal(event.target.value)} placeholder="Bounded subagent goal for the active run" />
            <button type="submit" disabled={!activeRun}>Queue Subagent</button>
            <button type="button" onClick={refreshTaskGraph} disabled={!activeRun}>Refresh Graph</button>
          </form>
          <div>
            <div className="section-title"><Activity size={18} /> Task Graph</div>
            {taskGraph?.tasks.map((task) => (
              <div className="row" key={task.task_id}>
                <strong>{task.title}</strong>
                <span>{task.profile} / {task.status} / {task.approved ? "approved" : "needs review"}</span>
                <p>{task.goal}</p>
              </div>
            ))}
            {taskGraph?.subagents.map((subagent) => (
              <div className="row" key={subagent.subagent_id}>
                <strong>{subagent.profile}</strong>
                <span>{subagent.status}</span>
                <p>{subagent.result || subagent.error || subagent.goal}</p>
              </div>
            ))}
          </div>
        </section>

        <section id="approvals" className="band two-col">
          <div>
            <div className="section-title"><ShieldCheck size={18} /> Pending Approvals</div>
            {approvals.length === 0 && <p className="muted">No blocked actions.</p>}
            {approvals.map((approval) => (
              <div className="approval" key={approval.approval_id}>
                <div>
                  <strong>{approval.tool_name}</strong>
                  <span>{approval.risk}</span>
                  <code>{JSON.stringify(approval.arguments)}</code>
                </div>
                <div className="actions">
                  <button onClick={() => decide(approval, true)}><Check size={16} /> Approve</button>
                  <button className="danger" onClick={() => decide(approval, false)}><X size={16} /> Deny</button>
                </div>
              </div>
            ))}
          </div>
          <div>
            <div className="section-title"><Wrench size={18} /> Tool Inventory</div>
            <div className="tool-grid">
              {tools.map((tool) => (
                <div className="tool" key={tool.name}>
                  <strong>{tool.name}</strong>
                  <span>{tool.source} / {tool.risk}</span>
                  <p>{tool.description}</p>
                </div>
              ))}
            </div>
          </div>
        </section>

        <section id="memory" className="band two-col">
          <form onSubmit={searchMemory} className="memory-search">
            <div className="section-title"><Search size={18} /> Memory Search</div>
            <input value={memoryQuery} onChange={(event) => setMemoryQuery(event.target.value)} placeholder="Search nested memory..." />
            <button type="submit">Search</button>
          </form>
          <div className="learning-panel">
            <form onSubmit={submitLearning} className="memory-search">
              <div className="section-title"><Brain size={18} /> Learning Signal</div>
              <input value={learningTitle} onChange={(event) => setLearningTitle(event.target.value)} placeholder="Title" />
              <textarea value={learningContent} onChange={(event) => setLearningContent(event.target.value)} placeholder="Validated memory content" />
              <div className="learning-controls">
                <select value={learningKind} onChange={(event) => setLearningKind(event.target.value)}>
                  <option value="observation">Observation</option>
                  <option value="fact">Fact</option>
                  <option value="event">Event</option>
                  <option value="failure">Failure</option>
                  <option value="procedure">Procedure</option>
                  <option value="policy">Policy</option>
                </select>
                <input value={learningValidation} onChange={(event) => setLearningValidation(event.target.value)} inputMode="decimal" />
                <input value={learningRepeat} onChange={(event) => setLearningRepeat(event.target.value)} inputMode="numeric" />
              </div>
              <label className="check-row">
                <input type="checkbox" checked={learningExplicit} onChange={(event) => setLearningExplicit(event.target.checked)} />
                <span>Explicit instruction</span>
              </label>
              <button type="submit">Learn</button>
            </form>
            {learningResult && <code>{JSON.stringify(learningResult).slice(0, 420)}</code>}
          </div>
          <div className="hits wide">
            {memoryHits.map((hit, index) => (
              <div className="hit" key={`${hit.title}-${index}`}>
                <strong>{hit.title}</strong>
                <span>{hit.layer} / {hit.kind} / {hit.score.toFixed(2)}</span>
                <p>{hit.snippet}</p>
              </div>
            ))}
          </div>
        </section>

        <section id="mcp" className="band two-col">
          <div>
            <div className="section-title"><PlugZap size={18} /> MCP Servers</div>
            <form onSubmit={submitMcp} className="memory-search">
              <input value={mcpId} onChange={(event) => setMcpId(event.target.value)} placeholder="Server id" />
              <select value={mcpTransport} onChange={(event) => setMcpTransport(event.target.value)}>
                <option value="stdio">stdio</option>
                <option value="streamable_http">streamable http</option>
                <option value="sse">sse</option>
              </select>
              <input value={mcpCommand} onChange={(event) => setMcpCommand(event.target.value)} placeholder="Command or URL" />
              <button type="submit">Add Server</button>
            </form>
            {mcpServers.map((server) => (
              <div className="row" key={server.id}>
                <strong>{server.name}</strong>
                <span>{server.transport} / {server.status} / {server.session_state ?? "disconnected"} / {server.tool_count ?? server.tools.length} tools</span>
                <div className="mcp-health">
                  <span>{server.risk_policy ?? "approval_by_default"}</span>
                  <span>{server.failure_count ?? 0} failures</span>
                  <span>{server.last_latency_ms ?? 0} ms</span>
                  {server.last_seen_at && <span>seen {new Date(server.last_seen_at).toLocaleTimeString()}</span>}
                </div>
                {server.error && <p>{server.error}</p>}
                <div className="actions">
                  <button onClick={() => controlMcp(server, "connect")}>Connect</button>
                  <button onClick={() => healthMcp(server)}>Health</button>
                  <button onClick={() => controlMcp(server, "restart")}>Restart</button>
                  <button onClick={() => controlMcp(server, "disconnect")}>Disconnect</button>
                  <button onClick={() => testMcp(server)}>Test</button>
                  <button onClick={() => syncMcp(server)}>Sync</button>
                  <button className="danger" onClick={() => deleteMcp(server)}>Delete</button>
                </div>
                {server.tools.length > 0 && (
                  <div className="tool-list">
                    {server.tools.map((tool) => (
                      <button
                        type="button"
                        key={tool.name}
                        onClick={() => {
                          setMcpToolSelection(`${server.id}::${tool.remote_name ?? tool.name}`);
                          setMcpArguments(JSON.stringify(tool.parameters ?? {}, null, 2));
                        }}
                      >
                        {tool.remote_name ?? tool.name} / {tool.risk}
                      </button>
                    ))}
                  </div>
                )}
              </div>
            ))}
            <form onSubmit={invokeMcp} className="mcp-invoke">
              <div className="section-title"><Wrench size={18} /> Manual MCP Invoke</div>
              <select value={mcpToolSelection} onChange={(event) => setMcpToolSelection(event.target.value)}>
                <option value="">Select tool</option>
                {mcpToolOptions.map(({ server, tool, value }) => (
                  <option key={value} value={value}>
                    {server.id} / {tool.remote_name ?? tool.name}
                  </option>
                ))}
              </select>
              <textarea value={mcpArguments} onChange={(event) => setMcpArguments(event.target.value)} />
              <button type="submit" disabled={!mcpToolSelection}>Invoke Tool</button>
              {mcpInvokeResult && <code>{JSON.stringify(mcpInvokeResult).slice(0, 680)}</code>}
            </form>
          </div>
          <div>
            <div className="section-title"><Sparkles size={18} /> Skills</div>
            <button onClick={discoverSkills}>Discover Skills</button>
            {skills.map((skill) => (
              <div className="row" key={skill.id}>
                <strong>{skill.name}</strong>
                <span>{skill.enabled ? "enabled" : "disabled"}</span>
                <p>{skill.description}</p>
                <button onClick={() => toggleSkill(skill)}>{skill.enabled ? "Disable" : "Enable"}</button>
              </div>
            ))}
          </div>
        </section>
      </main>
    </div>
  );
}
