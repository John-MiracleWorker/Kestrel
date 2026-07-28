from __future__ import annotations

import io
import json
import tomllib
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from nested_memvid_agent.launcher import (
    LauncherApplication,
    _default_application,
    build_parser,
    run,
)
from nested_memvid_agent.server_client import (
    KestrelServerClient,
    ServerClientError,
    ServerProbe,
)
from nested_memvid_agent.service_control import (
    ServiceManagement,
    ServicePaths,
    ServiceState,
    ServiceStatus,
)


class FakeController:
    def __init__(self, status: ServiceStatus) -> None:
        self.current = status
        self.calls: list[str] = []

    def status(self) -> ServiceStatus:
        self.calls.append("status")
        return self.current

    def start(self, **_kwargs: object) -> ServiceStatus:
        self.calls.append("start")
        return self.current

    def stop(self, **_kwargs: object) -> ServiceStatus:
        self.calls.append("stop")
        return self.current


class FakeClient:
    def __init__(
        self,
        *,
        probe: ServerProbe | None = None,
        provider: str = "mock",
        model: str = "mock",
        mode: str = "demo",
        next_action: str = "Run `kestrel chat`.",
        runs: list[dict[str, Any] | BaseException] | None = None,
    ) -> None:
        self.probe_result = probe or ServerProbe(True, True, False)
        self.runtime = {"provider": {"name": provider, "model": model}}
        self.readiness = {
            "experience_mode": mode,
            "next_action": next_action,
            "ready": mode != "model_not_connected",
            "checks": [],
        }
        self.runs = list(runs or [])
        self.calls: list[tuple[str, object]] = []
        self.created_payloads: list[dict[str, object]] = []

    def probe(self) -> ServerProbe:
        self.calls.append(("probe", None))
        return self.probe_result

    def get_runtime_config(self) -> dict[str, Any]:
        self.calls.append(("runtime", None))
        return self.runtime

    def get_setup_readiness(self) -> dict[str, Any]:
        self.calls.append(("readiness", None))
        return self.readiness

    def create_run(self, **payload: object) -> dict[str, Any]:
        self.calls.append(("create", payload))
        self.created_payloads.append(payload)
        return {"run_id": f"run_{len(self.created_payloads)}", "status": "queued"}

    def wait_for_run(self, run_id: str, **kwargs: object) -> dict[str, Any]:
        self.calls.append(("wait", {"run_id": run_id, **kwargs}))
        if not self.runs:
            return {
                "run_id": run_id,
                "status": "completed",
                "assistant_message": "mock response",
            }
        result = self.runs.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result


def _paths(tmp_path: Path) -> ServicePaths:
    home = tmp_path / "home"
    return ServicePaths(
        home=home,
        state_path=home / ".nest" / "state" / "agent.db",
        memory_dir=home / ".nest" / "memory",
        log_path=home / ".nest" / "server.log",
        pid_path=home / ".nest" / "server.pid",
        supervisor_pid_path=home / ".nest" / "server.supervisor.pid",
        pgid_path=home / ".nest" / "server.pgid",
        lifecycle_lock_path=home / ".nest" / "server.lifecycle.lock",
        supervisor_script=home / "scripts" / "installer-server-supervisor.sh",
        server_executable=home / ".venv" / "bin" / "nest-agent",
        host="127.0.0.1",
        port=18765,
        url="http://127.0.0.1:18765/",
    )


def _status(
    *,
    state: ServiceState = ServiceState.RUNNING,
    management: ServiceManagement = ServiceManagement.MANAGED,
    detail: str = "verified",
) -> ServiceStatus:
    return ServiceStatus(
        state=state,
        management=management,
        url="http://127.0.0.1:18765/",
        pid=201 if state == ServiceState.RUNNING else None,
        supervisor_pid=200 if management == ServiceManagement.MANAGED else None,
        pgid=201 if state == ServiceState.RUNNING else None,
        detail=detail,
    )


