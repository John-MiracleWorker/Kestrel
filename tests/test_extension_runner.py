from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pytest

import nested_memvid_agent.extension_runner as extension_runner
from nested_memvid_agent.extension_policy import (
    ExtensionPolicyError,
    ResolvedFilesystemScope,
    extension_tree_digest,
    parse_extension_scopes,
)
from nested_memvid_agent.extension_runner import (
    OCI_STDIN_LIMIT_BYTES,
    ContainerExecutionRequest,
    OCIContainerCleanupQuarantine,
    OCIContainerRunner,
)

PINNED_IMAGE = "example.invalid/kestrel-skill@sha256:" + "a" * 64


def test_container_runner_uses_fixed_default_deny_argv_and_bounded_payload(tmp_path: Path) -> None:
    engine, log_path = _fake_engine(tmp_path)
    source = _skill_tree(tmp_path)
    workspace = tmp_path / "workspace"
    inputs = workspace / "inputs"
    inputs.mkdir(parents=True)
    runner = OCIContainerRunner(engine_command=engine)

    result = runner.run(
        _request(
            source,
            workspace,
            command=("ok",),
            scopes={"filesystem": [{"root": "workspace", "path": "inputs", "access": "read"}]},
        )
    )

    assert result.success is True
    assert '"task":"bounded"' in result.stdout
    calls = _engine_calls(log_path)
    argv = calls[0]
    assert argv[0] == "run"
    for option in (
        "--network=none",
        "--read-only",
        "--cap-drop=ALL",
        "--security-opt=no-new-privileges:true",
        "--pids-limit=64",
        "--memory=256m",
        "--cpus=1.0",
        "--ipc=none",
        "--log-driver=none",
        "--pull=never",
        "--env=HOME=/tmp",
    ):
        assert option in argv
    assert not any(item.startswith("--ulimit=nproc=") for item in argv)
    assert any(item.endswith("target=/extension,readonly") for item in argv)
    assert any(item.endswith("target=/workspace/inputs,readonly") for item in argv)
    scope_mount = next(item for item in argv if "target=/workspace/inputs" in item)
    scope_source = Path(_mount_options(scope_mount)["source"])
    assert workspace not in scope_source.parents
    assert source not in scope_source.parents
    user_option = next(item for item in argv if item.startswith("--user="))
    assert not user_option.startswith("--user=0:")
    assert PINNED_IMAGE in argv
    assert argv[argv.index(PINNED_IMAGE) - 1] == "--"


def test_container_runner_fails_closed_for_unpinned_image_or_missing_engine(tmp_path: Path) -> None:
    engine, log_path = _fake_engine(tmp_path)
    source = _skill_tree(tmp_path)
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    unpinned = OCIContainerRunner(engine_command=engine).run(
        _request(source, workspace, image="python:3.11")
    )
    option_shaped = OCIContainerRunner(engine_command=engine).run(
        _request(source, workspace, image="--env=ESCAPE@sha256:" + "a" * 64)
    )
    missing = OCIContainerRunner(engine_command=(str(tmp_path / "missing-engine"),)).run(
        _request(source, workspace)
    )

    assert unpinned.error == "container_image_not_digest_pinned"
    assert option_shaped.error == "container_image_not_digest_pinned"
    assert missing.error == "container_runtime_unavailable"
    assert not log_path.exists()


def test_container_runner_rejects_changed_tree_before_engine_launch(tmp_path: Path) -> None:
    engine, log_path = _fake_engine(tmp_path)
    source = _skill_tree(tmp_path)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    expected = extension_tree_digest(source)
    (source / "skill.py").write_text("print('changed')\n", encoding="utf-8")
    request = _request(source, workspace, expected_tree_digest=expected)

    result = OCIContainerRunner(engine_command=engine).run(request)

    assert result.error == "extension_resource_changed"
    assert not log_path.exists()


