from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest

import nested_memvid_agent.repair_integrity as repair_integrity_module
import nested_memvid_agent.tools.repair_tools as repair_tools_module
from nested_memvid_agent.config import AgentConfig
from nested_memvid_agent.mcp_manager import MCPLaunchIdentityError, MCPManager
from nested_memvid_agent.orchestrator import build_memory_system
from nested_memvid_agent.repair_integrity import (
    _load_or_create_receipt_key,
    load_review_receipt,
    load_validation_receipt,
    repair_snapshot,
    require_git_root,
    write_repair_artifact,
    write_validation_receipt,
)
from nested_memvid_agent.runtime_models import ToolCall, ToolExecution
from nested_memvid_agent.state_store import AgentStateStore
from nested_memvid_agent.tools.base import ToolContext
from nested_memvid_agent.tools.builtin import build_default_tools
from nested_memvid_agent.tools.registry import ToolRegistry
from nested_memvid_agent.validation_runner import (
    IsolatedValidationResult,
    ValidationIsolationError,
)
from nested_memvid_agent.worker_isolation import prepare_git_worktree


@pytest.fixture(autouse=True)
def _isolated_validation_stub(monkeypatch: pytest.MonkeyPatch) -> None:
    """Exercise repair gates without requiring Docker in the unit-test tier."""

    def run_stub(
        *,
        workspace: Path,
        image: str | None,
        command: list[str],
        timeout_seconds: float,
        expected_repair_snapshot: dict[str, object] | None = None,
    ) -> IsolatedValidationResult:
        del image
        root = require_git_root(workspace)
        before = repair_snapshot(root)
        if expected_repair_snapshot is not None:
            assert before["diff_digest"] == expected_repair_snapshot["diff_digest"]
        host_command = list(command)
        if host_command and Path(host_command[0]).name.casefold().startswith("python"):
            host_command[0] = sys.executable
        completed = subprocess.run(
            host_command,
            cwd=root,
            env={},
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
        )
        after = repair_snapshot(root)
        if after["diff_digest"] != before["diff_digest"]:
            raise ValidationIsolationError(
                "validation_candidate_changed",
                "Validation candidate changed during isolated execution: diff_digest",
            )
        return IsolatedValidationResult(
            args=tuple(command),
            returncode=completed.returncode,
            stdout=completed.stdout,
            stderr=completed.stderr,
            isolation={
                "schema_version": 1,
                "mode": "oci_snapshot_v1",
                "image": "example.invalid/kestrel-validation@sha256:" + "a" * 64,
                "network": "none",
                "workspace_mount": "private_read_only_snapshot",
                "host_fallback": False,
                "source_tree_digest": "sha256:" + "b" * 64,
                "repair_diff_digest": before["diff_digest"],
                "repair_head_sha": before["head_sha"],
                "repair_branch": before["branch"],
            },
        )

    monkeypatch.setattr(
        "nested_memvid_agent.tools.process_tools.run_isolated_validation",
        run_stub,
    )


def test_signed_repair_receipt_survives_process_restart(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    project_src = Path(__file__).resolve().parents[1] / "src"
    environment = {**os.environ, "PYTHONPATH": str(project_src)}
    artifact_id = "repair_validation_restart_survival"
    writer = "\n".join(
        (
            "from pathlib import Path",
            "from nested_memvid_agent.repair_integrity import write_repair_artifact",
            f"workspace = Path({str(repo)!r})",
            f"artifact_id = {artifact_id!r}",
            "write_repair_artifact(workspace, 'repair_validations', artifact_id, "
            "{'schema_version': 1, 'validation_id': artifact_id, 'success': True})",
        )
    )
    reader = "\n".join(
        (
            "import json",
            "from pathlib import Path",
            "from nested_memvid_agent.repair_integrity import load_repair_artifact",
            f"workspace = Path({str(repo)!r})",
            f"artifact_id = {artifact_id!r}",
            "print(json.dumps(load_repair_artifact(workspace, collection='repair_validations', "
            "artifact_id=artifact_id, expected_prefix='repair_validation_', "
            "id_field='validation_id'), sort_keys=True))",
        )
    )

    subprocess.run(
        [sys.executable, "-c", writer],
        cwd=repo,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    )
    reopened = subprocess.run(
        [sys.executable, "-c", reader],
        cwd=repo,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    )

    payload = json.loads(reopened.stdout)
    assert payload["validation_id"] == artifact_id
    assert payload["success"] is True
    assert payload["_integrity"]["schema_version"] == 2
    assert payload["_integrity"]["process_bound"] is False
    if os.name != "nt":
        assert (repo / ".nest" / "repair_receipt_signing.v2.key").stat().st_mode & 0o777 == 0o600


def test_repair_receipts_use_secure_windows_path_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        repair_integrity_module,
        "_uses_windows_path_fallback",
        lambda: True,
    )
    repo = _repo(tmp_path)
    artifact_id = "repair_validation_windows_path_fallback"

    write_repair_artifact(
        repo,
        "repair_validations",
        artifact_id,
        {
            "schema_version": 1,
            "validation_id": artifact_id,
            "success": True,
        },
    )
    reopened = repair_integrity_module.load_repair_artifact(
        repo,
        collection="repair_validations",
        artifact_id=artifact_id,
        expected_prefix="repair_validation_",
        id_field="validation_id",
    )

    assert reopened["validation_id"] == artifact_id
    assert reopened["success"] is True


