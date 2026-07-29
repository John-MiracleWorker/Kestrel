from __future__ import annotations

import base64
import hashlib
import json
import subprocess
from pathlib import Path

import pytest

from nested_memvid_agent.config import AgentConfig
from nested_memvid_agent.engineering.browser_validation import (
    BrowserAssertion,
    BrowserInteraction,
    BrowserValidationRequest,
    BrowserValidationService,
)
from nested_memvid_agent.extension_runner import (
    ContainerExecutionRequest,
    ContainerExecutionResult,
)
from nested_memvid_agent.orchestrator import build_memory_system
from nested_memvid_agent.repair_integrity import repair_snapshot
from nested_memvid_agent.runtime_models import ToolCall
from nested_memvid_agent.security_boundary import register_secret_value
from nested_memvid_agent.state_store import AgentStateStore
from nested_memvid_agent.tools.base import ToolContext
from nested_memvid_agent.tools.browser_tools import BrowserValidateTool
from nested_memvid_agent.tools.registry import ToolRegistry

_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR4nGNgYAAAAAMAASsJTYQAAAAASUVORK5CYII="
)


class _Runner:
    def __init__(
        self,
        *,
        serious_violation: bool = False,
        omit_assertions: bool = False,
    ) -> None:
        self.requests: list[ContainerExecutionRequest] = []
        self.serious_violation = serious_violation
        self.omit_assertions = omit_assertions

    def run(self, request: ContainerExecutionRequest) -> ContainerExecutionResult:
        self.requests.append(request)
        spec = json.loads(
            base64.urlsafe_b64decode(request.command[-1]).decode("utf-8")
        )
        assertion_results = [
            {
                "selector": item["selector"],
                "expectation": item["expectation"],
                "passed": True,
                "detail": "matched",
            }
            for item in spec["assertions"]
        ]
        interaction_results = [
            {
                "action": item["action"],
                "selector": item["selector"],
                "passed": True,
                "detail": "completed",
            }
            for item in spec["interactions"]
        ]
        report = {
            "schema": "kestrel.browser_validation.v1",
            "rendered": True,
            "target_url": spec["target_url"],
            "assertions": assertion_results if not self.omit_assertions else [],
            "interactions": interaction_results,
            "console_errors": [],
            "network_errors": [],
            "accessibility": {
                "violations": (
                    [{"id": "color-contrast", "impact": "serious", "nodes": 1}]
                    if self.serious_violation
                    else []
                )
            },
            "dom_summary": {
                "title": "Dashboard",
                "url": spec["target_url"],
                "landmarks": ["main"],
                "headings": [],
                "text_excerpt": "Ready",
            },
            "screenshot": {
                "media_type": "image/png",
                "data_base64": base64.b64encode(_PNG).decode(),
                "sha256": hashlib.sha256(_PNG).hexdigest(),
                "width": 1,
                "height": 1,
            },
        }
        return ContainerExecutionResult(
            success=True,
            stdout=json.dumps(report),
            returncode=0,
            tree_digest=request.expected_tree_digest,
        )


def _repo(tmp_path: Path) -> Path:
    repository = tmp_path / "repo"
    repository.mkdir()
    subprocess.run(["git", "init", "-b", "main"], cwd=repository, check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.invalid"],
        cwd=repository,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"],
        cwd=repository,
        check=True,
    )
    (repository / "index.html").write_text("<main>Ready</main>\n", encoding="utf-8")
    subprocess.run(["git", "add", "index.html"], cwd=repository, check=True)
    subprocess.run(["git", "commit", "-m", "baseline"], cwd=repository, check=True)
    (repository / "index.html").write_text(
        "<main data-testid='ready'>Ready</main>\n",
        encoding="utf-8",
    )
    return repository


def _state(tmp_path: Path, repository: Path) -> AgentStateStore:
    state = AgentStateStore(tmp_path / "state.sqlite3")
    state.create_run(
        run_id="run_browser",
        message="Validate dashboard",
        session_id="session",
        workspace=str(repository),
        provider="mock",
        model="mock",
    )
    state.create_task_node(
        task_id="task_browser",
        run_id="run_browser",
        title="Validate dashboard",
        goal="Prove dashboard behavior.",
        status="running",
        approved=True,
        acceptance_criteria=("Dashboard renders and is accessible.",),
    )
    return state


