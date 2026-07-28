from __future__ import annotations

import argparse
import json
import os
import sys
import time
import webbrowser
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, NoReturn, TextIO
from uuid import uuid4

from .config import AgentConfig
from .security_boundary import redact_text
from .server_client import (
    KestrelServerClient,
    ServerClientError,
)
from .service_control import (
    ServiceControlError,
    ServiceController,
    ServiceManagement,
    ServicePaths,
    ServiceState,
    ServiceStatus,
    resolve_kestrel_home,
    resolve_service_paths,
)

_MODE_LABELS = {
    "demo": "Demo",
    "model_not_connected": "Model not connected",
    "connected": "Ready",
    "locked": "Access locked",
    "unknown": "Unavailable",
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="kestrel",
        description="Start, open, chat with, inspect, and stop local Kestrel.",
    )
    parser.add_argument(
        "--home",
        type=Path,
        help="Kestrel installation home (default: resolved launcher installation).",
    )
    parser.add_argument(
        "--port",
        type=int,
        help="Loopback Workbench port (default: KESTREL_PORT or 8765).",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    start = subparsers.add_parser("start", help="Start or reuse Kestrel.")
    start.add_argument(
        "--wait-timeout",
        type=float,
        default=15.0,
        help="Seconds to wait for verified API readiness.",
    )

    subparsers.add_parser("stop", help="Stop only a verified Kestrel service.")

    status = subparsers.add_parser(
        "status",
        help="Inspect service, provider, mode, and next action.",
    )
    status.add_argument("--json", action="store_true", help="Emit stable JSON.")

    opened = subparsers.add_parser(
        "open",
        help="Start or reuse Kestrel and open the Workbench.",
    )
    opened.add_argument(
        "--no-browser",
        action="store_true",
        help="Verify the complete launch path without opening a browser.",
    )
    opened.add_argument(
        "--wait-timeout",
        type=float,
        default=15.0,
        help="Seconds to wait for verified API readiness.",
    )

    chat = subparsers.add_parser(
        "chat",
        help="Chat through the authoritative Kestrel server API.",
    )
    chat.add_argument("prompt", nargs="?", help="One-shot message.")
    chat.add_argument("--message", help="One-shot message.")
    chat.add_argument("--json", action="store_true", help="Emit run JSON.")
    chat.add_argument(
        "--wait-timeout",
        type=float,
        default=120.0,
        help="Seconds to wait locally for a durable run.",
    )
    chat.add_argument(
        "--session-id",
        help="Optional explicit session ID; interactive mode otherwise creates one.",
    )

    doctor = subparsers.add_parser(
        "doctor",
        help="Diagnose through the live owner or safely offline.",
    )
    doctor.add_argument("--json", action="store_true", help="Emit diagnostic JSON.")
    return parser


@dataclass
class LauncherApplication:
    paths: ServicePaths
    controller: Any
    client: Any
    browser_open: Callable[[str], bool]
    offline_doctor: Callable[[ServicePaths], dict[str, Any]]
    input_fn: Callable[[str], str]
    stdout: TextIO
    stderr: TextIO
    session_id_factory: Callable[[], str]
    environ: Mapping[str, str] = field(
        default_factory=lambda: os.environ,
        repr=False,
        compare=False,
    )
    clock: Callable[[], float] = time.monotonic
    sleep: Callable[[float], None] = time.sleep

    def execute(self, args: argparse.Namespace) -> int:
        try:
            if args.command == "start":
                return self._start(args)
            if args.command == "stop":
                return self._stop()
            if args.command == "status":
                return self._status(json_output=bool(args.json))
            if args.command == "open":
                return self._open(args)
            if args.command == "chat":
                return self._chat(args)
            if args.command == "doctor":
                return self._doctor(json_output=bool(args.json))
        except ServiceControlError as exc:
            self._error(exc, recovery=exc.recovery)
            return 2 if exc.code in {
                "service_conflict",
                "identity_changed",
                "unsafe_metadata",
                "cleanup_indeterminate",
            } else 1
        except ServerClientError as exc:
            self._error(exc, recovery=exc.recovery)
            return 2 if exc.code == "conflict" else 1
        except ValueError as exc:
            self._error(exc, recovery="Run `kestrel --help` and correct the command.")
            return 2
        self._error(
            f"Unsupported command: {args.command}",
            recovery="Run `kestrel --help`.",
        )
        return 2

    def _start(self, args: argparse.Namespace) -> int:
        status = self.controller.start(readiness_timeout=args.wait_timeout)
        view = self._status_view(status)
        self._print_status(view)
        return _view_exit_code(view)

    def _stop(self) -> int:
        status = self.controller.stop()
        self.stdout.write(f"{status.detail}\n")
        return 0 if status.state == ServiceState.STOPPED else 1

    def _status(self, *, json_output: bool) -> int:
        view = self._status_view(self.controller.status())
        if json_output:
            self.stdout.write(json.dumps(view, sort_keys=True) + "\n")
        else:
            self._print_status(view)
        return _view_exit_code(view)

    def _open(self, args: argparse.Namespace) -> int:
        status = self.controller.start(readiness_timeout=args.wait_timeout)
        if status.state != ServiceState.RUNNING:
            self._print_status(self._status_view(status))
            return 2 if status.state == ServiceState.CONFLICT else 1
        probe = self.client.probe()
        if not (probe.healthy or probe.locked):
            self._error(
                probe.detail or "Kestrel API readiness could not be verified.",
                recovery=f"Run `kestrel doctor` and inspect {self.paths.log_path}.",
            )
            return 1
        if args.no_browser:
            self.stdout.write(f"Kestrel is running at {status.url}\n")
            return 0
        try:
            opened = bool(self.browser_open(status.url))
        except Exception as exc:  # noqa: BLE001 - opener failure must preserve service
            opened = False
            detail = redact_text(str(exc), environ=self.environ)
            self.stderr.write(
                f"Browser opener failed: {type(exc).__name__}: {detail}\n"
            )
        if opened:
            self.stdout.write(f"Opened Kestrel Workbench at {status.url}\n")
        else:
            self.stderr.write(
                "Could not open a browser automatically; Kestrel remains running.\n"
            )
            self.stdout.write(f"Open {status.url}\n")
        return 0

    def _chat(self, args: argparse.Namespace) -> int:
        if args.prompt is not None and args.message is not None:
            raise ValueError("Provide a positional prompt or --message, not both")
        status = self.controller.start()
        if status.state != ServiceState.RUNNING:
            self._print_status(self._status_view(status))
            return 2 if status.state == ServiceState.CONFLICT else 1
        session_id = args.session_id or self.session_id_factory()
        message = args.message if args.message is not None else args.prompt
        if message is not None:
            return self._chat_turn(
                message,
                session_id=session_id,
                json_output=bool(args.json),
                wait_timeout=args.wait_timeout,
            )
        while True:
            try:
                interactive_message = self.input_fn("You> ")
            except EOFError:
                return 0
            except KeyboardInterrupt:
                self.stdout.write("\n")
                return 130
            if not interactive_message.strip():
                continue
            if interactive_message.strip().lower() in {"/quit", "/exit"}:
                return 0
            self._chat_turn(
                interactive_message,
                session_id=session_id,
                json_output=bool(args.json),
                wait_timeout=args.wait_timeout,
            )

    def _chat_turn(
        self,
        message: str,
        *,
        session_id: str,
        json_output: bool,
        wait_timeout: float,
    ) -> int:
        created = self.client.create_run(
            message=message,
            session_id=session_id,
            workspace=None,
            provider=None,
            model=None,
            autonomy_mode="background",
        )
        run_id = created.get("run_id")
        if not isinstance(run_id, str) or not run_id:
            raise ServerClientError(
                "Kestrel did not return a valid run ID.",
                code="invalid_response",
                recovery="Run `kestrel doctor` and inspect the service log.",
            )
        try:
            run = self.client.wait_for_run(
                run_id,
                timeout_seconds=wait_timeout,
                clock=self.clock,
                sleep=self.sleep,
            )
        except ServerClientError as exc:
            if exc.code != "run_timeout":
                raise
            self._print_durable_wait_result(run_id, json_output=json_output)
            return 1
        except KeyboardInterrupt:
            self._print_durable_wait_result(
                run_id,
                json_output=json_output,
                interrupted=True,
            )
            return 130
        if json_output:
            self.stdout.write(json.dumps(run, sort_keys=True) + "\n")
        status = str(run.get("status") or "")
        if status == "completed":
            if not json_output:
                self.stdout.write(f"{run.get('assistant_message') or ''}\n")
            return 0
        if status == "blocked":
            if not json_output:
                reason = (
                    run.get("assistant_message")
                    or run.get("stop_reason")
                    or "Approval is required."
                )
                self.stdout.write(
                    f"{reason}\nReview the pending approval at {self.paths.url}\n"
                )
            return 1
        if not json_output:
            detail = (
                run.get("assistant_message")
                or run.get("error")
                or f"Run ended with status {status or 'unknown'}."
            )
            self.stdout.write(f"{detail}\nRun: {run_id}\n")
        return 1

    def _print_durable_wait_result(
        self,
        run_id: str,
        *,
        json_output: bool,
        interrupted: bool = False,
    ) -> None:
        if json_output:
            self.stdout.write(
                json.dumps(
                    {
                        "run_id": run_id,
                        "status": "active",
                        "durable": True,
                        "cancelled": False,
                        "workbench_url": self.paths.url,
                    },
                    sort_keys=True,
                )
                + "\n"
            )
            return
        prefix = "\n" if interrupted else ""
        self.stdout.write(
            f"{prefix}Run {run_id} may still be active; it was not cancelled. "
            f"Inspect it at {self.paths.url}\n"
        )

    def _doctor(self, *, json_output: bool) -> int:
        status = self.controller.status()
        if status.state == ServiceState.STOPPED:
            report = self.offline_doctor(self.paths)
            code = 0 if bool(report.get("ok")) else 1
        elif status.state == ServiceState.CONFLICT:
            report = {
                "ok": False,
                "installation": self._installation_report(),
                "service": _service_payload(status),
                "paths": self._path_report(),
                "recovery": (
                    "Ownership is ambiguous. No state database or Memvid layer "
                    "was opened; inspect the listener before retrying."
                ),
            }
            code = 2
        else:
            probe = self.client.probe()
            runtime: dict[str, Any] | None = None
            readiness: dict[str, Any] | None = None
            if probe.healthy:
                runtime = self.client.get_runtime_config()
                readiness = self.client.get_setup_readiness()
            report = {
                "ok": bool(probe.healthy),
                "installation": self._installation_report(),
                "service": _service_payload(status),
                "api": {
                    "reachable": probe.reachable,
                    "healthy": probe.healthy,
                    "locked": probe.locked,
                    "detail": probe.detail,
                },
                "paths": self._path_report(),
                "provider": (
                    runtime.get("provider")
                    if isinstance(runtime, dict)
                    else None
                ),
                "setup": readiness,
            }
            code = 0 if probe.healthy else 1
        if json_output:
            self.stdout.write(json.dumps(report, sort_keys=True) + "\n")
        else:
            self.stdout.write(json.dumps(report, indent=2, sort_keys=True) + "\n")
        return code

    def _status_view(self, status: ServiceStatus) -> dict[str, Any]:
        base = {
            "service": status.state.value,
            "management": status.management.value,
            "url": status.url,
            "mode": "unknown",
            "provider": None,
            "model": None,
            "pid": status.pid,
            "next_action": _service_next_action(status),
            "detail": status.detail,
        }
        if status.state != ServiceState.RUNNING:
            return base
        probe = self.client.probe()
        if probe.locked:
            base["mode"] = "locked"
            base["next_action"] = (
                "Set the configured Kestrel API token environment variable."
            )
            return base
        if not probe.healthy:
            base["next_action"] = "Run `kestrel doctor` and inspect the service log."
            return base
        runtime = self.client.get_runtime_config()
        readiness = self.client.get_setup_readiness()
        provider = runtime.get("provider")
        if isinstance(provider, dict):
            name = provider.get("name")
            model = provider.get("model")
            base["provider"] = name if isinstance(name, str) else None
            base["model"] = model if isinstance(model, str) else None
        mode = readiness.get("experience_mode")
        if isinstance(mode, str) and mode in {
            "demo",
            "model_not_connected",
            "connected",
        }:
            base["mode"] = mode
        next_action = readiness.get("next_action")
        if isinstance(next_action, str) and next_action:
            base["next_action"] = next_action
        return base

    def _print_status(self, view: Mapping[str, Any]) -> None:
        mode = str(view.get("mode") or "unknown")
        provider = view.get("provider")
        model = view.get("model")
        provider_text = (
            f"{provider} / {model}"
            if provider is not None and model is not None
            else "unavailable"
        )
        pid = view.get("pid")
        process_text = (
            f"verified PID {pid}" if isinstance(pid, int) else "n/a"
        )
        management = str(view.get("management"))
        managed_text = {
            ServiceManagement.MANAGED.value: "yes",
            ServiceManagement.EXTERNAL.value: "external",
            ServiceManagement.NONE.value: "n/a",
        }.get(management, "n/a")
        self.stdout.write(
            f"Service:   {view.get('service')}\n"
            f"Workbench: {view.get('url')}\n"
            f"Mode:      {_MODE_LABELS.get(mode, 'Unavailable')}\n"
            f"Provider:  {provider_text}\n"
            f"Process:   {process_text}\n"
            f"Managed:   {managed_text}\n"
            f"Next:      {view.get('next_action')}\n"
        )

    def _installation_report(self) -> dict[str, Any]:
        return {
            "home": str(self.paths.home),
            "server_executable": str(self.paths.server_executable),
            "server_executable_present": self.paths.server_executable.is_file(),
            "supervisor_script": str(self.paths.supervisor_script),
            "supervisor_script_present": self.paths.supervisor_script.is_file(),
        }

    def _path_report(self) -> dict[str, str]:
        return {
            "state": str(self.paths.state_path),
            "memory": str(self.paths.memory_dir),
            "log": str(self.paths.log_path),
        }

    def _error(self, error: object, *, recovery: str) -> None:
        self.stderr.write(f"Error: {error}\nRecovery: {recovery}\n")


def _service_payload(status: ServiceStatus) -> dict[str, Any]:
    return {
        "state": status.state.value,
        "management": status.management.value,
        "url": status.url,
        "pid": status.pid,
        "supervisor_pid": status.supervisor_pid,
        "pgid": status.pgid,
        "lifecycle_busy": status.lifecycle_busy,
        "detail": status.detail,
    }


def _service_next_action(status: ServiceStatus) -> str:
    if status.state == ServiceState.STOPPED:
        return "`kestrel open`"
    if status.state == ServiceState.STARTING:
        return "Wait briefly, then run `kestrel status`."
    if status.state == ServiceState.CONFLICT:
        return "`kestrel doctor`"
    return "`kestrel chat`"


def _view_exit_code(view: Mapping[str, Any]) -> int:
    service = view.get("service")
    if service == ServiceState.CONFLICT.value:
        return 2
    if service != ServiceState.RUNNING.value:
        return 1
    return (
        0
        if view.get("mode") in {"demo", "connected"}
        else 1
    )


def _default_offline_doctor(paths: ServicePaths) -> dict[str, Any]:
    from .cli import _doctor_runtime

    config = replace(
        AgentConfig.from_env(),
        backend="memvid",
        memory_dir=paths.memory_dir,
        state_path=paths.state_path,
        log_dir=paths.log_path.parent,
        workspace=paths.home,
    )
    return _doctor_runtime(config)


def _default_application(
    paths: ServicePaths,
    *,
    stdout: TextIO = sys.stdout,
    stderr: TextIO = sys.stderr,
    environ: Mapping[str, str] | None = None,
    client_factory: Callable[..., KestrelServerClient] = KestrelServerClient,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> LauncherApplication:
    environment = os.environ if environ is None else environ
    client = client_factory(paths.url, environ=environment)
    controller = ServiceController(paths, client=client)
    return LauncherApplication(
        paths=paths,
        controller=controller,
        client=client,
        browser_open=webbrowser.open,
        offline_doctor=_default_offline_doctor,
        input_fn=input,
        stdout=stdout,
        stderr=stderr,
        session_id_factory=lambda: f"kestrel-cli-{uuid4().hex}",
        environ=environment,
        clock=clock,
        sleep=sleep,
    )


def run(
    argv: Sequence[str] | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    cwd: Path | None = None,
    application_factory: Callable[[ServicePaths], LauncherApplication] | None = None,
    client_factory: Callable[..., KestrelServerClient] = KestrelServerClient,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    try:
        args = build_parser().parse_args(argv)
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else 2
    environment = os.environ if environ is None else environ
    try:
        home = resolve_kestrel_home(
            explicit_home=args.home,
            environ=environment,
            embedded_home=environment.get("KESTREL_LAUNCHER_HOME"),
            cwd=cwd,
        )
        paths = resolve_service_paths(
            home,
            port=args.port,
            environ=environment,
        )
    except ValueError as exc:
        sys.stderr.write(
            f"Error: {exc}\nRecovery: pass --home for a complete Kestrel installation.\n"
        )
        return 2
    application = (
        application_factory(paths)
        if application_factory is not None
        else _default_application(
            paths,
            environ=environment,
            client_factory=client_factory,
            clock=clock,
            sleep=sleep,
        )
    )
    return application.execute(args)


def main() -> NoReturn:
    raise SystemExit(run())