def _application(
    tmp_path: Path,
    *,
    controller: FakeController | None = None,
    client: FakeClient | None = None,
    browser_open: Any | None = None,
    offline_doctor: Any | None = None,
    input_fn: Any | None = None,
    environ: Mapping[str, str] | None = None,
    clock: Any | None = None,
    sleep: Any | None = None,
) -> tuple[LauncherApplication, io.StringIO, io.StringIO]:
    stdout = io.StringIO()
    stderr = io.StringIO()
    app = LauncherApplication(
        paths=_paths(tmp_path),
        controller=controller or FakeController(_status()),
        client=client or FakeClient(),
        browser_open=browser_open or (lambda _url: True),
        offline_doctor=offline_doctor or (lambda _paths: {"ok": True}),
        input_fn=input_fn or (lambda _prompt: (_ for _ in ()).throw(EOFError)),
        stdout=stdout,
        stderr=stderr,
        session_id_factory=lambda: "session-fixed",
        environ={} if environ is None else environ,
        clock=clock or (lambda: 0.0),
        sleep=sleep or (lambda _seconds: None),
    )
    return app, stdout, stderr


def test_product_parser_exposes_only_the_everyday_commands() -> None:
    parser = build_parser()

    for command in ("start", "stop", "status", "open", "chat", "doctor"):
        args = parser.parse_args([command])
        assert args.command == command

    status = parser.parse_args(
        ["--home", "/tmp/kestrel", "--port", "18765", "status", "--json"]
    )
    assert status.home == Path("/tmp/kestrel")
    assert status.port == 18765
    assert status.json is True

    opened = parser.parse_args(["open", "--no-browser"])
    assert opened.no_browser is True

    chat = parser.parse_args(
        ["chat", "--message", "hello", "--json", "--wait-timeout", "9"]
    )
    assert chat.message == "hello"
    assert chat.json is True
    assert chat.wait_timeout == 9


def test_packaging_keeps_compatibility_commands_and_adds_kestrel() -> None:
    project = tomllib.loads(
        (Path(__file__).parents[1] / "pyproject.toml").read_text(
            encoding="utf-8"
        )
    )

    scripts = project["project"]["scripts"]
    assert scripts["nested-memvid"] == "nested_memvid_agent.cli:main"
    assert scripts["nest-agent"] == "nested_memvid_agent.cli:main"
    assert scripts["kestrel"] == "nested_memvid_agent.launcher:main"


@pytest.mark.parametrize(
    ("mode", "expected_exit"),
    [
        ("demo", 0),
        ("model_not_connected", 1),
        ("connected", 0),
    ],
)
def test_status_json_reports_service_provider_mode_and_next_action(
    tmp_path: Path,
    mode: str,
    expected_exit: int,
) -> None:
    client = FakeClient(
        provider="openai-compatible" if mode != "demo" else "mock",
        model="local-model" if mode != "demo" else "mock",
        mode=mode,
        next_action="Open Settings." if mode == "model_not_connected" else "Chat.",
    )
    app, stdout, _stderr = _application(tmp_path, client=client)

    code = app.execute(build_parser().parse_args(["status", "--json"]))
    payload = json.loads(stdout.getvalue())

    assert code == expected_exit
    assert payload == {
        "service": "running",
        "management": "managed",
        "url": "http://127.0.0.1:18765/",
        "mode": mode,
        "provider": client.runtime["provider"]["name"],
        "model": client.runtime["provider"]["model"],
        "pid": 201,
        "next_action": client.readiness["next_action"],
        "detail": "verified",
    }


def test_status_human_output_has_fixed_scannable_fields(tmp_path: Path) -> None:
    app, stdout, _stderr = _application(tmp_path)

    code = app.execute(build_parser().parse_args(["status"]))
    output = stdout.getvalue()

    assert code == 0
    assert "Service:   running" in output
    assert "Workbench: http://127.0.0.1:18765/" in output
    assert "Mode:      Demo" in output
    assert "Provider:  mock / mock" in output
    assert "Process:   verified PID 201" in output
    assert "Managed:   yes" in output
    assert "Next:      Run `kestrel chat`." in output


