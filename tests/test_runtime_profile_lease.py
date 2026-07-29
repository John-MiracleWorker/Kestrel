from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from nested_memvid_agent.file_lock import lock_exclusive, unlock
from nested_memvid_agent.private_artifacts import (
    open_private_file_descriptor,
    write_private_text,
)
from nested_memvid_agent.runtime_profile_lease import (
    LeaseProcessSnapshot,
    RuntimeLeaseConflict,
    RuntimeLeaseIdentity,
    RuntimeProfileLease,
    runtime_profile_lock_path,
    runtime_profile_metadata_path,
)


def _identity(
    management: str,
    *,
    profile_id: str = "default",
    version: str = "0.5.0",
    pid: int = 4242,
) -> RuntimeLeaseIdentity:
    return RuntimeLeaseIdentity(
        profile_id=profile_id,
        management=management,
        owner_digest="1" * 64,
        pid=pid,
        process_birth_marker="process-birth-4242",
        executable_digest="2" * 64,
        launch_nonce_digest="3" * 64,
        base_url="http://127.0.0.1:8765/",
        version=version,
        created_at="2026-07-29T12:00:00+00:00",
    )


def _matching_process(identity: RuntimeLeaseIdentity) -> LeaseProcessSnapshot:
    return LeaseProcessSnapshot(
        pid=identity.pid,
        owner_digest=identity.owner_digest,
        process_birth_marker=identity.process_birth_marker,
        executable_digest=identity.executable_digest,
    )


def _write_unlocked_metadata(
    profile_root: Path,
    identity: RuntimeLeaseIdentity,
) -> None:
    write_private_text(
        runtime_profile_metadata_path(profile_root),
        json.dumps(identity.to_payload(), sort_keys=True),
    )


def test_only_one_writer_can_acquire_profile_lease(tmp_path: Path) -> None:
    first = RuntimeProfileLease.acquire(tmp_path, _identity("desktop"))
    try:
        with pytest.raises(RuntimeLeaseConflict) as raised:
            RuntimeProfileLease.acquire(tmp_path, _identity("cli"))
        assert raised.value.current is not None
        assert raised.value.current.management == "desktop"
    finally:
        first.release()


def test_profile_lease_contention_crosses_a_real_process_boundary(
    tmp_path: Path,
) -> None:
    holder = RuntimeProfileLease.acquire(tmp_path, _identity("desktop"))
    script = """
import sys
from pathlib import Path
from nested_memvid_agent.runtime_profile_lease import (
    RuntimeLeaseConflict,
    RuntimeLeaseIdentity,
    RuntimeProfileLease,
)

identity = RuntimeLeaseIdentity(
    profile_id="default",
    management="cli",
    owner_digest="4" * 64,
    pid=8484,
    process_birth_marker="process-birth-8484",
    executable_digest="5" * 64,
    launch_nonce_digest="6" * 64,
    base_url="http://127.0.0.1:8765/",
    version="0.5.0",
    created_at="2026-07-29T12:01:00+00:00",
)
try:
    RuntimeProfileLease.acquire(Path(sys.argv[1]), identity)
except RuntimeLeaseConflict as exc:
    print(exc.current.management if exc.current else "unknown")
    raise SystemExit(23)
raise SystemExit(24)
"""
    environment = dict(os.environ)
    source_root = Path(__file__).parents[1] / "src"
    environment["PYTHONPATH"] = os.pathsep.join(
        [str(source_root), environment.get("PYTHONPATH", "")]
    )
    try:
        contender = subprocess.run(  # noqa: S603
            [sys.executable, "-c", script, str(tmp_path)],
            cwd=Path(__file__).parents[1],
            env=environment,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    finally:
        holder.release()

    assert contender.returncode == 23
    assert contender.stdout.strip() == "desktop"

    successor = RuntimeProfileLease.acquire(tmp_path, _identity("cli"))
    successor.release()


def test_stale_metadata_is_reported_but_not_treated_as_kill_authority(
    tmp_path: Path,
) -> None:
    _write_unlocked_metadata(tmp_path, _identity("desktop"))

    state = RuntimeProfileLease.inspect(
        tmp_path,
        inspector=lambda _pid: None,
    )

    assert state.status == "stale_unverified"
    assert state.can_terminate is False
    assert state.current is not None
    assert state.current.pid == 4242


@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("owner_digest", "7" * 64),
        ("process_birth_marker", "reused-process-birth"),
        ("executable_digest", "8" * 64),
    ],
)
def test_busy_lease_with_mutated_process_identity_is_foreign_not_kill_authority(
    tmp_path: Path,
    field: str,
    replacement: str,
) -> None:
    identity = _identity("desktop")
    holder = RuntimeProfileLease.acquire(tmp_path, identity)
    mismatched = replace(_matching_process(identity), **{field: replacement})
    try:
        state = RuntimeProfileLease.inspect(
            tmp_path,
            inspector=lambda _pid: mismatched,
        )
    finally:
        holder.release()

    assert state.status == "foreign_or_unrelated"
    assert state.can_terminate is False


