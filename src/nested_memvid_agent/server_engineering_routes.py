from __future__ import annotations

from typing import Any, cast

from .server_models import (
    ApprovalPacketCreateRequest,
    ApprovalPacketDecisionRequest,
    BenchmarkCreateRequest,
    BenchmarkReplayRequest,
    BrowserValidationAPIRequest,
    CandidateEvidenceRequest,
    CandidateFanoutCreateRequest,
    CandidateFanoutPreviewRequest,
    GitHubChangeRequestPrepareRequest,
    GraphAmendmentDecisionRequest,
    GraphAmendmentRequest,
)
from .server_support import execution_response


def register_engineering_routes(
    app: Any,
    *,
    active_config: Any,
    state: Any,
    runs: Any,
    http_exception: Any,
) -> None:
    """Expose bounded, digest-bound task graph amendments."""

    service = runs.graph_amendments

    def config() -> Any:
        return active_config() if callable(active_config) else active_config

    def require_owner_api() -> None:
        if not bool(config().require_api_auth):
            raise http_exception(
                status_code=403,
                detail="engineering_mutation_requires_api_auth",
            )

    @app.get("/api/runs/{run_id}/graph/amendments")  # type: ignore[untyped-decorator]
    def list_graph_amendments(run_id: str) -> dict[str, Any]:
        try:
            state.get_run(run_id)
            items = service.list(run_id=run_id)
        except KeyError as exc:
            raise http_exception(status_code=404, detail=str(exc)) from exc
        return {
            "run_id": run_id,
            "items": [item.to_payload() for item in items],
        }

    @app.post("/api/runs/{run_id}/graph/amendments")  # type: ignore[untyped-decorator]
    def propose_graph_amendment(
        run_id: str,
        request: GraphAmendmentRequest,
    ) -> dict[str, Any]:
        require_owner_api()
        try:
            run = state.get_run(run_id)
            if run.status in {"completed", "failed", "cancelled"}:
                raise ValueError("terminal runs cannot accept graph amendments")
            record = service.propose(
                amendment_id=request.amendment_id,
                run_id=run_id,
                operation=request.operation,
                payload=request.payload,
                actor="owner",
                evidence_refs=tuple(request.evidence_refs),
                permitted_tools=_permitted_tools(state=state, runs=runs, run=run),
            )
        except KeyError as exc:
            raise http_exception(status_code=404, detail=str(exc)) from exc
        except PermissionError as exc:
            raise http_exception(status_code=403, detail=str(exc)) from exc
        except ValueError as exc:
            raise http_exception(status_code=409, detail=str(exc)) from exc
        runs.events.publish(
            run_id,
            (
                "graph.amendment.applied"
                if record.status == "applied"
                else "graph.amendment.requested"
            ),
            record.to_payload(),
        )
        return cast(dict[str, Any], record.to_payload())

    @app.post(  # type: ignore[untyped-decorator]
        "/api/runs/{run_id}/graph/amendments/{amendment_id}/decision"
    )
    def decide_graph_amendment(
        run_id: str,
        amendment_id: str,
        request: GraphAmendmentDecisionRequest,
    ) -> dict[str, Any]:
        require_owner_api()
        try:
            existing = service.get(amendment_id)
            if existing is None or existing.run_id != run_id:
                raise KeyError(f"Unknown graph amendment: {amendment_id}")
            record = service.decide(
                amendment_id,
                approved=request.approved,
                actor="owner",
                expected_base_graph_digest=request.expected_base_graph_digest,
            )
            scheduler: dict[str, Any] | None = None
            run = state.get_run(run_id)
            if (
                record.status == "applied"
                and run.status == "blocked"
                and run.stop_reason == "graph_amendment_approval_required"
            ):
                queued = state.transition_run(
                    run_id,
                    "queued",
                    expected_statuses=("blocked",),
                    expected_stop_reason="graph_amendment_approval_required",
                    stop_reason="graph_amendment_approved",
                )
                if queued.status == "queued":
                    scheduler = runs.run_scheduler_until_idle(run_id)
            payload = record.to_payload()
            payload["scheduler_resume"] = scheduler
        except KeyError as exc:
            raise http_exception(status_code=404, detail=str(exc)) from exc
        except PermissionError as exc:
            raise http_exception(status_code=403, detail=str(exc)) from exc
        except ValueError as exc:
            raise http_exception(status_code=409, detail=str(exc)) from exc
        runs.events.publish(
            run_id,
            (
                "graph.amendment.applied"
                if record.status == "applied"
                else "graph.amendment.rejected"
            ),
            payload,
        )
        return cast(dict[str, Any], payload)

    @app.post(  # type: ignore[untyped-decorator]
        "/api/runs/{run_id}/candidate-fanouts/preview"
    )
    def preview_candidate_fanout(
        run_id: str,
        request: CandidateFanoutPreviewRequest,
    ) -> dict[str, Any]:
        try:
            return cast(
                dict[str, Any],
                runs.preview_candidate_fanout(
                    fanout_id=request.fanout_id,
                    run_id=run_id,
                    source_task_id=request.source_task_id,
                    candidate_count=request.candidate_count,
                    estimated_budget_delta_usd=(
                        request.estimated_budget_delta_usd
                    ),
                ),
            )
        except KeyError as exc:
            raise http_exception(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise http_exception(status_code=409, detail=str(exc)) from exc

    @app.get("/api/runs/{run_id}/candidate-fanouts")  # type: ignore[untyped-decorator]
    def list_candidate_fanouts(run_id: str) -> dict[str, Any]:
        try:
            state.get_run(run_id)
            items = runs.candidate_fanouts.list_fanouts(run_id=run_id)
        except KeyError as exc:
            raise http_exception(status_code=404, detail=str(exc)) from exc
        return {
            "run_id": run_id,
            "items": [item.to_payload() for item in items],
        }

    @app.post("/api/runs/{run_id}/candidate-fanouts")  # type: ignore[untyped-decorator]
    def create_candidate_fanout(
        run_id: str,
        request: CandidateFanoutCreateRequest,
    ) -> dict[str, Any]:
        require_owner_api()
        if str(request.plan.get("run_id") or "") != run_id:
            raise http_exception(
                status_code=409,
                detail="candidate fanout plan belongs to another run",
            )
        try:
            return cast(
                dict[str, Any],
                runs.create_candidate_fanout(
                    fanout_id=request.fanout_id,
                    plan=request.plan,
                    approved_plan_digest=request.approved_plan_digest,
                    actor="owner",
                    start=request.start,
                ),
            )
        except KeyError as exc:
            raise http_exception(status_code=404, detail=str(exc)) from exc
        except (PermissionError, RuntimeError, ValueError) as exc:
            raise http_exception(status_code=409, detail=str(exc)) from exc

    @app.get(  # type: ignore[untyped-decorator]
        "/api/runs/{run_id}/candidate-fanouts/{fanout_id}"
    )
    def inspect_candidate_fanout(run_id: str, fanout_id: str) -> dict[str, Any]:
        try:
            fanout = runs.candidate_fanouts.get_fanout(fanout_id)
            if fanout.run_id != run_id:
                raise KeyError(f"Unknown candidate fanout: {fanout_id}")
            selection = runs.candidate_fanouts.get_selection(fanout_id)
        except KeyError as exc:
            raise http_exception(status_code=404, detail=str(exc)) from exc
        return {
            "fanout": fanout.to_payload(),
            "selection": None if selection is None else selection.to_payload(),
        }

    @app.post(  # type: ignore[untyped-decorator]
        "/api/runs/{run_id}/candidate-fanouts/{fanout_id}/"
        "candidates/{candidate_id}/evidence"
    )
    def record_candidate_evidence(
        run_id: str,
        fanout_id: str,
        candidate_id: str,
        request: CandidateEvidenceRequest,
    ) -> dict[str, Any]:
        require_owner_api()
        try:
            fanout = runs.candidate_fanouts.get_fanout(fanout_id)
            if fanout.run_id != run_id:
                raise KeyError(f"Unknown candidate fanout: {fanout_id}")
            candidate = next(
                (
                    item
                    for item in fanout.candidates
                    if item.candidate_id == candidate_id
                ),
                None,
            )
            if candidate is None:
                raise KeyError(f"Unknown candidate: {candidate_id}")
            recorded = runs.candidate_fanouts.record_result(
                candidate_id=candidate_id,
                task_contract_digest=request.task_contract_digest,
                validation_id=request.validation_id,
                reviews=tuple(
                    (
                        item.review_id,
                        item.reviewer_id,
                        item.evidence_ref,
                    )
                    for item in request.reviews
                ),
                actual_cost_usd=request.actual_cost_usd,
                latency_seconds=request.latency_seconds,
                result=request.result,
            )
        except KeyError as exc:
            raise http_exception(status_code=404, detail=str(exc)) from exc
        except (PermissionError, ValueError) as exc:
            raise http_exception(status_code=409, detail=str(exc)) from exc
        payload = recorded.to_payload()
        runs.events.publish(run_id, "candidate.evidence.recorded", payload)
        return cast(dict[str, Any], payload)

    @app.post(  # type: ignore[untyped-decorator]
        "/api/runs/{run_id}/candidate-fanouts/{fanout_id}/select"
    )
    def select_candidate(run_id: str, fanout_id: str) -> dict[str, Any]:
        require_owner_api()
        try:
            fanout = runs.candidate_fanouts.get_fanout(fanout_id)
            if fanout.run_id != run_id:
                raise KeyError(f"Unknown candidate fanout: {fanout_id}")
            selection = runs.candidate_fanouts.select(
                fanout_id=fanout_id,
                actor="reviewer_gate",
            )
        except KeyError as exc:
            raise http_exception(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise http_exception(status_code=409, detail=str(exc)) from exc
        payload = selection.to_payload()
        runs.events.publish(run_id, "candidate.selected", payload)
        return cast(dict[str, Any], payload)

    @app.get("/api/runs/{run_id}/browser-validations")  # type: ignore[untyped-decorator]
    def list_browser_validations(
        run_id: str,
        candidate_id: str | None = None,
    ) -> dict[str, Any]:
        try:
            state.get_run(run_id)
            items = runs.browser_validations.list(
                run_id=run_id,
                candidate_id=candidate_id,
            )
        except KeyError as exc:
            raise http_exception(status_code=404, detail=str(exc)) from exc
        return {
            "run_id": run_id,
            "candidate_id": candidate_id,
            "items": [item.to_payload() for item in items],
        }

    @app.post("/api/runs/{run_id}/browser-validations")  # type: ignore[untyped-decorator]
    def validate_browser(
        run_id: str,
        request: BrowserValidationAPIRequest,
    ) -> dict[str, object]:
        require_owner_api()
        try:
            state.get_run(run_id)
            execution = runs.invoke_tool(
                tool_name="browser.validate",
                arguments={
                    "task_id": request.task_id,
                    "candidate_id": request.candidate_id,
                    "expected_candidate_digest": request.expected_candidate_digest,
                    "image": request.image,
                    "start_command": request.start_command,
                    "target_url": request.target_url,
                    "assertions": [
                        item.model_dump() for item in request.assertions
                    ],
                    "interactions": [
                        item.model_dump() for item in request.interactions
                    ],
                    "allowed_domains": request.allowed_domains,
                    "network_fixtures": request.network_fixtures,
                    "timeout_seconds": request.timeout_seconds,
                },
                session_id="api",
                run_id=run_id,
            )
            return execution_response(execution)
        except KeyError as exc:
            raise http_exception(status_code=404, detail=str(exc)) from exc

    @app.get("/api/runs/{run_id}/approval-packets")  # type: ignore[untyped-decorator]
    def list_approval_packets(run_id: str) -> dict[str, Any]:
        try:
            state.get_run(run_id)
            packets = runs.approval_packets.list(run_id=run_id)
        except KeyError as exc:
            raise http_exception(status_code=404, detail=str(exc)) from exc
        return {
            "run_id": run_id,
            "items": [packet.to_payload() for packet in packets],
        }

    @app.post("/api/runs/{run_id}/approval-packets")  # type: ignore[untyped-decorator]
    def create_approval_packet(
        run_id: str,
        request: ApprovalPacketCreateRequest,
    ) -> dict[str, Any]:
        require_owner_api()
        try:
            return cast(
                dict[str, Any],
                runs.create_approval_packet(
                    packet_id=request.packet_id,
                    run_id=run_id,
                    objective=request.objective,
                    checkpoint=request.checkpoint,
                    calls=tuple(item.model_dump() for item in request.calls),
                    actor="planner",
                ),
            )
        except KeyError as exc:
            raise http_exception(status_code=404, detail=str(exc)) from exc
        except PermissionError as exc:
            raise http_exception(status_code=403, detail=str(exc)) from exc
        except ValueError as exc:
            raise http_exception(status_code=409, detail=str(exc)) from exc

    @app.post(  # type: ignore[untyped-decorator]
        "/api/runs/{run_id}/approval-packets/{packet_id}/decision"
    )
    def decide_approval_packet(
        run_id: str,
        packet_id: str,
        request: ApprovalPacketDecisionRequest,
    ) -> dict[str, Any]:
        require_owner_api()
        try:
            packet = runs.approval_packets.get(packet_id)
            if packet.run_id != run_id:
                raise KeyError(f"Unknown approval packet: {packet_id}")
            return cast(
                dict[str, Any],
                runs.decide_approval_packet(
                    packet_id,
                    expected_packet_digest=request.expected_packet_digest,
                    decisions=request.decisions,
                    actor="owner",
                ),
            )
        except KeyError as exc:
            raise http_exception(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise http_exception(status_code=409, detail=str(exc)) from exc

    @app.get("/api/outcomes")  # type: ignore[untyped-decorator]
    def outcome_dashboard(
        project_id: str | None = None,
        task_family: str | None = None,
        provider: str | None = None,
        model: str | None = None,
        policy_id: str | None = None,
        since: str | None = None,
    ) -> dict[str, Any]:
        try:
            return cast(
                dict[str, Any],
                runs.outcomes.report(
                    project_id=project_id,
                    task_family=task_family,
                    provider=provider,
                    model=model,
                    policy_id=policy_id,
                    since=since,
                ),
            )
        except ValueError as exc:
            raise http_exception(status_code=400, detail=str(exc)) from exc

    @app.get("/api/outcomes/export")  # type: ignore[untyped-decorator]
    def export_outcomes(
        project_id: str | None = None,
        since: str | None = None,
    ) -> dict[str, Any]:
        try:
            report = runs.outcomes.report(project_id=project_id, since=since)
        except ValueError as exc:
            raise http_exception(status_code=400, detail=str(exc)) from exc
        return {
            "schema": "kestrel.outcome_analytics_export.v1",
            "redacted": True,
            "report": report,
        }

    @app.get("/api/benchmarks")  # type: ignore[untyped-decorator]
    def list_benchmarks(project_id: str | None = None) -> dict[str, Any]:
        try:
            items = runs.outcomes.list_benchmarks(project_id=project_id)
        except ValueError as exc:
            raise http_exception(status_code=400, detail=str(exc)) from exc
        return {
            "items": [item.to_payload() for item in items],
            "count": len(items),
        }

    @app.post("/api/benchmarks", status_code=201)  # type: ignore[untyped-decorator]
    def create_benchmark(request: BenchmarkCreateRequest) -> dict[str, Any]:
        require_owner_api()
        try:
            record = runs.outcomes.create_benchmark(
                case_id=request.case_id,
                project_id=request.project_id,
                name=request.name,
                task_family=request.task_family,
                risk=request.risk,
                fixture=request.fixture,
                acceptance_criteria=tuple(request.acceptance_criteria),
                actor="owner",
            )
        except KeyError as exc:
            raise http_exception(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise http_exception(status_code=409, detail=str(exc)) from exc
        return cast(dict[str, Any], record.to_payload())

    @app.get("/api/benchmarks/{case_id}")  # type: ignore[untyped-decorator]
    def get_benchmark(case_id: str) -> dict[str, Any]:
        try:
            case = runs.outcomes.get_benchmark(case_id)
            replays = runs.outcomes.list_replays(case_id=case_id)
        except KeyError as exc:
            raise http_exception(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise http_exception(status_code=400, detail=str(exc)) from exc
        return {
            **case.to_payload(),
            "replays": [item.to_payload() for item in replays],
        }

    @app.post(  # type: ignore[untyped-decorator]
        "/api/benchmarks/{case_id}/replays",
        status_code=201,
    )
    def replay_benchmark(
        case_id: str,
        request: BenchmarkReplayRequest,
    ) -> dict[str, Any]:
        require_owner_api()
        if not request.launch and request.existing_run_id is None:
            raise http_exception(
                status_code=400,
                detail="a non-launching replay requires existing_run_id",
            )
        try:
            return cast(
                dict[str, Any],
                runs.start_benchmark_replay(
                    replay_id=request.replay_id,
                    case_id=case_id,
                    route_policy_id=request.route_policy_id,
                    context_strategy=request.context_strategy,
                    baseline=request.baseline,
                    existing_run_id=request.existing_run_id,
                    actor="owner",
                ),
            )
        except KeyError as exc:
            raise http_exception(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise http_exception(status_code=409, detail=str(exc)) from exc

    @app.get(  # type: ignore[untyped-decorator]
        "/api/runs/{run_id}/github-change-requests"
    )
    def list_github_change_requests(run_id: str) -> dict[str, Any]:
        try:
            state.get_run(run_id)
            items = runs.github_workflow.list(run_id=run_id)
        except KeyError as exc:
            raise http_exception(status_code=404, detail=str(exc)) from exc
        return {
            "run_id": run_id,
            "items": [item.to_payload() for item in items],
        }

    @app.post(  # type: ignore[untyped-decorator]
        "/api/runs/{run_id}/github-change-requests",
        status_code=201,
    )
    def prepare_github_change_request(
        run_id: str,
        request: GitHubChangeRequestPrepareRequest,
    ) -> dict[str, Any]:
        require_owner_api()
        try:
            return cast(
                dict[str, Any],
                runs.prepare_github_change_request(
                    request_id=request.request_id,
                    run_id=run_id,
                    review_id=request.review_id,
                    title=request.title,
                    base_branch=request.base_branch,
                    head_branch=request.head_branch,
                    actor="owner",
                ),
            )
        except KeyError as exc:
            raise http_exception(status_code=404, detail=str(exc)) from exc
        except (PermissionError, RuntimeError, ValueError) as exc:
            raise http_exception(status_code=409, detail=str(exc)) from exc

    @app.get(  # type: ignore[untyped-decorator]
        "/api/github-change-requests/{request_id}"
    )
    def get_github_change_request(request_id: str) -> dict[str, Any]:
        try:
            return cast(
                dict[str, Any],
                runs.github_workflow.get(request_id).to_payload(),
            )
        except KeyError as exc:
            raise http_exception(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise http_exception(status_code=400, detail=str(exc)) from exc

    @app.post(  # type: ignore[untyped-decorator]
        "/api/github-change-requests/{request_id}/actions/recover"
    )
    def recover_github_change_request(request_id: str) -> dict[str, Any]:
        require_owner_api()
        try:
            return cast(
                dict[str, Any],
                runs.recover_github_feedback(request_id, actor="owner"),
            )
        except KeyError as exc:
            raise http_exception(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise http_exception(status_code=409, detail=str(exc)) from exc


def _permitted_tools(*, state: Any, runs: Any, run: Any) -> set[str]:
    registry = runs.build_registry()
    list_specs = getattr(registry, "all_specs", None)
    specs = list_specs() if callable(list_specs) else registry.specs()
    enabled = {
        str(spec.name)
        for spec in specs
        if runs.capabilities.tool_decision(spec).effective_enabled
    }
    if run.project_id is None:
        return enabled
    project = state.get_project(run.project_id)
    ceiling = set(project.capability_ceiling)
    return {name for name in enabled if f"tool:{name}" in ceiling}