def test_status_reports_stopped_and_conflict_with_stable_exit_codes(
    tmp_path: Path,
) -> None:
    stopped_controller = FakeController(
        _status(
            state=ServiceState.STOPPED,
            management=ServiceManagement.NONE,
            detail="stopped",
        )
    )
    stopped, stopped_out, _ = _application(
        tmp_path,
        controller=stopped_controller,
    )
    conflict_controller = FakeController(
        _status(
            state=ServiceState.CONFLICT,
            management=ServiceManagement.NONE,
            detail="unknown listener",
        )
    )
    conflict, conflict_out, _ = _application(
        tmp_path,
        controller=conflict_controller,
    )

    assert stopped.execute(build_parser().parse_args(["status"])) == 1
    assert "stopped" in stopped_out.getvalue()
    assert conflict.execute(build_parser().parse_args(["status"])) == 2
    assert "conflict" in conflict_out.getvalue()


def test_authenticated_service_is_locked_not_offline_or_ready(
    tmp_path: Path,
) -> None:
    client = FakeClient(
        probe=ServerProbe(True, False, True, "API token required")
    )
    app, stdout, _stderr = _application(tmp_path, client=client)

    code = app.execute(build_parser().parse_args(["status", "--json"]))
    payload = json.loads(stdout.getvalue())

    assert code == 1
    assert payload["service"] == "running"
    assert payload["mode"] == "locked"
    assert payload["provider"] is None
    assert payload["model"] is None
    assert "token" in payload["next_action"].lower()


def test_open_starts_then_verifies_before_opening_browser(
    tmp_path: Path,
) -> None:
    events: list[str] = []
    controller = FakeController(_status())
    original_start = controller.start

    def start(**kwargs: object) -> ServiceStatus:
        events.append("start")
        return original_start(**kwargs)

    controller.start = start  # type: ignore[method-assign]
    client = FakeClient()
    original_probe = client.probe

    def probe() -> ServerProbe:
        events.append("probe")
        return original_probe()

    client.probe = probe  # type: ignore[method-assign]
    app, stdout, _stderr = _application(
        tmp_path,
        controller=controller,
        client=client,
        browser_open=lambda url: events.append(f"open:{url}") or True,
    )

    code = app.execute(build_parser().parse_args(["open"]))

    assert code == 0
    assert events == [
        "start",
        "probe",
        "open:http://127.0.0.1:18765/",
    ]
    assert "opened" in stdout.getvalue().lower()


def test_open_no_browser_and_opener_failure_keep_service_available(
    tmp_path: Path,
) -> None:
    opened: list[str] = []
    no_browser, no_browser_out, _ = _application(
        tmp_path,
        browser_open=lambda url: opened.append(url) or True,
    )

    assert (
        no_browser.execute(
            build_parser().parse_args(["open", "--no-browser"])
        )
        == 0
    )
    assert opened == []
    assert "http://127.0.0.1:18765/" in no_browser_out.getvalue()

    failed, failed_out, failed_err = _application(
        tmp_path,
        browser_open=lambda _url: False,
    )
    assert failed.execute(build_parser().parse_args(["open"])) == 0
    assert "http://127.0.0.1:18765/" in failed_out.getvalue()
    assert "could not open" in failed_err.getvalue().lower()


def test_open_redacts_browser_opener_exception_with_injected_environment(
    tmp_path: Path,
) -> None:
    token = "opaque-browser-token-value"

    def browser_open(_url: str) -> bool:
        raise RuntimeError(f"browser rejected token {token}")

    app, stdout, stderr = _application(
        tmp_path,
        browser_open=browser_open,
        environ={"CUSTOM_BROWSER_TOKEN": token},
    )

    assert app.execute(build_parser().parse_args(["open"])) == 0
    assert "<redacted>" in stderr.getvalue()
    assert token not in stderr.getvalue()
    assert "http://127.0.0.1:18765/" in stdout.getvalue()