def test_verified_compatible_desktop_lease_is_attachable(tmp_path: Path) -> None:
    identity = _identity("desktop")
    holder = RuntimeProfileLease.acquire(tmp_path, identity)
    try:
        state = RuntimeProfileLease.inspect(
            tmp_path,
            profile_id="default",
            version="0.5.0",
            inspector=lambda _pid: _matching_process(identity),
        )
    finally:
        holder.release()

    assert state.status == "attach_desktop"
    assert state.can_terminate is False


def test_verified_cli_lease_can_only_offer_desktop_takeover(tmp_path: Path) -> None:
    identity = _identity("cli")
    holder = RuntimeProfileLease.acquire(tmp_path, identity)
    try:
        state = RuntimeProfileLease.inspect(
            tmp_path,
            profile_id="default",
            version="0.5.0",
            inspector=lambda _pid: _matching_process(identity),
        )
    finally:
        holder.release()

    assert state.status == "offer_desktop_takeover"
    assert state.can_terminate is False


def test_verified_lease_with_other_version_reports_version_conflict(
    tmp_path: Path,
) -> None:
    identity = _identity("desktop", version="0.4.11")
    holder = RuntimeProfileLease.acquire(tmp_path, identity)
    try:
        state = RuntimeProfileLease.inspect(
            tmp_path,
            profile_id="default",
            version="0.5.0",
            inspector=lambda _pid: _matching_process(identity),
        )
    finally:
        holder.release()

    assert state.status == "version_conflict"
    assert state.can_terminate is False


def test_corrupt_metadata_under_a_busy_lock_is_foreign_and_preserved(
    tmp_path: Path,
) -> None:
    lock_path = runtime_profile_lock_path(tmp_path)
    descriptor = open_private_file_descriptor(lock_path)
    handle = os.fdopen(descriptor, "r+", encoding="utf-8")
    lock_exclusive(handle, blocking=False)
    write_private_text(runtime_profile_metadata_path(tmp_path), "{not-json")
    try:
        state = RuntimeProfileLease.inspect(tmp_path, inspector=lambda _pid: None)
    finally:
        unlock(handle)
        handle.close()

    assert state.status == "foreign_or_unrelated"
    assert state.current is None
    assert state.can_terminate is False
    assert runtime_profile_metadata_path(tmp_path).read_text(encoding="utf-8") == "{not-json"


@pytest.mark.skipif(os.name == "nt", reason="POSIX mode bits are not enforced on Windows")
def test_profile_lease_artifacts_are_owner_only(tmp_path: Path) -> None:
    lease = RuntimeProfileLease.acquire(tmp_path, _identity("cli"))
    try:
        assert stat.S_IMODE(runtime_profile_lock_path(tmp_path).stat().st_mode) == 0o600
        assert stat.S_IMODE(runtime_profile_metadata_path(tmp_path).stat().st_mode) == 0o600
    finally:
        lease.release()


def test_release_removes_only_its_metadata_and_makes_profile_available(
    tmp_path: Path,
) -> None:
    lease = RuntimeProfileLease.acquire(tmp_path, _identity("cli"))
    lease.release()
    lease.release()

    state = RuntimeProfileLease.inspect(tmp_path, inspector=lambda _pid: None)

    assert state.status == "available"
    assert state.current is None
    assert state.can_terminate is False
