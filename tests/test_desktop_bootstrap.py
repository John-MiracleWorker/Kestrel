from __future__ import annotations

import json
import os
from hashlib import sha256
from importlib import metadata as importlib_metadata
from pathlib import Path

import pytest

from nested_memvid_agent.desktop_bootstrap import consume_desktop_bootstrap

_PACKAGE_VERSION = importlib_metadata.version("nested-memvid-agent")

_MEMORY_LAYERS = [
    "working",
    "episodic",
    "semantic",
    "procedural",
    "self",
    "policy",
]


def _bootstrap_payload(
    tmp_path: Path,
    *,
    token: str = "desktop-secret-token",
    nonce: str = "launch-nonce",
) -> dict[str, object]:
    profile_root = tmp_path / "profile"
    profile_root.mkdir(exist_ok=True)
    return {
        "schema": "kestrel.desktop.bootstrap.v1",
        "profile_id": "default",
        "profile_root": str(profile_root),
        "state_path": str(profile_root / "state" / "agent.db"),
        "memory_dir": str(profile_root / "memory"),
        "runtime_settings_path": str(profile_root / "config" / "runtime_settings.json"),
        "launch_nonce": nonce,
        "api_token": token,
        "parent_pid": 4242,
        "parent_birth_marker": "desktop-parent-birth-marker",
        "resource_manifest_digest": "sha256:" + ("a" * 64),
        "assurance_mode": "release",
        "memory_layers": list(_MEMORY_LAYERS),
    }


def _write_bootstrap(path: Path, payload: dict[str, object]) -> Path:
    path.write_text(json.dumps(payload), encoding="utf-8")
    path.chmod(0o600)
    return path


def test_bootstrap_consumes_private_file_without_leaking_secrets(tmp_path: Path) -> None:
    path = _write_bootstrap(tmp_path / "bootstrap.json", _bootstrap_payload(tmp_path))

    launch = consume_desktop_bootstrap(path)

    assert launch.api_token == "desktop-secret-token"
    assert launch.launch_nonce_matches("launch-nonce")
    assert launch.assurance_mode == "release"
    assert not launch.launch_nonce_matches("wrong-nonce")
    assert not path.exists()
    assert "desktop-secret-token" not in repr(launch)
    assert "launch-nonce" not in repr(launch)
    public_payload = launch.to_public_payload()
    assert public_payload == {
        "schema": "kestrel.desktop.readiness.v1",
        "ready": True,
        "profile_id": "default",
        "launch_nonce_digest": sha256(b"launch-nonce").hexdigest(),
        "sidecar_version": _PACKAGE_VERSION,
        "state_schema_version": 21,
        "routing_schema_version": 5,
        "memory_layers": list(_MEMORY_LAYERS),
    }
    serialized = json.dumps(public_payload, sort_keys=True)
    assert "desktop-secret-token" not in serialized
    assert "launch-nonce" not in serialized


def test_bootstrap_rejects_symlink_and_oversized_input(tmp_path: Path) -> None:
    target = _write_bootstrap(tmp_path / "target.json", _bootstrap_payload(tmp_path))
    link = tmp_path / "bootstrap-link.json"
    link.symlink_to(target)

    with pytest.raises(PermissionError, match="symlink"):
        consume_desktop_bootstrap(link)

    oversized = tmp_path / "oversized.json"
    oversized.write_bytes(b" " * ((16 * 1024) + 1))
    oversized.chmod(0o600)
    with pytest.raises(ValueError, match="16 KiB"):
        consume_desktop_bootstrap(oversized)

    assert link.is_symlink()
    assert target.exists()
    assert oversized.exists()


@pytest.mark.skipif(os.name == "nt", reason="POSIX mode bits are not a Windows ACL")
def test_bootstrap_rejects_non_owner_only_permissions(tmp_path: Path) -> None:
    path = _write_bootstrap(tmp_path / "bootstrap.json", _bootstrap_payload(tmp_path))
    path.chmod(0o640)

    with pytest.raises(PermissionError, match="owner-only"):
        consume_desktop_bootstrap(path)

    assert path.exists()


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ({"unexpected": "field"}, "exactly"),
        (
            {"memory_preflight_receipt": {"forged": True}},
            "exactly",
        ),
        ({"memory_layers": _MEMORY_LAYERS[:-1]}, "six default"),
        ({"parent_pid": True}, "parent_pid"),
        ({"assurance_mode": "test"}, "assurance_mode"),
    ],
)
def test_bootstrap_rejects_non_strict_schema(
    tmp_path: Path,
    mutation: dict[str, object],
    message: str,
) -> None:
    payload = _bootstrap_payload(tmp_path)
    payload.update(mutation)
    path = _write_bootstrap(tmp_path / "bootstrap.json", payload)

    with pytest.raises(ValueError, match=message):
        consume_desktop_bootstrap(path)

    assert not path.exists()


def test_bootstrap_rejects_paths_outside_resolved_profile_root(tmp_path: Path) -> None:
    payload = _bootstrap_payload(tmp_path)
    payload["memory_dir"] = str(tmp_path / "outside-memory")
    path = _write_bootstrap(tmp_path / "bootstrap.json", payload)

    with pytest.raises(ValueError, match="memory_dir.*profile_root"):
        consume_desktop_bootstrap(path)

    assert not path.exists()