def test_legacy_signed_validation_and_review_receipts_are_not_authorization(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path)
    validation_id = "repair_validation_legacy_schema"
    review_id = "repair_review_legacy_schema"
    write_repair_artifact(
        repo,
        "repair_validations",
        validation_id,
        {
            "schema_version": 1,
            "validation_id": validation_id,
            "success": True,
        },
    )
    write_repair_artifact(
        repo,
        "repair_reviews",
        review_id,
        {
            "schema_version": 1,
            "review_id": review_id,
            "validation_id": validation_id,
            "commit_gate": {"commit_allowed": True},
        },
    )

    with pytest.raises(ValueError, match="Legacy repair validation"):
        load_validation_receipt(repo, validation_id)
    with pytest.raises(ValueError, match="Legacy repair review"):
        load_review_receipt(repo, review_id)


def test_repair_snapshot_never_executes_repo_or_inherited_git_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = _repo(tmp_path)
    marker = tmp_path / "fsmonitor-executed.txt"
    leaked = tmp_path / "fsmonitor-secret.txt"
    filter_marker = tmp_path / "clean-filter-executed.txt"
    filter_leaked = tmp_path / "clean-filter-secret.txt"
    sentinel = "opaque-provider-env-sentinel-123456"
    monitor = tmp_path / "hostile-fsmonitor.py"
    monitor.write_text(
        f"#!{sys.executable}\n"
        "import os\n"
        "from pathlib import Path\n"
        f"Path({str(marker)!r}).write_text('executed')\n"
        f"Path({str(leaked)!r}).write_text(os.environ.get('GOOGLE_API_KEY', 'missing'))\n",
        encoding="utf-8",
    )
    monitor.chmod(0o700)
    clean_filter = tmp_path / "hostile-clean-filter.py"
    clean_filter.write_text(
        f"#!{sys.executable}\n"
        "import os, sys\n"
        "from pathlib import Path\n"
        "payload = sys.stdin.buffer.read()\n"
        f"Path({str(filter_marker)!r}).write_text('executed')\n"
        f"Path({str(filter_leaked)!r}).write_text(os.environ.get('GOOGLE_API_KEY', 'missing'))\n"
        "sys.stdout.buffer.write(payload)\n",
        encoding="utf-8",
    )
    clean_filter.chmod(0o700)
    (repo / ".gitattributes").write_text(
        "README.md filter=kestrel-hostile\n",
        encoding="utf-8",
    )
    subprocess.run(
        ["git", "add", ".gitattributes"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        ["git", "commit", "-m", "add filter attribute"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        ["git", "config", "core.fsmonitor", str(monitor)],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        ["git", "config", "filter.kestrel-hostile.clean", str(clean_filter)],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    monkeypatch.setenv("GOOGLE_API_KEY", sentinel)
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "core.fsmonitor")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", str(monitor))
    (repo / "README.md").write_text("candidate\n", encoding="utf-8")
    # Ignore any execution caused by the deliberately unhardened Git commands
    # used to construct this hostile fixture; only Kestrel probes are in scope.
    filter_marker.unlink(missing_ok=True)
    filter_leaked.unlink(missing_ok=True)

    snapshot = repair_snapshot(repo)
    assert filter_marker.exists() is False
    assert filter_leaked.exists() is False
    memory = build_memory_system("memory", tmp_path / "memory")
    status = build_default_tools(("git.status",)).execute(
        ToolCall(name="git.status", arguments={}),
        ToolContext(memory=memory, config=AgentConfig(), workspace=repo),
    )

    assert snapshot["changed_files"] == ["README.md"]
    assert status.success is True
    assert marker.exists() is False
    assert leaked.exists() is False
    assert filter_marker.exists() is False
    assert filter_leaked.exists() is False


def test_each_isolated_validation_rotates_prior_receipt_trust(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    (repo / "README.md").write_text("candidate\n", encoding="utf-8")
    snapshot = repair_snapshot(repo)
    first = write_validation_receipt(
        repo,
        tool_name="repair.validate",
        command=["python", "-m", "compileall", "src"],
        success=True,
        returncode=0,
        content="first",
        validation_evidence={},
        snapshot=snapshot,
        started_at="2026-07-20T00:00:00+00:00",
        isolation_attestation=_isolation_attestation(snapshot),
    )
    first_key = (repo / ".nest" / "repair_receipt_signing.v2.key").read_bytes()
    second = write_validation_receipt(
        repo,
        tool_name="repair.validate",
        command=["python", "-m", "compileall", "src"],
        success=True,
        returncode=0,
        content="second",
        validation_evidence={},
        snapshot=snapshot,
        started_at="2026-07-20T00:00:01+00:00",
        isolation_attestation=_isolation_attestation(snapshot),
    )
    second_key = (repo / ".nest" / "repair_receipt_signing.v2.key").read_bytes()

    assert first_key != second_key
    with pytest.raises(ValueError, match="not created by this Kestrel workspace"):
        load_validation_receipt(repo, str(first["validation_id"]))
    assert load_validation_receipt(repo, str(second["validation_id"]))["success"] is True


def test_repair_signing_key_concurrent_first_open_is_single_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = _repo(tmp_path)
    # LF bytes are translated to CRLF by Windows' default CRT text mode.  Use
    # them deliberately so this concurrency test also proves binary key I/O.
    candidate = b"\n" * 32
    monkeypatch.setattr(
        repair_integrity_module.secrets,
        "token_bytes",
        lambda size: candidate if size == len(candidate) else b"x" * size,
    )

    with ThreadPoolExecutor(max_workers=12) as pool:
        keys = list(pool.map(lambda _: _load_or_create_receipt_key(repo), range(24)))

    assert keys == [candidate] * 24
    key_path = repo / ".nest" / "repair_receipt_signing.v2.key"
    assert key_path.read_bytes() == candidate
    if os.name != "nt":
        assert key_path.stat().st_mode & 0o777 == 0o600


def test_changed_path_manifest_opens_literal_bytes_in_binary_mode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidate = tmp_path / "literal.bin"
    candidate.write_bytes(b"literal\r\nbytes\r\n")
    binary_flag = 1 << 29
    platform_binary_flag = getattr(os, "O_BINARY", 0)
    real_open = os.open
    observed_flags: list[int] = []

    def open_without_synthetic_flag(
        path: os.PathLike[str] | str,
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        observed_flags.append(flags)
        platform_flags = flags & ~binary_flag
        if flags & binary_flag:
            platform_flags |= platform_binary_flag
        return real_open(path, platform_flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(repair_integrity_module.os, "O_BINARY", binary_flag, raising=False)
    monkeypatch.setattr(repair_integrity_module.os, "open", open_without_synthetic_flag)

    manifest = repair_integrity_module._changed_path_manifest(  # noqa: SLF001
        tmp_path,
        candidate.name,
        reject_symlink=True,
        max_bytes=1024,
        deadline=time.monotonic() + 1,
    )

    assert observed_flags and observed_flags[0] & binary_flag
    assert manifest["size"] == len(candidate.read_bytes())


def test_repair_key_creation_closes_stdio_before_key_publication(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    key_path = repo / ".nest" / "repair_receipt_signing.v2.key"
    manager = MCPManager(AgentStateStore(tmp_path / "mcp-state.db"))
    observations: list[bool] = []

    class _Worker:
        server = SimpleNamespace(transport="stdio")

        def close(self, *, timeout: float) -> bool:
            del timeout
            observations.append(key_path.exists())
            return True

    manager._sessions["active-repair-stdio"] = _Worker()  # type: ignore[assignment]

    try:
        key = _load_or_create_receipt_key(repo)
    finally:
        manager.shutdown()

    assert len(key) == 32
    assert observations == [False]
    assert key_path.read_bytes() == key
    assert "active-repair-stdio" not in manager._sessions


def test_repair_key_creation_aborts_if_stdio_close_cannot_be_verified(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path)
    key_path = repo / ".nest" / "repair_receipt_signing.v2.key"
    manager = MCPManager(AgentStateStore(tmp_path / "mcp-state.db"))
    close_attempts = 0

    class _Worker:
        server = SimpleNamespace(transport="stdio")

        def close(self, *, timeout: float) -> bool:
            nonlocal close_attempts
            del timeout
            close_attempts += 1
            return close_attempts > 1

    manager._sessions["stuck-repair-stdio"] = _Worker()  # type: ignore[assignment]

    try:
        with pytest.raises(MCPLaunchIdentityError) as raised:
            _load_or_create_receipt_key(repo)
    finally:
        manager.shutdown()

    assert raised.value.code == "mcp_stdio_quiesce_failed"
    assert not key_path.exists()


@pytest.mark.parametrize("failure_point", ["write", "file_fsync", "publish"])
def test_repair_signing_key_fault_before_publication_cleans_temp_and_recovers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_point: str,
) -> None:
    repo = _repo(tmp_path)
    key_path = repo / ".nest" / "repair_receipt_signing.v2.key"
    temp_path = repo / ".nest" / ".repair_receipt_signing.v2.key.tmp"

    with monkeypatch.context() as fault:
        if failure_point == "write":
            def fail_partial_write(descriptor: int, candidate: bytes) -> None:
                assert os.write(descriptor, candidate[:7]) == 7
                raise OSError("injected signing-key write failure")

            fault.setattr(
                repair_integrity_module,
                "_write_receipt_key_bytes",
                fail_partial_write,
            )
        elif failure_point == "file_fsync":
            fault.setattr(
                repair_integrity_module,
                "_sync_receipt_key_file",
                lambda _descriptor: (_ for _ in ()).throw(
                    OSError("injected signing-key file fsync failure")
                ),
            )
        else:
            fault.setattr(
                repair_integrity_module,
                "_publish_receipt_key_temp",
                lambda _directory, *, expected_identity: (_ for _ in ()).throw(
                    OSError(
                        "injected signing-key publish failure "
                        f"for {expected_identity[1]}"
                    )
                ),
            )

        with pytest.raises(OSError, match="injected signing-key"):
            _load_or_create_receipt_key(repo)

    assert not key_path.exists()
    assert not temp_path.exists()
    recovered = _load_or_create_receipt_key(repo)
    assert len(recovered) == 32
    assert key_path.read_bytes() == recovered
    assert not temp_path.exists()


def test_repair_signing_key_failure_closes_handle_before_windows_style_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = _repo(tmp_path)
    key_path = repo / ".nest" / "repair_receipt_signing.v2.key"
    temp_path = repo / ".nest" / ".repair_receipt_signing.v2.key.tmp"
    failed_descriptor: int | None = None
    cleanup_observations: list[bool] = []
    original_unlink = repair_integrity_module._unlink_private_at

    def fail_partial_write(descriptor: int, candidate: bytes) -> None:
        nonlocal failed_descriptor
        failed_descriptor = descriptor
        assert os.write(descriptor, candidate[:7]) == 7
        raise OSError("injected Windows signing-key write failure")

    def deny_open_handle_unlink(
        directory: repair_integrity_module._RepairDirectoryHandle,
        name: str,
    ) -> None:
        assert failed_descriptor is not None
        try:
            os.fstat(failed_descriptor)
        except OSError:
            descriptor_closed = True
        else:
            descriptor_closed = False
        cleanup_observations.append(descriptor_closed)
        if not descriptor_closed:
            raise PermissionError("Windows denies unlink of an open file")
        original_unlink(directory, name)

    monkeypatch.setattr(
        repair_integrity_module,
        "_write_receipt_key_bytes",
        fail_partial_write,
    )
    monkeypatch.setattr(
        repair_integrity_module,
        "_unlink_private_at",
        deny_open_handle_unlink,
    )

    with pytest.raises(OSError, match="injected Windows signing-key write failure"):
        _load_or_create_receipt_key(repo)

    assert cleanup_observations == [True]
    assert not key_path.exists()
    assert not temp_path.exists()


def test_repair_signing_key_directory_fsync_failure_leaves_valid_recoverable_key(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = _repo(tmp_path)
    key_path = repo / ".nest" / "repair_receipt_signing.v2.key"
    temp_path = repo / ".nest" / ".repair_receipt_signing.v2.key.tmp"

    with monkeypatch.context() as fault:
        fault.setattr(
            repair_integrity_module,
            "_sync_receipt_key_directory",
            lambda _directory: (_ for _ in ()).throw(
                OSError("injected signing-key directory fsync failure")
            ),
        )
        with pytest.raises(OSError, match="directory fsync failure"):
            _load_or_create_receipt_key(repo)

    published = key_path.read_bytes()
    assert len(published) == 32
    assert not temp_path.exists()
    assert _load_or_create_receipt_key(repo) == published


def test_repair_signing_key_recovers_partial_orphan_temp(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path)
    nest = repo / ".nest"
    nest.mkdir(mode=0o700)
    temp_path = nest / ".repair_receipt_signing.v2.key.tmp"
    temp_path.write_bytes(b"partial")
    temp_path.chmod(0o600)

    recovered = _load_or_create_receipt_key(repo)

    assert len(recovered) == 32
    assert (nest / "repair_receipt_signing.v2.key").read_bytes() == recovered
    assert not temp_path.exists()


def test_repair_signing_key_recovers_post_link_crash_state(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path)
    nest = repo / ".nest"
    nest.mkdir(mode=0o700)
    temp_path = nest / ".repair_receipt_signing.v2.key.tmp"
    key_path = nest / "repair_receipt_signing.v2.key"
    expected = os.urandom(32)
    temp_path.write_bytes(expected)
    temp_path.chmod(0o600)
    os.link(temp_path, key_path)
    assert temp_path.stat().st_nlink == 2

    recovered = _load_or_create_receipt_key(repo)

    assert recovered == expected
    assert not temp_path.exists()
    assert key_path.stat().st_nlink == 1


def test_repair_signing_key_publish_never_overwrites_external_winner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = _repo(tmp_path)
    key_path = repo / ".nest" / "repair_receipt_signing.v2.key"
    temp_path = repo / ".nest" / ".repair_receipt_signing.v2.key.tmp"
    winner = os.urandom(32)
    original_publish = repair_integrity_module._publish_receipt_key_temp

    def publish_after_external_winner(
        directory: int,
        *,
        expected_identity: tuple[int, int],
    ) -> None:
        key_path.write_bytes(winner)
        key_path.chmod(0o600)
        original_publish(directory, expected_identity=expected_identity)

    monkeypatch.setattr(
        repair_integrity_module,
        "_publish_receipt_key_temp",
        publish_after_external_winner,
    )

    loaded = _load_or_create_receipt_key(repo)

    assert loaded == winner
    assert key_path.read_bytes() == winner
    assert not temp_path.exists()


@pytest.mark.skipif(os.name == "nt", reason="POSIX link semantics required")
@pytest.mark.parametrize("alias_kind", ["symlink", "hardlink"])
def test_repair_signing_key_rejects_link_aliases(
    tmp_path: Path,
    alias_kind: str,
) -> None:
    repo = _repo(tmp_path)
    nest = repo / ".nest"
    nest.mkdir(mode=0o700)
    outside = tmp_path / f"{alias_kind}-repair-key"
    outside.write_bytes(b"x" * 32)
    key_path = nest / "repair_receipt_signing.v2.key"
    if alias_kind == "symlink":
        key_path.symlink_to(outside)
    else:
        os.link(outside, key_path)

    with pytest.raises((OSError, ValueError), match="link|regular"):
        _load_or_create_receipt_key(repo)


def test_managed_repair_prepare_keeps_shared_worker_branch_for_next_phase(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path)
    memory = build_memory_system("memory", tmp_path / "memory")
    registry = build_default_tools()
    isolation = prepare_git_worktree(
        workspace=repo,
        worktree_root=tmp_path / "worktrees",
        branch_prefix="kestrel/worker",
        run_id="repair-run",
        worker_id="repair",
    )
    prepare_call = ToolCall(
        name="repair.prepare",
        arguments={"branch": "fix/provider-preferred-name"},
        id="adopt_managed_worktree",
    )
    prepared = registry.execute(
        prepare_call,
        _approved(memory, isolation.workspace, prepare_call),
    )
    assert prepared.success
    assert prepared.data["mode"] == "git-worktree"
    assert prepared.data["branch"] == isolation.branch
    assert prepared.data["requested_branch"] == "fix/provider-preferred-name"

    patch_call = ToolCall(
        name="repair.apply_patch",
        arguments={
            "patch": ("--- a/README.md\n+++ b/README.md\n@@ -1 +1 @@\n-seed\n+managed repair\n")
        },
        id="patch_managed_worktree",
    )
    patched = registry.execute(
        patch_call,
        _approved(memory, isolation.workspace, patch_call),
    )
    assert patched.success
    assert (isolation.workspace / "README.md").read_text(encoding="utf-8") == "managed repair\n"

    reused = prepare_git_worktree(
        workspace=repo,
        worktree_root=tmp_path / "worktrees",
        branch_prefix="kestrel/worker",
        run_id="repair-run",
        worker_id="repair",
    )
    assert reused.branch == isolation.branch
    assert (repo / "README.md").read_text(encoding="utf-8") == "seed\n"


def test_repair_review_rejects_forged_and_stale_validation_receipts(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _branch(repo, "fix/receipt-integrity")
    memory = build_memory_system("memory", tmp_path / "memory")
    registry = build_default_tools()
    (repo / "README.md").write_text("first candidate\n", encoding="utf-8")

    forged_call = ToolCall(
        name="repair.review",
        arguments={
            "validation": {"success": True},
            "summary": "Caller asserted success without running validation.",
        },
        id="forged_review",
    )
    forged = registry.execute(forged_call, _approved(memory, repo, forged_call))
    assert forged.error == "validation_receipt_required"

    validation_id = _validate(registry, memory, repo, "validation_before_drift")
    (repo / "README.md").write_text("candidate changed after validation\n", encoding="utf-8")
    stale_call = ToolCall(
        name="repair.review",
        arguments={"validation_id": validation_id, "summary": "stale"},
        id="stale_validation_review",
    )
    stale = registry.execute(stale_call, _approved(memory, repo, stale_call))
    assert stale.error == "validation_receipt_stale"
    assert "diff_digest" in stale.data["drift_fields"]


def test_repair_commit_stages_reviewed_untracked_binary_without_digest_drift(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path)
    _branch(repo, "fix/binary-candidate")
    memory = build_memory_system("memory", tmp_path / "memory")
    registry = build_default_tools()
    expected = b"\x00\x01binary\xffpayload\n"
    (repo / "asset.bin").write_bytes(expected)

    validation_id = _validate(registry, memory, repo, "binary_validation")
    review = _review(registry, memory, repo, validation_id, "binary_review")
    assert review.data["repair_snapshot"]["untracked_manifest"][0]["sha256"]
    call = ToolCall(
        name="git.commit",
        arguments={
            "message": "repair: add reviewed binary",
            "repair_review_id": review.data["review_id"],
        },
        id="binary_commit",
    )
    committed = registry.execute(call, _approved(memory, repo, call))

    assert committed.success
    shown = subprocess.run(
        ["git", "show", "HEAD:asset.bin"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    assert shown.stdout == expected


def test_repair_commit_uses_literal_bytes_without_running_clean_filters(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path)
    _branch(repo, "fix/literal-filter-boundary")
    memory = build_memory_system("memory", tmp_path / "memory")
    registry = build_default_tools()
    sentinel = tmp_path / "clean-filter-ran"
    filter_script = tmp_path / "clean-filter.sh"
    filter_script.write_text(
        f"#!/bin/sh\ntouch '{sentinel}'\ncat\n",
        encoding="utf-8",
    )
    filter_script.chmod(0o755)
    subprocess.run(
        ["git", "config", "filter.kestrel-test.clean", str(filter_script)],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    (repo / ".gitattributes").write_text(
        "filtered.txt filter=kestrel-test text eol=lf\n",
        encoding="utf-8",
    )
    expected = b"literal\r\nbytes\r\n"
    (repo / "filtered.txt").write_bytes(expected)

    validation_id = _validate(registry, memory, repo, "literal_filter_validation")
    review = _review(registry, memory, repo, validation_id, "literal_filter_review")
    call = ToolCall(
        name="git.commit",
        arguments={
            "message": "repair: preserve reviewed literal bytes",
            "repair_review_id": review.data["review_id"],
        },
        id="literal_filter_commit",
    )
    committed = registry.execute(call, _approved(memory, repo, call))

    assert committed.success
    assert not sentinel.exists()
    shown = subprocess.run(
        ["git", "show", "HEAD:filtered.txt"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    assert shown.stdout == expected


def test_signed_receipts_reject_tampering_and_redact_validation_output(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path)
    _branch(repo, "fix/signed-receipt")
    memory = build_memory_system("memory", tmp_path / "memory")
    registry = build_default_tools()
    (repo / "README.md").write_text("signed candidate\n", encoding="utf-8")
    token = "sk-proj-abcdefghijklmnopqrstuvwxyz123456"
    validation_call = ToolCall(
        name="repair.validate",
        arguments={"command": ["python", "-c", f"print('{token}')"]},
        id="signed_validation",
    )
    validation = registry.execute(
        validation_call,
        _approved(memory, repo, validation_call),
    )
    assert validation.success
    assert token not in validation.content
    validation_id = str(validation.data["validation_id"])
    receipt_path = repo / ".nest" / "repair_validations" / f"{validation_id}.json"
    receipt_text = receipt_path.read_text(encoding="utf-8")
    assert token not in receipt_text
    assert "<redacted>" in receipt_text

    payload = json.loads(receipt_text)
    payload["repair_snapshot"]["diff_digest"] = "0" * 64
    receipt_path.write_text(json.dumps(payload), encoding="utf-8")
    review_call = ToolCall(
        name="repair.review",
        arguments={"validation_id": validation_id, "summary": "tampered"},
        id="tampered_review",
    )
    review = registry.execute(review_call, _approved(memory, repo, review_call))
    assert review.error == "validation_receipt_invalid"
    assert "integrity" in review.content.lower()


def test_repair_validation_rejects_candidate_drift_and_nested_workspace(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path)
    _branch(repo, "fix/validation-drift")
    memory = build_memory_system("memory", tmp_path / "memory")
    registry = build_default_tools()
    candidate = repo / "README.md"
    candidate.write_text("before validation\n", encoding="utf-8")
    mutate_script = (
        "from pathlib import Path; "
        f"Path({str(candidate)!r}).write_text('changed during validation\\n')"
    )
    validation_call = ToolCall(
        name="repair.validate",
        arguments={"command": ["python", "-c", mutate_script]},
        id="drifting_validation",
    )
    validation = registry.execute(
        validation_call,
        _approved(memory, repo, validation_call),
    )
    assert validation.error == "validation_candidate_changed"
    assert "diff_digest" in validation.content

    nested = repo / "nested"
    nested.mkdir()
    nested_call = ToolCall(
        name="repair.validate",
        arguments={"command": ["python", "-c", "print('must not run')"]},
        id="nested_validation",
    )
    nested_result = registry.execute(
        nested_call,
        _approved(memory, nested, nested_call),
    )
    assert nested_result.error == "repair_validation_failed"
    assert "exact Git top-level" in nested_result.content


def test_timed_out_repair_validation_cannot_mutate_after_terminal_result(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path)
    _branch(repo, "fix/validation-timeout")
    memory = build_memory_system("memory", tmp_path / "memory")
    registry = build_default_tools()
    (repo / "README.md").write_text("timeout candidate\n", encoding="utf-8")
    sentinel = repo / "late-mutation.txt"
    script = (
        "import time; from pathlib import Path; time.sleep(0.5); "
        f"Path({str(sentinel)!r}).write_text('too late')"
    )
    call = ToolCall(
        name="repair.validate",
        arguments={"command": ["python", "-c", script], "timeout": 0.05},
        id="cancelled_validation",
    )

    result = registry.execute(
        call,
        _approved(memory, repo, call, tool_timeout_seconds=0.05),
    )

    assert not result.success
    time.sleep(0.6)
    assert not sentinel.exists()


def test_repair_receipts_refuse_nest_symlink_escape(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _branch(repo, "fix/nest-symlink")
    memory = build_memory_system("memory", tmp_path / "memory")
    registry = build_default_tools()
    (repo / "README.md").write_text("candidate\n", encoding="utf-8")
    outside = tmp_path / "outside"
    outside.mkdir()
    (repo / ".nest").symlink_to(outside, target_is_directory=True)
    validation_call = ToolCall(
        name="repair.validate",
        arguments={"command": ["python", "-c", "print('validated')"]},
        id="nest_symlink_validation",
    )
    validation = registry.execute(
        validation_call,
        _approved(memory, repo, validation_call),
    )
    assert validation.error == "repair_validation_failed"
    assert list(outside.iterdir()) == []


def test_tracked_nest_source_file_is_not_hidden_from_repair_review(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    subprocess.run(
        ["git", "check-ignore", "-q", ".nest/config.toml"],
        cwd=repo,
        check=False,
        capture_output=True,
    )
    (repo / ".gitignore").write_text(".nest/repair_*\n", encoding="utf-8")
    nest = repo / ".nest"
    nest.mkdir()
    source = nest / "config.toml"
    source.write_text("value = 1\n", encoding="utf-8")
    subprocess.run(
        ["git", "add", ".gitignore", ".nest/config.toml"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        ["git", "commit", "-m", "track nest source"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    _branch(repo, "fix/tracked-nest-source")
    source.write_text("value = 2\n", encoding="utf-8")

    snapshot = repair_snapshot(repo)

    assert ".nest/config.toml" in snapshot["tracked_files"]
    assert ".nest/config.toml" in snapshot["changed_files"]


def test_repair_commit_rechecks_head_and_disables_repository_hooks(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _branch(repo, "fix/hook-boundary")
    memory = build_memory_system("memory", tmp_path / "memory")
    registry = build_default_tools()
    (repo / "README.md").write_text("reviewed candidate\n", encoding="utf-8")
    validation_id = _validate(registry, memory, repo, "hook_validation")
    review = _review(registry, memory, repo, validation_id, "hook_review")

    sentinel = repo / "hook-ran"
    hook = repo / ".git" / "hooks" / "pre-commit"
    hook.write_text(f"#!/bin/sh\ntouch '{sentinel}'\nexit 1\n", encoding="utf-8")
    hook.chmod(0o755)
    call = ToolCall(
        name="git.commit",
        arguments={
            "message": "repair: bypass unapproved repository hooks",
            "repair_review_id": review.data["review_id"],
        },
        id="hook_commit",
    )
    committed = registry.execute(call, _approved(memory, repo, call))
    assert committed.success
    assert not sentinel.exists()

    (repo / "README.md").write_text("second candidate\n", encoding="utf-8")
    validation_id = _validate(registry, memory, repo, "head_validation")
    review = _review(registry, memory, repo, validation_id, "head_review")
    subprocess.run(
        ["git", "commit", "--allow-empty", "--no-verify", "-m", "advance head"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    stale_call = ToolCall(
        name="git.commit",
        arguments={
            "message": "must not commit stale review",
            "repair_review_id": review.data["review_id"],
        },
        id="stale_head_commit",
    )
    stale = registry.execute(stale_call, _approved(memory, repo, stale_call))
    assert stale.error == "repair_review_stale"
    assert stale.data["expected_head_sha"] != stale.data["actual_head_sha"]


def test_receipt_scoped_rollback_resets_staged_changes_and_preserves_unrelated_files(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path)
    _branch(repo, "repair/scoped-rollback")
    memory = build_memory_system("memory", tmp_path / "memory")
    registry = build_default_tools()
    (repo / "README.md").write_text("reviewed tracked change\n", encoding="utf-8")
    (repo / "reviewed-new.txt").write_text("reviewed new file\n", encoding="utf-8")
    subprocess.run(
        ["git", "add", "README.md"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    validation_id = _validate(registry, memory, repo, "rollback_validation")
    (repo / "unrelated-user-note.txt").write_text("preserve me\n", encoding="utf-8")
    call = ToolCall(
        name="repair.rollback",
        arguments={
            "validation_id": validation_id,
            "reason": "test scoped rollback",
            "expected_current_diff_digest": repair_snapshot(repo)["diff_digest"],
        },
        id="scoped_rollback",
    )
    result = registry.execute(call, _approved(memory, repo, call))

    assert result.success
    assert (repo / "README.md").read_text(encoding="utf-8") == "seed\n"
    assert not (repo / "reviewed-new.txt").exists()
    assert (repo / "unrelated-user-note.txt").read_text(encoding="utf-8") == "preserve me\n"
    assert result.data["after"]["preserved_changed_files"] == ["unrelated-user-note.txt"]
    assert result.data["recoverable"] is True
    quarantine = repo / result.data["quarantine_path"]
    reviewed_entry = result.data["quarantine_manifest"]["reviewed-new.txt"]
    assert (quarantine / reviewed_entry["stored_name"]).read_text(encoding="utf-8") == (
        "reviewed new file\n"
    )
    artifact = repo / result.data["rollback_artifact"]
    assert json.loads(artifact.read_text(encoding="utf-8"))["success"] is True
    if os.name != "nt":
        assert artifact.stat().st_mode & 0o777 == 0o600


def test_receipt_scoped_rollback_windows_path_fallback_preserves_recovery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = _repo(tmp_path)
    _branch(repo, "repair/windows-scoped-rollback")
    memory = build_memory_system("memory", tmp_path / "memory")
    registry = build_default_tools()
    (repo / "README.md").write_text("reviewed tracked change\n", encoding="utf-8")
    (repo / "reviewed-new.txt").write_text("reviewed new file\n", encoding="utf-8")
    validation_id = _validate(registry, memory, repo, "windows_rollback_validation")
    (repo / "unrelated-user-note.txt").write_text("preserve me\n", encoding="utf-8")
    monkeypatch.setattr(
        repair_tools_module,
        "_uses_windows_rollback_path_fallback",
        lambda: True,
    )
    call = ToolCall(
        name="repair.rollback",
        arguments={
            "validation_id": validation_id,
            "reason": "exercise Windows path fallback",
            "expected_current_diff_digest": repair_snapshot(repo)["diff_digest"],
        },
        id="windows_scoped_rollback",
    )

    result = registry.execute(call, _approved(memory, repo, call))

    assert result.success, result
    assert (repo / "README.md").read_text(encoding="utf-8") == "seed\n"
    assert not (repo / "reviewed-new.txt").exists()
    assert (repo / "unrelated-user-note.txt").read_text(encoding="utf-8") == "preserve me\n"
    quarantine = repo / result.data["quarantine_path"]
    entry = result.data["quarantine_manifest"]["reviewed-new.txt"]
    assert (quarantine / entry["stored_name"]).read_text(encoding="utf-8") == (
        "reviewed new file\n"
    )
    assert result.data["recoverable"] is True


def test_rollback_rejects_state_changed_after_exact_approval(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _branch(repo, "repair/rollback-digest")
    memory = build_memory_system("memory", tmp_path / "memory")
    registry = build_default_tools()
    candidate = repo / "README.md"
    candidate.write_text("approved rollback candidate\n", encoding="utf-8")
    validation_id = _validate(registry, memory, repo, "rollback_digest_validation")
    approved_digest = repair_snapshot(repo)["diff_digest"]
    candidate.write_text("changed after approval\n", encoding="utf-8")
    call = ToolCall(
        name="repair.rollback",
        arguments={
            "validation_id": validation_id,
            "expected_current_diff_digest": approved_digest,
        },
        id="stale_rollback_digest",
    )

    result = registry.execute(call, _approved(memory, repo, call))

    assert result.error == "rollback_snapshot_stale"
    assert candidate.read_text(encoding="utf-8") == "changed after approval\n"


def test_git_commit_fails_closed_for_detached_head_and_nonlocal_write_mode(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path)
    memory = build_memory_system("memory", tmp_path / "memory")
    registry = build_default_tools()
    (repo / "README.md").write_text("staged\n", encoding="utf-8")
    subprocess.run(
        ["git", "add", "README.md"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        ["git", "checkout", "--detach"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    detached_call = ToolCall(
        name="git.commit",
        arguments={"message": "detached commit"},
        id="detached_commit",
    )
    detached = registry.execute(detached_call, _approved(memory, repo, detached_call))
    assert detached.error == "detached_head"

    subprocess.run(
        ["git", "switch", "-c", "topic/local-mode"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    mode_call = ToolCall(
        name="git.commit",
        arguments={"message": "wrong mode"},
        id="wrong_mode_commit",
    )
    mode_context = _approved(memory, repo, mode_call, git_write_mode="fork_pr")
    mode_blocked = registry.execute(mode_call, mode_context)
    assert mode_blocked.error == "git_write_mode_blocked"


def test_nonrepair_commit_binds_approval_to_exact_staged_tree(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _branch(repo, "topic/exact-staged-tree")
    memory = build_memory_system("memory", tmp_path / "memory")
    registry = build_default_tools()
    (repo / "README.md").write_text("approved staged change\n", encoding="utf-8")
    subprocess.run(
        ["git", "add", "README.md"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    approved_tree = subprocess.run(
        ["git", "write-tree"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    approved_preview = _commit_preview(registry, memory, repo)
    assert approved_preview["expected_tree_sha"] == approved_tree
    call = ToolCall(
        name="git.commit",
        arguments={"message": "exact staged commit", **approved_preview},
        id="exact_staged_commit",
    )
    (repo / "extra.txt").write_text("not approved\n", encoding="utf-8")
    subprocess.run(
        ["git", "add", "extra.txt"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )

    stale = registry.execute(call, _approved(memory, repo, call))

    assert stale.error == "commit_preview_stale"
    assert subprocess.run(
        ["git", "rev-list", "--count", "HEAD"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip() == "1"


def test_nonrepair_commit_rejects_branch_drift_with_same_staged_tree(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path)
    _branch(repo, "topic/approved-destination")
    memory = build_memory_system("memory", tmp_path / "memory")
    registry = build_default_tools()
    (repo / "README.md").write_text("approved staged change\n", encoding="utf-8")
    subprocess.run(
        ["git", "add", "README.md"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    approved_preview = _commit_preview(registry, memory, repo)
    call = ToolCall(
        name="git.commit",
        arguments={"message": "must stay on approved branch", **approved_preview},
        id="branch_drift_commit",
    )

    _branch(repo, "topic/redirected-destination")
    drifted_preview = _commit_preview(registry, memory, repo)
    assert drifted_preview["expected_tree_sha"] == approved_preview["expected_tree_sha"]
    assert drifted_preview["expected_head_sha"] == approved_preview["expected_head_sha"]

    stale = registry.execute(call, _approved(memory, repo, call))

    assert stale.error == "commit_preview_stale"
    assert stale.data["drift_fields"] == ["branch"]
    assert stale.data["expected_branch"] == "topic/approved-destination"
    assert stale.data["actual_branch"] == "topic/redirected-destination"
    assert _commit_preview(registry, memory, repo) == drifted_preview


def test_nonrepair_commit_rejects_head_drift_with_same_staged_tree(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path)
    branch = "topic/approved-head"
    _branch(repo, branch)
    memory = build_memory_system("memory", tmp_path / "memory")
    registry = build_default_tools()
    (repo / "README.md").write_text("approved staged change\n", encoding="utf-8")
    subprocess.run(
        ["git", "add", "README.md"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    approved_preview = _commit_preview(registry, memory, repo)
    call = ToolCall(
        name="git.commit",
        arguments={"message": "must retain approved parent", **approved_preview},
        id="head_drift_commit",
    )
    approved_head = approved_preview["expected_head_sha"]
    approved_head_tree = subprocess.run(
        ["git", "rev-parse", f"{approved_head}^{{tree}}"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    drifted_head = subprocess.run(
        ["git", "commit-tree", approved_head_tree, "-p", approved_head, "-m", "concurrent"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    subprocess.run(
        ["git", "update-ref", f"refs/heads/{branch}", drifted_head, approved_head],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    drifted_preview = _commit_preview(registry, memory, repo)
    assert drifted_preview["expected_tree_sha"] == approved_preview["expected_tree_sha"]
    assert drifted_preview["expected_branch"] == approved_preview["expected_branch"]

    stale = registry.execute(call, _approved(memory, repo, call))

    assert stale.error == "commit_preview_stale"
    assert stale.data["drift_fields"] == ["head_sha"]
    assert stale.data["expected_head_sha"] == approved_head
    assert stale.data["actual_head_sha"] == drifted_head
    assert _commit_preview(registry, memory, repo) == drifted_preview


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True, text=True)
    subprocess.run(
        ["git", "config", "user.email", "kestrel@example.test"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Kestrel Test"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        ["git", "config", "core.autocrlf", "false"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    (repo / ".gitignore").write_bytes(b".nest/\n")
    (repo / "README.md").write_bytes(b"seed\n")
    subprocess.run(
        ["git", "add", ".gitignore", "README.md"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        ["git", "commit", "-m", "seed"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    return repo


def _isolation_attestation(snapshot: dict[str, object]) -> dict[str, object]:
    return {
        "schema_version": 1,
        "mode": "oci_snapshot_v1",
        "image": "example.invalid/kestrel-validation@sha256:" + "a" * 64,
        "network": "none",
        "workspace_mount": "private_read_only_snapshot",
        "host_fallback": False,
        "source_tree_digest": "sha256:" + "b" * 64,
        "repair_diff_digest": snapshot["diff_digest"],
        "repair_head_sha": snapshot["head_sha"],
        "repair_branch": snapshot["branch"],
    }


def _branch(repo: Path, name: str) -> None:
    subprocess.run(
        ["git", "switch", "-c", name],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )


def _commit_preview(
    registry: ToolRegistry,
    memory: object,
    repo: Path,
) -> dict[str, str]:
    result = registry.execute(
        ToolCall(name="git.status", arguments={}),
        ToolContext(
            memory=memory,  # type: ignore[arg-type]
            config=AgentConfig(),
            workspace=repo,
        ),
    )
    assert result.success
    preview = result.data.get("commit_preview")
    assert isinstance(preview, dict)
    assert all(isinstance(preview.get(key), str) and preview[key] for key in (
        "expected_branch",
        "expected_head_sha",
        "expected_tree_sha",
    ))
    return {key: str(value) for key, value in preview.items()}


def _approved(
    memory: object,
    repo: Path,
    call: ToolCall,
    *,
    git_write_mode: str = "local_branch",
    tool_timeout_seconds: float = 30.0,
) -> ToolContext:
    return ToolContext(
        memory=memory,  # type: ignore[arg-type]
        config=AgentConfig(
            allow_file_write=True,
            allow_shell=True,
            allow_git_commit=True,
            git_write_mode=git_write_mode,
            tool_timeout_seconds=tool_timeout_seconds,
        ),
        workspace=repo,
        approved_tool_call_ids=frozenset({call.id}),
        approved_tool_call_arguments={call.id: call.arguments},
    )


def _validate(
    registry: ToolRegistry,
    memory: object,
    repo: Path,
    call_id: str,
) -> str:
    call = ToolCall(
        name="repair.validate",
        arguments={"command": ["python", "-c", "print('validated')"]},
        id=call_id,
    )
    result = registry.execute(call, _approved(memory, repo, call))
    assert result.success
    return str(result.data["validation_id"])


def _review(
    registry: ToolRegistry,
    memory: object,
    repo: Path,
    validation_id: str,
    call_id: str,
) -> ToolExecution:
    call = ToolCall(
        name="repair.review",
        arguments={"validation_id": validation_id, "summary": "validated repair candidate"},
        id=call_id,
    )
    result = registry.execute(call, _approved(memory, repo, call))
    assert result.success
    return result