def test_one_shot_chat_uses_only_the_server_run_api(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = FakeClient(
        runs=[
            {
                "run_id": "run_1",
                "status": "completed",
                "assistant_message": "server-owned response",
            }
        ]
    )
    monkeypatch.setattr(
        "nested_memvid_agent.run_manager.RunManager",
        lambda *_args, **_kwargs: pytest.fail(
            "product chat must never construct RunManager"
        ),
    )
    app, stdout, _stderr = _application(tmp_path, client=client)

    code = app.execute(
        build_parser().parse_args(["chat", "--message", "hello"])
    )

    assert code == 0
    assert stdout.getvalue().strip() == "server-owned response"
    assert [call[0] for call in client.calls] == ["create", "wait"]
    assert client.created_payloads[0]["message"] == "hello"
    assert client.created_payloads[0]["session_id"] == "session-fixed"


def test_chat_json_blocked_timeout_and_interruption_are_durable(
    tmp_path: Path,
) -> None:
    blocked_client = FakeClient(
        runs=[
            {
                "run_id": "run_blocked",
                "status": "blocked",
                "assistant_message": "Approval required",
                "stop_reason": "approval_required",
            }
        ]
    )
    blocked, blocked_out, _ = _application(tmp_path, client=blocked_client)
    assert (
        blocked.execute(
            build_parser().parse_args(["chat", "do work", "--json"])
        )
        == 1
    )
    assert json.loads(blocked_out.getvalue())["run_id"] == "run_blocked"

    timeout = ServerClientError(
        "still running",
        code="run_timeout",
        recovery="inspect",
        run_id="run_timeout",
    )
    timeout_client = FakeClient(runs=[timeout])
    timed, timed_out, _ = _application(tmp_path, client=timeout_client)
    assert (
        timed.execute(
            build_parser().parse_args(["chat", "--message", "slow", "--json"])
        )
        == 1
    )
    timeout_payload = json.loads(timed_out.getvalue())
    assert timeout_payload == {
        "run_id": "run_1",
        "status": "active",
        "durable": True,
        "cancelled": False,
        "workbench_url": "http://127.0.0.1:18765/",
    }
    assert not any("cancel" in str(call) for call in timeout_client.calls)

    interrupted_client = FakeClient(runs=[KeyboardInterrupt()])
    interrupted, interrupted_out, _ = _application(
        tmp_path,
        client=interrupted_client,
    )
    assert (
        interrupted.execute(
            build_parser().parse_args(
                ["chat", "--message", "interrupt", "--json"]
            )
        )
        == 130
    )
    interrupted_payload = json.loads(interrupted_out.getvalue())
    assert interrupted_payload == {
        "run_id": "run_1",
        "status": "active",
        "durable": True,
        "cancelled": False,
        "workbench_url": "http://127.0.0.1:18765/",
    }
    assert not any("cancel" in str(call) for call in interrupted_client.calls)


def test_interactive_chat_reuses_one_session_id(tmp_path: Path) -> None:
    prompts = iter(["first", "second"])

    def input_fn(_prompt: str) -> str:
        try:
            return next(prompts)
        except StopIteration:
            raise EOFError from None

    client = FakeClient()
    app, stdout, _stderr = _application(
        tmp_path,
        client=client,
        input_fn=input_fn,
    )

    assert app.execute(build_parser().parse_args(["chat"])) == 0
    assert [payload["session_id"] for payload in client.created_payloads] == [
        "session-fixed",
        "session-fixed",
    ]
    assert stdout.getvalue().count("mock response") == 2


def test_chat_wait_uses_the_application_clock_and_sleep(tmp_path: Path) -> None:
    client = FakeClient()

    def clock() -> float:
        return 5.0

    def sleep(_seconds: float) -> None:
        return None

    app, _stdout, _stderr = _application(
        tmp_path,
        client=client,
        clock=clock,
        sleep=sleep,
    )

    assert app.execute(build_parser().parse_args(["chat", "--message", "hello"])) == 0

    wait = next(payload for call, payload in client.calls if call == "wait")
    assert wait["clock"] is clock
    assert wait["sleep"] is sleep


def test_default_application_uses_injected_environment_for_shared_client(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token = "opaque-injected-launch-token"
    environment = {
        "NEST_AGENT_API_AUTH_TOKEN_ENV": "INJECTED_KESTREL_TOKEN",
        "INJECTED_KESTREL_TOKEN": token,
    }
    monkeypatch.setenv("NEST_AGENT_API_TOKEN", "host-secret-must-not-be-used")
    observed: dict[str, object] = {}

    def client_factory(
        base_url: str,
        *,
        environ: Mapping[str, str],
    ) -> KestrelServerClient:
        observed["environ"] = environ
        return KestrelServerClient(base_url, environ=environ)

    def clock() -> float:
        return 0.0

    def sleep(_seconds: float) -> None:
        return None

    app = _default_application(
        _paths(tmp_path),
        client_factory=client_factory,
        environ=environment,
        clock=clock,
        sleep=sleep,
    )

    assert observed["environ"] is environment
    assert app.client is app.controller.client
    assert app.client._token() == token
    assert app.environ is environment
    assert app.clock is clock
    assert app.sleep is sleep


def test_run_passes_the_exact_injected_environment_to_the_default_factory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "installed-kestrel"
    (home / "scripts").mkdir(parents=True)
    (home / "scripts" / "installer-server-supervisor.sh").touch()
    (home / ".venv" / "bin").mkdir(parents=True)
    (home / ".venv" / "bin" / "nest-agent").touch()
    environment = {"NEST_AGENT_API_TOKEN": "opaque-run-token"}
    observed: dict[str, object] = {}

    class Application:
        def execute(self, _args: object) -> int:
            return 0

    def default_factory(_paths: ServicePaths, **kwargs: object) -> Application:
        observed.update(kwargs)
        return Application()

    monkeypatch.setattr(
        "nested_memvid_agent.launcher._default_application",
        default_factory,
    )

    assert run(["--home", str(home), "status"], environ=environment) == 0
    assert observed["environ"] is environment


def test_run_returns_argparse_usage_error_without_raising_system_exit(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert run(["not-a-kestrel-command"]) == 2
    assert "usage: kestrel" in capsys.readouterr().err


def test_doctor_routes_running_stopped_and_conflict_without_owner_collision(
    tmp_path: Path,
) -> None:
    offline_calls: list[ServicePaths] = []

    def offline(paths: ServicePaths) -> dict[str, object]:
        offline_calls.append(paths)
        return {"ok": True, "memory": {"ok": True}}

    running, running_out, _ = _application(
        tmp_path,
        offline_doctor=offline,
    )
    assert (
        running.execute(
            build_parser().parse_args(["doctor", "--json"])
        )
        == 0
    )
    assert json.loads(running_out.getvalue())["service"]["state"] == "running"
    assert offline_calls == []

    stopped_controller = FakeController(
        _status(
            state=ServiceState.STOPPED,
            management=ServiceManagement.NONE,
        )
    )
    stopped, stopped_out, _ = _application(
        tmp_path,
        controller=stopped_controller,
        offline_doctor=offline,
    )
    assert (
        stopped.execute(
            build_parser().parse_args(["doctor", "--json"])
        )
        == 0
    )
    assert json.loads(stopped_out.getvalue())["memory"]["ok"] is True
    assert offline_calls == [_paths(tmp_path)]

    conflict_controller = FakeController(
        _status(
            state=ServiceState.CONFLICT,
            management=ServiceManagement.NONE,
            detail="ambiguous listener",
        )
    )
    conflict, conflict_out, _ = _application(
        tmp_path,
        controller=conflict_controller,
        offline_doctor=offline,
    )
    assert (
        conflict.execute(
            build_parser().parse_args(["doctor", "--json"])
        )
        == 2
    )
    assert json.loads(conflict_out.getvalue())["service"]["state"] == "conflict"
    assert offline_calls == [_paths(tmp_path)]