def test_container_runner_snapshots_outside_source_and_workspace(tmp_path: Path) -> None:
    engine, log_path = _fake_engine(tmp_path)
    comma_parent = tmp_path / "extension,parent"
    comma_parent.mkdir()
    source = _skill_tree(comma_parent)
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    result = OCIContainerRunner(engine_command=engine).run(_request(source, workspace))

    assert result.success is True
    run_call = _engine_calls(log_path)[0]
    extension_mount = next(item for item in run_call if "target=/extension" in item)
    snapshot = Path(_mount_options(extension_mount)["source"])
    assert source not in snapshot.parents
    assert workspace not in snapshot.parents
    assert not snapshot.exists()


def test_container_runner_caps_output_and_removes_timed_out_container(tmp_path: Path) -> None:
    engine, log_path = _fake_engine(tmp_path)
    source = _skill_tree(tmp_path)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    runner = OCIContainerRunner(engine_command=engine, output_limit_bytes=1024)

    overflow = runner.run(_request(source, workspace, command=("overflow",)))
    timed_out = runner.run(
        _request(source, workspace, command=("sleep",), timeout_seconds=0.1)
    )

    assert overflow.error == "extension_output_limit_exceeded"
    assert len(overflow.stdout.encode("utf-8")) <= 1024
    assert timed_out.error == "extension_timeout"
    calls = _engine_calls(log_path)
    assert any(call and call[:2] == ["rm", "-f"] for call in calls)
    assert any(call and call[0] == "ps" for call in calls)