def _request(**overrides: object) -> BrowserValidationRequest:
    values: dict[str, object] = {
        "run_id": "run_browser",
        "task_id": "task_browser",
        "candidate_id": None,
        "workspace": Path("."),
        "image": "playwright@sha256:" + "1" * 64,
        "start_command": ("npm", "run", "preview", "--", "--host", "127.0.0.1"),
        "target_url": "http://127.0.0.1:4173/dashboard",
        "assertions": (
            BrowserAssertion(
                selector="[data-testid=ready]",
                expectation="visible",
            ),
        ),
        "interactions": (
            BrowserInteraction(action="click", selector="button"),
        ),
        "allowed_domains": (),
        "network_fixtures": {},
        "timeout_seconds": 60.0,
    }
    values.update(overrides)
    return BrowserValidationRequest(**values)  # type: ignore[arg-type]


def test_browser_validation_is_digest_bound_network_none_and_reviewable(
    tmp_path: Path,
) -> None:
    repository = _repo(tmp_path)
    state = _state(tmp_path, repository)
    runner = _Runner()
    service = BrowserValidationService(state, runner=runner)
    expected = repair_snapshot(repository)["diff_digest"]

    record = service.validate(
        _request(workspace=repository),
        expected_candidate_digest=expected,
    )

    assert record.status == "passed"
    assert record.candidate_digest == expected
    assert record.network_policy == {
        "mode": "none",
        "allowed_domains": [],
        "live_egress": False,
    }
    assert record.screenshot_sha256 == hashlib.sha256(_PNG).hexdigest()
    assert record.report["screenshot"]["data_url"].startswith("data:image/png;base64,")
    assert record.evidence_refs == (
        f"browser_validation:{record.validation_id}",
        f"candidate_digest:{expected}",
    )
    request = runner.requests[0]
    assert request.scopes.network == "none"
    assert request.image.endswith("1" * 64)


def test_serious_accessibility_violation_fails_without_losing_artifact(
    tmp_path: Path,
) -> None:
    repository = _repo(tmp_path)
    state = _state(tmp_path, repository)
    service = BrowserValidationService(state, runner=_Runner(serious_violation=True))

    record = service.validate(
        _request(workspace=repository),
        expected_candidate_digest=repair_snapshot(repository)["diff_digest"],
    )

    assert record.status == "failed"
    assert "serious_accessibility_violation" in record.failure_codes
    persisted = service.get(record.validation_id)
    assert persisted.screenshot_sha256 == record.screenshot_sha256


def test_browser_report_cannot_omit_requested_evidence(tmp_path: Path) -> None:
    repository = _repo(tmp_path)
    state = _state(tmp_path, repository)
    service = BrowserValidationService(state, runner=_Runner(omit_assertions=True))

    with pytest.raises(ValueError, match="assertions does not match request"):
        service.validate(
            _request(workspace=repository),
            expected_candidate_digest=repair_snapshot(repository)["diff_digest"],
        )

    assert service.list(run_id="run_browser") == []


def test_browser_validation_allows_only_deterministic_domain_fixtures(
    tmp_path: Path,
) -> None:
    repository = _repo(tmp_path)
    state = _state(tmp_path, repository)
    service = BrowserValidationService(state, runner=_Runner())

    with pytest.raises(ValueError, match="fixture"):
        service.validate(
            _request(
                workspace=repository,
                allowed_domains=("api.example.com",),
                network_fixtures={},
            ),
            expected_candidate_digest=repair_snapshot(repository)["diff_digest"],
        )

    record = service.validate(
        _request(
            workspace=repository,
            allowed_domains=("api.example.com",),
            network_fixtures={
                "https://api.example.com/v1/status": {
                    "status": 200,
                    "content_type": "application/json",
                    "body": '{"ok":true}',
                }
            },
        ),
        expected_candidate_digest=repair_snapshot(repository)["diff_digest"],
    )
    assert record.network_policy["mode"] == "fixture_allowlist"
    assert record.network_policy["allowed_domains"] == ["api.example.com"]
    assert record.network_policy["live_egress"] is False


def test_browser_validation_refuses_registered_secrets_before_container_execution(
    tmp_path: Path,
) -> None:
    repository = _repo(tmp_path)
    state = _state(tmp_path, repository)
    runner = _Runner()
    service = BrowserValidationService(state, runner=runner)
    secret = "browser-container-secret-49827"
    register_secret_value(secret)

    with pytest.raises(ValueError, match="invalid token"):
        service.validate(
            _request(
                workspace=repository,
                start_command=("npm", "run", "preview", f"--token={secret}"),
            ),
            expected_candidate_digest=repair_snapshot(repository)["diff_digest"],
        )

    assert runner.requests == []


