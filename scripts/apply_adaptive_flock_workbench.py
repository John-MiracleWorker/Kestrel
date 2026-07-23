from __future__ import annotations

from pathlib import Path
from textwrap import dedent


def replace_once(text: str, marker: str, replacement: str, *, name: str) -> str:
    count = text.count(marker)
    if count != 1:
        raise SystemExit(f"{name} marker count was {count}")
    return text.replace(marker, replacement, 1)


def main() -> None:
    app = Path("web/src/App.tsx")
    text = app.read_text(encoding="utf-8")
    text = replace_once(
        text,
        'import { EmptyState, Field, InlineMeta, JsonBlock, Panel, StatusBadge } from "./components";\n',
        'import { EmptyState, Field, InlineMeta, JsonBlock, Panel, StatusBadge } from "./components";\n'
        'import { RoutingCenter } from "./routing/RoutingCenter";\n',
        name="Routing Center import",
    )
    text = replace_once(
        text,
        'type AppSection = "chat" | "routines" | "advanced" | "settings";\n',
        'type AppSection = "chat" | "routines" | "routing" | "advanced" | "settings";\n',
        name="AppSection",
    )
    text = replace_once(
        text,
        '  "subagent.started",\n'
        '  "subagent.completed",\n'
        '  "subagent.blocked",\n'
        '  "worker.isolated",\n'
        '  "subagent.failed"\n',
        '  "subagent.started",\n'
        '  "subagent.completed",\n'
        '  "subagent.blocked",\n'
        '  "worker.isolated",\n'
        '  "subagent.failed",\n'
        '  "routing.selected",\n'
        '  "routing.attempt_started",\n'
        '  "routing.shadow_unavailable",\n'
        '  "routing.guardrail_blocked",\n'
        '  "routing.assignment_failed",\n'
        '  "routing.start_failed",\n'
        '  "routing.outcome_recorded",\n'
        '  "routing.outcome_failed"\n',
        name="routing event allowlist",
    )
    text = replace_once(
        text,
        dedent(
            '''\
                  <button
                    className={`nav-button ${activeSection === "routines" ? "active" : ""}`}
                    type="button"
                    onClick={() => routeToSection("routines")}
                  >
                    <CalendarClock size={16} />
                    Routines
                  </button>
                  <button className="nav-button" type="button" onClick={() => jumpToAdvanced("activity")}> 
            '''
        ).rstrip() + "\n",
        dedent(
            '''\
                  <button
                    className={`nav-button ${activeSection === "routines" ? "active" : ""}`}
                    type="button"
                    onClick={() => routeToSection("routines")}
                  >
                    <CalendarClock size={16} />
                    Routines
                  </button>
                  <button
                    className={`nav-button ${activeSection === "routing" ? "active" : ""}`}
                    type="button"
                    onClick={() => routeToSection("routing")}
                  >
                    <Route size={16} />
                    Routing
                  </button>
                  <button className="nav-button" type="button" onClick={() => jumpToAdvanced("activity")}> 
            '''
        ).rstrip() + "\n",
        name="routing nav button",
    )
    text = replace_once(
        text,
        dedent(
            '''\
            {activeSection === "advanced" && (
              <section
            '''
        ),
        dedent(
            '''\
            {activeSection === "routing" && (
              <section
                id="routing-workbench"
                className="shell page-shell advanced-page"
                ref={conversationRef}
                tabIndex={0}
                aria-label="Adaptive Flock routing workbench"
              >
                <header className="page-header advanced-header">
                  <div>
                    <span className="eyebrow">Adaptive execution</span>
                    <h1>Adaptive Flock Routing</h1>
                    <p>Configure provider pools, inspect route policies, and preview why Kestrel selects a worker.</p>
                  </div>
                  <button type="button" className="secondary-button" onClick={() => routeToSection("chat")}>
                    <MessageCircle size={16} />
                    Back to chat
                  </button>
                </header>
                {error && <div className="banner error">{error}</div>}
                {notice && <div className="banner success">{notice}</div>}
                <RoutingCenter
                  activeRunId={activeRun?.run_id ?? null}
                  activeTaskId={
                    taskGraph?.tasks.find((task) => ["running", "blocked", "pending"].includes(task.status))?.task_id ??
                    null
                  }
                  onError={setError}
                  onNotice={setNotice}
                />
              </section>
            )}

            {activeSection === "advanced" && (
              <section
            '''
        ),
        name="routing section render",
    )
    text = replace_once(
        text,
        '  return normalized === "chat" || normalized === "routines" || normalized === "advanced" || normalized === "settings"\n'
        '    ? normalized\n'
        '    : null;\n',
        '  return normalized === "chat" ||\n'
        '    normalized === "routines" ||\n'
        '    normalized === "routing" ||\n'
        '    normalized === "advanced" ||\n'
        '    normalized === "settings"\n'
        '    ? normalized\n'
        '    : null;\n',
        name="routing hash parser",
    )
    app.write_text(text, encoding="utf-8")

    tests = Path("web/src/App.test.tsx")
    test_text = tests.read_text(encoding="utf-8")
    test_text = replace_once(
        test_text,
        '    expect(screen.getByRole("button", { name: /advanced/i })).toBeInTheDocument();\n',
        '    expect(screen.getByRole("button", { name: /routing/i })).toBeInTheDocument();\n'
        '    expect(screen.getByRole("button", { name: /advanced/i })).toBeInTheDocument();\n',
        name="routing nav expectation",
    )
    routing_test = dedent(
        '''\

          it("opens the Adaptive Flock Routing Center from primary navigation", async () => {
            render(<App />);

            expect(await screen.findByRole("heading", { name: "Ask Kestrel" })).toBeInTheDocument();
            fireEvent.click(screen.getByRole("button", { name: /routing/i }));

            expect(await screen.findByRole("heading", { name: "Adaptive Flock Routing" })).toBeInTheDocument();
            expect(await screen.findByText("Local server")).toBeInTheDocument();
            expect(screen.getByRole("button", { name: /preview decision/i })).toBeInTheDocument();
          });
        '''
    )
    test_text = replace_once(
        test_text,
        '  it("keeps idle chat polling lightweight", async () => {\n',
        routing_test + '\n  it("keeps idle chat polling lightweight", async () => {\n',
        name="routing integration test",
    )
    test_text = replace_once(
        test_text,
        '  if (path === "/api/secrets") return secrets;\n\n',
        dedent(
            '''\
              if (path === "/api/secrets") return secrets;
              if (path === "/api/routing/status") {
                return {
                  schema: "kestrel.adaptive_flock.status.v1",
                  runtime: { enabled: false, mode: "off", policy_id: "balanced" },
                  routing_schema_version: 1,
                  counts: {
                    provider_profiles: 1,
                    enabled_provider_profiles: 1,
                    model_targets: 1,
                    enabled_model_targets: 1,
                    policies: 1,
                    enabled_policies: 1
                  }
                };
              }
              if (path === "/api/routing/providers") {
                return [
                  {
                    profile_id: "local",
                    display_name: "Local server",
                    adapter: "openai-compatible",
                    base_url_configured: true,
                    secret_configured: false,
                    enabled: true,
                    locality: "local",
                    trust_class: "standard",
                    max_concurrency: 1,
                    metadata: {},
                    revision: 1,
                    created_at: "2026-05-16T00:00:00Z",
                    updated_at: "2026-05-16T00:00:00Z"
                  }
                ];
              }
              if (path === "/api/routing/targets") {
                return [
                  {
                    target_id: "local-worker",
                    provider_profile_id: "local",
                    provider: "openai-compatible",
                    model: "qwen-coder",
                    enabled: true,
                    locality: "local",
                    trust_class: "standard",
                    capability_tags: ["worker"],
                    role_affinities: ["worker"],
                    task_family_affinities: [],
                    max_context_tokens: 32768,
                    supports_tools: true,
                    supports_json: false,
                    supports_vision: false,
                    supports_reasoning: false,
                    supports_streaming: true,
                    quality_tier: 2,
                    latency_tier: 2,
                    operator_priority: 0,
                    estimated_cost_usd: 0,
                    health: "healthy",
                    recent_failure_rate: 0,
                    predicted_success: null,
                    metadata: {},
                    revision: 1,
                    created_at: "2026-05-16T00:00:00Z",
                    updated_at: "2026-05-16T00:00:00Z"
                  }
                ];
              }
              if (path === "/api/routing/policies") {
                return [
                  {
                    policy_id: "balanced",
                    enabled: true,
                    quality_weight: 0.4,
                    affinity_weight: 0.16,
                    health_weight: 0.1,
                    context_weight: 0.08,
                    locality_weight: 0.08,
                    operator_weight: 0.05,
                    cost_weight: 0.08,
                    latency_weight: 0.03,
                    failure_weight: 0.12,
                    require_different_target_for_review: false,
                    require_different_model_family_for_review: false,
                    prefer_different_provider_for_review: false,
                    minimum_quality_by_risk: { low: 1, medium: 2, high: 3, critical: 4 },
                    revision: 1,
                    created_at: "2026-05-16T00:00:00Z",
                    updated_at: "2026-05-16T00:00:00Z"
                  }
                ];
              }
              if (/^\/api\/runs\/[^/]+\/routing(?:\?.*)?$/.test(path)) {
                return { run_id: "run_2", task_id: null, decisions: [], outcomes: [] };
              }

            '''
        ),
        name="routing API fixtures",
    )
    tests.write_text(test_text, encoding="utf-8")

    Path(".github/workflows/adaptive-flock-workbench.yml").unlink()
    Path(__file__).unlink()


if __name__ == "__main__":
    main()