def test_container_runner_caps_utf8_stdin_before_launch(tmp_path: Path) -> None:
    engine, log_path = _fake_engine(tmp_path)
    source = _skill_tree(tmp_path)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    runner = OCIContainerRunner(engine_command=engine)

    at_limit = runner.run(
        _request(
            source,
            workspace,
            command=("discard",),
            stdin="x" * OCI_STDIN_LIMIT_BYTES,
        )
    )
    over_limit = runner.run(
        _request(source, workspace, stdin="é" * (OCI_STDIN_LIMIT_BYTES // 2 + 1))
    )
    invalid = runner.run(_request(source, workspace, stdin="\ud800"))

    assert at_limit.success is True
    assert over_limit.error == "extension_stdin_limit_exceeded"
    assert invalid.error == "extension_stdin_invalid"
    assert sum(call[0] == "run" for call in _engine_calls(log_path)) == 1


def test_container_runner_rejects_remote_or_ambiguous_docker_configuration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine, log_path = _fake_engine(tmp_path)
    source = _skill_tree(tmp_path)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setattr(extension_runner.shutil, "which", lambda _name: engine[0])
    monkeypatch.setenv("DOCKER_HOST", "tcp://remote.invalid:2375")

    remote = OCIContainerRunner(engine_command=("docker",)).run(
        _request(source, workspace)
    )
    monkeypatch.delenv("DOCKER_HOST")
    ambiguous = OCIContainerRunner(
        engine_command=("docker", "--context", "possibly-remote")
    ).run(_request(source, workspace))

    assert remote.error == "container_runtime_nonlocal"
    assert ambiguous.error == "container_runtime_nonlocal"
    assert not log_path.exists()


def test_container_runner_fails_closed_when_docker_endpoint_is_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine, log_path = _fake_engine(tmp_path)
    source = _skill_tree(tmp_path)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setattr(extension_runner.shutil, "which", lambda _name: engine[0])
    monkeypatch.setattr(
        extension_runner,
        "_verified_docker_endpoint",
        lambda _engine, _environment: (None, None),
    )

    result = OCIContainerRunner(engine_command=("docker",)).run(
        _request(source, workspace)
    )

    assert result.error == "container_runtime_nonlocal"
    assert "could not be verified as local" in result.content
    assert not log_path.exists()


def test_container_argv_rejects_unexpected_internal_write_mount(tmp_path: Path) -> None:
    source = _skill_tree(tmp_path)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    mount_source = tmp_path / "private-snapshot"
    mount_source.mkdir()
    request = _request(source, workspace)
    mount = ResolvedFilesystemScope(
        source=mount_source,
        target="/workspace/inputs",
        access="write",
        declared_path="inputs",
        source_stat=mount_source.lstat(),
    )

    with pytest.raises(ExtensionPolicyError, match="extension_write_scope_unsupported"):
        extension_runner._container_argv(  # noqa: SLF001 - defense-in-depth contract
            ("engine",),
            request=request,
            snapshot=source,
            mounts=(mount,),
            container_name="kestrel-skill-test",
        )


def test_container_runner_quarantines_unverified_cleanup_until_retry_succeeds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine, log_path = _fake_engine(tmp_path, cleanup_visible=True)
    source = _skill_tree(tmp_path)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setattr(extension_runner, "OCI_CLEANUP_TIMEOUT_SECONDS", 0.1)
    quarantine = OCIContainerCleanupQuarantine()
    runner = OCIContainerRunner(
        engine_command=engine,
        cleanup_quarantine=quarantine,
    )

    result = runner.run(_request(source, workspace))

    assert result.error == "extension_cleanup_unverified"
    assert runner.pending_cleanup_count == 1
    pending = runner.pending_cleanups()
    first_run = next(call for call in _engine_calls(log_path) if call[0] == "run")
    assert pending == (
        {
            "engine": list(engine),
            "container_name": first_run[first_run.index("--name") + 1],
        },
    )

    run_count = sum(call[0] == "run" for call in _engine_calls(log_path))
    blocked = OCIContainerRunner(
        engine_command=engine,
        cleanup_quarantine=quarantine,
    ).run(_request(source, workspace))
    assert blocked.error == "extension_cleanup_pending"
    assert sum(call[0] == "run" for call in _engine_calls(log_path)) == run_count

    _fake_engine(tmp_path, cleanup_visible=False)
    monkeypatch.setattr(extension_runner, "OCI_ABSENCE_CONFIRMATIONS", 1)
    assert runner.retry_cleanup(timeout_seconds=1.0) is True
    assert runner.pending_cleanup_count == 0

    resumed = runner.run(_request(source, workspace))
    assert resumed.success is True


def test_default_runners_share_process_cleanup_ownership(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    quarantine = OCIContainerCleanupQuarantine()
    monkeypatch.setattr(
        extension_runner,
        "_PROCESS_OCI_CLEANUP_QUARANTINE",
        quarantine,
    )
    engine, log_path = _fake_engine(tmp_path)
    first = OCIContainerRunner(engine_command=engine)
    second = OCIContainerRunner(engine_command=engine)
    quarantine.retain(
        engine=engine,
        container_name="kestrel-skill-process-owned-deadbeef",
        environment={"PATH": "/trusted/bin"},
    )

    source = _skill_tree(tmp_path)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    result = second.run(_request(source, workspace))

    assert first.cleanup_quarantine is quarantine
    assert second.cleanup_quarantine is quarantine
    assert result.error == "extension_cleanup_pending"
    assert not log_path.exists()


def test_container_cleanup_retry_is_bounded_and_retains_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    quarantine = OCIContainerCleanupQuarantine()
    quarantine.retain(
        engine=("/trusted/engine", "--host", "unix:///trusted.sock"),
        container_name="kestrel-skill-exact-deadbeef",
        environment={"PATH": "/trusted/bin"},
    )
    attempted: list[tuple[tuple[str, ...], str, dict[str, str], float]] = []

    def fail_cleanup(
        engine: tuple[str, ...],
        name: str,
        environment: dict[str, str],
        *,
        timeout_seconds: float,
    ) -> bool:
        attempted.append((engine, name, environment, timeout_seconds))
        raise OSError("injected engine failure")

    monkeypatch.setattr(
        extension_runner,
        "_remove_and_confirm_container_absent",
        fail_cleanup,
    )

    assert quarantine.retry_cleanup(timeout_seconds=float("inf")) is False
    assert attempted == []
    assert quarantine.retry_cleanup(timeout_seconds=60.0) is False
    assert attempted == [
        (
            ("/trusted/engine", "--host", "unix:///trusted.sock"),
            "kestrel-skill-exact-deadbeef",
            {"PATH": "/trusted/bin"},
            pytest.approx(extension_runner.OCI_CLEANUP_RETRY_MAX_SECONDS),
        )
    ]
    assert quarantine.status() == (
        {
            "engine": ["/trusted/engine", "--host", "unix:///trusted.sock"],
            "container_name": "kestrel-skill-exact-deadbeef",
        },
    )


def test_container_timeout_kills_late_mutation_before_return(tmp_path: Path) -> None:
    engine, _ = _fake_engine(tmp_path)
    source = _skill_tree(tmp_path)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    sentinel = tmp_path / "late-write.txt"

    result = OCIContainerRunner(engine_command=engine).run(
        _request(
            source,
            workspace,
            command=("delayed-write", str(sentinel)),
            timeout_seconds=0.1,
        )
    )
    time.sleep(1.1)

    assert result.error == "extension_timeout"
    assert not sentinel.exists()


def _request(
    source: Path,
    workspace: Path,
    *,
    image: str = PINNED_IMAGE,
    command: tuple[str, ...] = ("ok",),
    scopes: dict[str, object] | None = None,
    timeout_seconds: float = 2.0,
    expected_tree_digest: str | None = None,
    stdin: str = '{"task":"bounded"}',
) -> ContainerExecutionRequest:
    return ContainerExecutionRequest(
        extension_id="test-skill",
        source_dir=source,
        expected_tree_digest=expected_tree_digest or extension_tree_digest(source),
        workspace=workspace,
        scopes=parse_extension_scopes(scopes or {}),
        image=image,
        command=command,
        stdin=stdin,
        timeout_seconds=timeout_seconds,
    )


def _skill_tree(tmp_path: Path) -> Path:
    source = tmp_path / "skill"
    source.mkdir()
    (source / "skill.py").write_text("print('skill')\n", encoding="utf-8")
    (source / "SKILL.md").write_text("Run in a container.\n", encoding="utf-8")
    return source


def _fake_engine(
    tmp_path: Path, *, cleanup_visible: bool = False
) -> tuple[tuple[str, ...], Path]:
    script = tmp_path / "fake_engine.py"
    log_path = tmp_path / "engine_calls.jsonl"
    script.write_text(
        "\n".join(
            [
                "import json, signal, sys, time",
                "from pathlib import Path",
                f"log = Path({str(log_path)!r})",
                "with log.open('a', encoding='utf-8') as handle:",
                "    handle.write(json.dumps(sys.argv[1:]) + '\\n')",
                "if sys.argv[1] == 'rm':",
                "    raise SystemExit(0)",
                "if sys.argv[1] == 'ps':",
                f"    sys.stdout.write(sys.argv[-2] if {cleanup_visible!r} else '')",
                "    raise SystemExit(0)",
                "if 'delayed-write' in sys.argv:",
                "    signal.signal(signal.SIGTERM, signal.SIG_IGN)",
                "    time.sleep(1)",
                "    Path(sys.argv[-1]).write_text('late', encoding='utf-8')",
                "    raise SystemExit(0)",
                "mode = sys.argv[-1]",
                "if mode == 'sleep':",
                "    time.sleep(5)",
                "elif mode == 'overflow':",
                "    sys.stdout.write('x' * 4096)",
                "elif mode == 'discard':",
                "    sys.stdin.read()",
                "else:",
                "    sys.stdout.write(sys.stdin.read())",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    return (sys.executable, str(script)), log_path


def _engine_calls(path: Path) -> list[list[str]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def _mount_options(value: str) -> dict[str, str]:
    return dict(part.split("=", 1) for part in value.split(",") if "=" in part)