def test_candidate_validation_uses_the_bound_candidate_workspace(
    tmp_path: Path,
) -> None:
    repository = _repo(tmp_path)
    state = _state(tmp_path, repository)
    candidate_root = tmp_path / "candidate-root"
    candidate_root.mkdir()
    candidate = _repo(candidate_root)
    (candidate / "index.html").write_text(
        "<main data-testid='ready'>Candidate only</main>\n",
        encoding="utf-8",
    )
    service = BrowserValidationService(state, runner=_Runner())
    with state._connect() as connection:
        connection.execute(
            """
            INSERT INTO candidate_fanouts (
                fanout_id, run_id, source_task_id, task_contract_digest,
                plan_digest, status, estimated_budget_delta_usd, actor,
                selected_candidate_id, created_at, selected_at
            ) VALUES (?, ?, ?, ?, ?, 'running', 0, 'test', NULL, ?, NULL)
            """,
            (
                "fanout_browser",
                "run_browser",
                "task_browser",
                "a" * 64,
                "b" * 64,
                "2026-07-28T12:00:00+00:00",
            ),
        )
        connection.execute(
            """
            INSERT INTO candidate_attempts (
                candidate_id, fanout_id, run_id, task_id,
                task_contract_digest, workspace, branch, workspace_identity,
                status, candidate_digest, validation_id, validation_passed,
                validation_evidence_refs_json, review_artifact_refs_json,
                reviewer_identities_json, reviewer_evidence_refs_json,
                changed_file_count, changed_line_count, risk_notes_json,
                actual_cost_usd, latency_seconds, evidence_retained,
                result_json, created_at, finished_at
            ) VALUES (?, ?, ?, ?, ?, ?, 'main', ?, 'running', NULL, NULL, NULL,
                '[]', '[]', '[]', '[]', NULL, NULL, '[]', NULL, NULL, 1,
                '{}', ?, NULL)
            """,
            (
                "candidate_browser",
                "fanout_browser",
                "run_browser",
                "task_browser",
                "a" * 64,
                str(candidate),
                str(candidate.resolve()),
                "2026-07-28T12:00:00+00:00",
            ),
        )

    record = service.validate(
        _request(
            workspace=repository,
            candidate_id="candidate_browser",
        ),
        expected_candidate_digest=repair_snapshot(candidate)["diff_digest"],
    )

    assert record.candidate_id == "candidate_browser"
    assert record.candidate_digest == repair_snapshot(candidate)["diff_digest"]


def test_browser_tool_is_default_off_and_preserves_exact_call_identity(
    tmp_path: Path,
) -> None:
    repository = _repo(tmp_path)
    state = _state(tmp_path, repository)
    memory = build_memory_system("memory", tmp_path / "memory")
    runner = _Runner()
    registry = ToolRegistry()
    registry.register(BrowserValidateTool(runner=runner))
    arguments = {
        "task_id": "task_browser",
        "expected_candidate_digest": repair_snapshot(repository)["diff_digest"],
        "start_command": ["npm", "run", "preview"],
        "target_url": "http://127.0.0.1:4173/dashboard",
    }
    call = ToolCall(name="browser.validate", arguments=arguments, id="browser_call")
    base = AgentConfig(
        state_path=state.path,
        workspace=repository,
        validation_container_image="playwright@sha256:" + "1" * 64,
    )

    disabled = registry.execute(
        call,
        ToolContext(
            memory=memory,
            config=base,
            workspace=repository,
            run_id="run_browser",
            approved_tool_call_ids=frozenset({call.id}),
            approved_tool_call_arguments={call.id: call.arguments},
        ),
    )
    assert disabled.error == "tool_disabled"

    enabled = registry.execute(
        call,
        ToolContext(
            memory=memory,
            config=AgentConfig(
                **{
                    **base.__dict__,
                    "allow_browser_validation": True,
                }
            ),
            workspace=repository,
            run_id="run_browser",
            approved_tool_call_ids=frozenset({call.id}),
            approved_tool_call_arguments={call.id: call.arguments},
        ),
    )
    assert enabled.success is True
    assert enabled.call.id == "browser_call"
    assert enabled.call.arguments == arguments


def test_browser_validation_rejects_host_or_external_target_urls(tmp_path: Path) -> None:
    repository = _repo(tmp_path)
    state = _state(tmp_path, repository)
    service = BrowserValidationService(state, runner=_Runner())

    with pytest.raises(ValueError, match="container-local"):
        service.validate(
            _request(
                workspace=repository,
                target_url="https://example.com/dashboard",
            ),
            expected_candidate_digest=repair_snapshot(repository)["diff_digest"],
        )
