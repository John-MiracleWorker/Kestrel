from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

import nested_memvid_agent.repair_integrity as repair_integrity_module
from nested_memvid_agent.config import AgentConfig
from nested_memvid_agent.orchestrator import build_memory_system
from nested_memvid_agent.repair_integrity import (
    _load_or_create_receipt_key,
    repair_snapshot,
)
from nested_memvid_agent.runtime_models import ToolCall, ToolExecution
from nested_memvid_agent.tools.base import ToolContext
from nested_memvid_agent.tools.builtin import build_default_tools
from nested_memvid_agent.tools.registry import ToolRegistry
from nested_memvid_agent.worker_isolation import prepare_git_worktree


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
            "from nested_memvid_agent.repair_integrity import load_validation_receipt",
            f"workspace = Path({str(repo)!r})",
            f"artifact_id = {artifact_id!r}",
            "print(json.dumps(load_validation_receipt(workspace, artifact_id), sort_keys=True))",
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
    assert payload["_integrity"]["process_bound"] is False
    if os.name != "nt":
        assert (repo / ".nest" / "repair_receipt_signing.key").stat().st_mode & 0o777 == 0o600


def test_repair_signing_key_concurrent_first_open_is_single_identity(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path)

    with ThreadPoolExecutor(max_workers=12) as pool:
        keys = list(pool.map(lambda _: _load_or_create_receipt_key(repo), range(24)))

    assert len(set(keys)) == 1
    assert len(keys[0]) == 32
    key_path = repo / ".nest" / "repair_receipt_signing.key"
    assert key_path.read_bytes() == keys[0]
    if os.name != "nt":
        assert key_path.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("failure_point", ["write", "file_fsync", "publish"])
def test_repair_signing_key_fault_before_publication_cleans_temp_and_recovers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_point: str,
) -> None:
    repo = _repo(tmp_path)
    key_path = repo / ".nest" / "repair_receipt_signing.key"
    temp_path = repo / ".nest" / ".repair_receipt_signing.key.tmp"

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


def test_repair_signing_key_directory_fsync_failure_leaves_valid_recoverable_key(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = _repo(tmp_path)
    key_path = repo / ".nest" / "repair_receipt_signing.key"
    temp_path = repo / ".nest" / ".repair_receipt_signing.key.tmp"

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
    temp_path = nest / ".repair_receipt_signing.key.tmp"
    temp_path.write_bytes(b"partial")
    temp_path.chmod(0o600)

    recovered = _load_or_create_receipt_key(repo)

    assert len(recovered) == 32
    assert (nest / "repair_receipt_signing.key").read_bytes() == recovered
    assert not temp_path.exists()


def test_repair_signing_key_recovers_post_link_crash_state(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path)
    nest = repo / ".nest"
    nest.mkdir(mode=0o700)
    temp_path = nest / ".repair_receipt_signing.key.tmp"
    key_path = nest / "repair_receipt_signing.key"
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
    key_path = repo / ".nest" / "repair_receipt_signing.key"
    temp_path = repo / ".nest" / ".repair_receipt_signing.key.tmp"
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
    key_path = nest / "repair_receipt_signing.key"
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
    assert validation.error == "repair_validation_failed"
    assert validation.data["validation_drift_fields"] == ["diff_digest"]

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
    call = ToolCall(
        name="git.commit",
        arguments={"message": "exact staged commit", "expected_tree_sha": approved_tree},
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
    (repo / ".gitignore").write_text(".nest/\n", encoding="utf-8")
    (repo / "README.md").write_text("seed\n", encoding="utf-8")
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


def _branch(repo: Path, name: str) -> None:
    subprocess.run(
        ["git", "switch", "-c", name],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )


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
