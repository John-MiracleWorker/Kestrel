#!/usr/bin/env python3
"""Safely create, commit, and roll back Kestrel user launch artifacts.

All artifact changes are made through a pinned, no-follow directory descriptor.
This intentionally keeps staging and backups beside their final artifact: a
rename is therefore atomic even when $HOME and the selected bin directory are
on different filesystems.
"""
from __future__ import annotations

import argparse
import json
import os
import plistlib
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
from collections.abc import Callable, Mapping, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, NoReturn
from uuid import uuid4

SHIM_MARKER = "KESTREL_MANAGED_COMMAND_SHIM_V1"
APP_MARKER = "KESTREL_MANAGED_MACOS_APP_V1"
APP_MARKER_FILENAME = "kestrel-managed-launcher-v1"
MANIFEST_SCHEMA = "kestrel.user_launchers.v2"
_MAX_MANIFEST_BYTES = 1_000_000
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_DIRECTORY = getattr(os, "O_DIRECTORY", 0)
_CLOEXEC = getattr(os, "O_CLOEXEC", 0)


class LauncherArtifactError(RuntimeError):
    pass


def select_bin_directory(*, explicit: str | Path | None, user_home: str | Path,
                         environ: Mapping[str, str] | None = None,
                         current_uid: int | None = None) -> Path:
    environment = os.environ if environ is None else environ
    uid = _current_uid() if current_uid is None else current_uid
    home = _validated_user_home(_canonical_path(Path(user_home)), uid=uid)
    if explicit is not None:
        requested = _canonical_path(Path(explicit))
        if not requested.is_absolute():
            raise LauncherArtifactError("KESTREL_BIN_DIR must be an absolute directory path")
        _create_bin_directory(requested, user_home=home, uid=uid)
        return _validate_bin_directory(requested, user_home=home, uid=uid)
    for raw_entry in environment.get("PATH", "").split(os.pathsep):
        candidate = _canonical_path(Path(raw_entry)) if raw_entry else None
        if candidate is None or not candidate.is_absolute() or not candidate.is_dir():
            continue
        try:
            return _validate_bin_directory(candidate, user_home=home, uid=uid)
        except LauncherArtifactError:
            continue
    fallback = home / ".local" / "bin"
    _create_bin_directory(fallback, user_home=home, uid=uid)
    return _validate_bin_directory(fallback, user_home=home, uid=uid)


def prepare_launchers(*, kestrel_home: str | Path, user_home: str | Path,
                      manifest_path: str | Path, bin_dir: str | Path | None = None,
                      platform: str | None = None,
                      environ: Mapping[str, str] | None = None,
                      which: Callable[[str], str | None] = shutil.which) -> dict[str, Any]:
    environment = os.environ if environ is None else environ
    uid = _current_uid()
    home = _validate_kestrel_home(_canonical_path(Path(kestrel_home)), uid=uid)
    user = _validated_user_home(_canonical_path(Path(user_home)), uid=uid)
    manifest = _prepare_manifest_path(_canonical_path(Path(manifest_path)), uid=uid)
    selected_platform = (platform or sys.platform).lower()
    if selected_platform not in {"darwin", "linux"}:
        raise LauncherArtifactError(f"Unsupported launcher platform: {selected_platform}")
    selected_bin = select_bin_directory(explicit=bin_dir, user_home=user,
                                        environ=environment, current_uid=uid)
    app_parent = user / "Applications"
    if selected_platform == "darwin":
        _create_user_artifact_directory(app_parent, user_home=user, uid=uid)
    transaction_id = uuid4().hex
    artifacts = _derived_artifacts(selected_bin, user, selected_platform, transaction_id)
    for artifact in artifacts:
        artifact["had_previous"] = _preflight_artifact(Path(artifact["target"]), kind=artifact["kind"])
    codesign_status = "not_applicable"
    shim = artifacts[0]
    _write_staged_shim(shim, _shim_text(home), uid=uid)
    if selected_platform == "darwin":
        app = artifacts[1]
        _build_staged_app(app, shim_path=Path(shim["target"]),
                          log_path=home / ".nest" / "server.log", uid=uid)
        codesign_status = _codesign_app(Path(app["staged"]), which=which)
    payload: dict[str, Any] = {
        "schema": MANIFEST_SCHEMA, "transaction_id": transaction_id,
        "kestrel_home": str(home), "user_home": str(user),
        "platform": selected_platform, "bin_dir": str(selected_bin),
        "artifacts": artifacts, "codesign": codesign_status,
    }
    _validate_payload(payload, manifest_path=manifest, uid=uid)
    _write_manifest(manifest, payload)
    try:
        for artifact in artifacts:
            _install_prepared_artifact(artifact, payload=payload, manifest_path=manifest)
    except Exception as exc:
        try:
            _rollback_payload(payload, manifest_path=manifest, remove_manifest=True)
        except Exception as rollback_exc:
            raise LauncherArtifactError("Launcher installation failed and rollback could not be proven: "
                                        f"{rollback_exc}") from exc
        raise LauncherArtifactError(f"Launcher installation failed and was rolled back: {exc}") from exc
    finally:
        _remove_staged_leftovers(payload)
    path_entries = {str(_canonical_path(Path(entry))) for entry in environment.get("PATH", "").split(os.pathsep)
                    if entry and Path(entry).is_absolute()}
    return {"shim_path": str(Path(shim["target"])),
            "app_path": str(Path(artifacts[1]["target"])) if selected_platform == "darwin" else None,
            "manifest_path": str(manifest), "bin_on_path": str(selected_bin) in path_entries,
            "codesign": codesign_status}


def commit_launchers(manifest_path: str | Path) -> dict[str, Any]:
    manifest = _canonical_path(Path(manifest_path))
    payload = _read_manifest(manifest)
    _require_terminal_transaction(payload)
    for artifact in payload["artifacts"]:
        _remove_if_present(artifact, "backup")
    _unlink_manifest(manifest, expected_payload=payload)
    return {"committed": True, "manifest_path": str(manifest)}


def rollback_launchers(manifest_path: str | Path) -> dict[str, Any]:
    manifest = _canonical_path(Path(manifest_path))
    payload = _read_manifest(manifest)
    _rollback_payload(payload, manifest_path=manifest, remove_manifest=True)
    return {"rolled_back": True, "manifest_path": str(manifest)}


def _derived_artifacts(bin_dir: Path, user_home: Path, platform: str, transaction_id: str) -> list[dict[str, Any]]:
    specs: list[tuple[str, Path]] = [("shim", bin_dir / "kestrel")]
    if platform == "darwin":
        specs.append(("app", user_home / "Applications" / "Kestrel.app"))
    return [{"kind": kind, "target": str(target),
             "staged": str(target.parent / f".kestrel-stage-{transaction_id}-{kind}"),
             "backup": str(target.parent / f".kestrel-backup-{transaction_id}-{kind}"),
             "had_previous": False, "backed_up": False, "installed": False}
            for kind, target in specs]


def _install_prepared_artifact(artifact: dict[str, Any], *, payload: dict[str, Any], manifest_path: Path) -> None:
    target, staged, backup = (Path(artifact[key]) for key in ("target", "staged", "backup"))
    if not (target.parent == staged.parent == backup.parent):
        raise LauncherArtifactError("Launcher artifact transaction paths must share a parent")
    with _pinned_directory(target.parent, uid=_current_uid()) as parent_fd:
        target_name, staged_name, backup_name = target.name, staged.name, backup.name
        if bool(artifact["had_previous"]):
            if not _artifact_is_managed_at(parent_fd, target_name, kind=artifact["kind"]):
                raise LauncherArtifactError(f"Managed launcher disappeared before replacement: {target}")
            if _entry_exists(parent_fd, backup_name):
                raise LauncherArtifactError(f"Launcher backup path already exists: {backup}")
            _final_replace(target.parent, parent_fd, target_name, backup_name)
            artifact["backed_up"] = True
            _write_manifest(manifest_path, payload)
        elif _entry_exists(parent_fd, target_name):
            raise LauncherArtifactError(f"Launcher target appeared before installation: {target}")
        _final_replace(target.parent, parent_fd, staged_name, target_name)
        artifact["installed"] = True
        _write_manifest(manifest_path, payload)


def _rollback_payload(payload: dict[str, Any], *, manifest_path: Path, remove_manifest: bool) -> None:
    _validate_payload(payload, manifest_path=manifest_path, uid=_current_uid())
    for artifact in reversed(payload["artifacts"]):
        target, staged, backup = (Path(artifact[key]) for key in ("target", "staged", "backup"))
        with _pinned_directory(target.parent, uid=_current_uid()) as parent_fd:
            target_exists = _entry_exists(parent_fd, target.name)
            backup_exists = _entry_exists(parent_fd, backup.name)
            if backup_exists:
                # A backup is the only durable proof that a predecessor moved.
                # It wins over stale manifest flags from a process crash.
                if not _artifact_is_managed_at(parent_fd, backup.name, kind=artifact["kind"]):
                    raise LauncherArtifactError(f"Rollback backup changed or is unrelated: {backup}")
                if target_exists:
                    if not _artifact_is_managed_at(parent_fd, target.name, kind=artifact["kind"]):
                        raise LauncherArtifactError(f"Rollback target changed or is unrelated: {target}")
                    _remove_managed_at(parent_fd, target.name, kind=artifact["kind"], parent=target.parent)
                _final_replace(target.parent, parent_fd, backup.name, target.name)
            elif target_exists and not bool(artifact["had_previous"]):
                # With no predecessor, a managed target can only be our newly
                # installed artifact.  Remove it.  With a predecessor and no
                # backup, leaving the target intact is the only lossless choice.
                if not _artifact_is_managed_at(parent_fd, target.name, kind=artifact["kind"]):
                    raise LauncherArtifactError(f"Rollback target changed or is unrelated: {target}")
                _remove_managed_at(parent_fd, target.name, kind=artifact["kind"], parent=target.parent)
            if _entry_exists(parent_fd, staged.name):
                _remove_managed_at(parent_fd, staged.name, kind=artifact["kind"], parent=target.parent)
    if remove_manifest:
        _unlink_manifest(manifest_path, expected_payload=payload)


def _remove_staged_leftovers(payload: dict[str, Any]) -> None:
    for artifact in payload["artifacts"]:
        target, staged = Path(artifact["target"]), Path(artifact["staged"])
        with _pinned_directory(target.parent, uid=_current_uid()) as parent_fd:
            if _entry_exists(parent_fd, staged.name):
                _remove_managed_at(parent_fd, staged.name, kind=artifact["kind"], parent=target.parent)


def _remove_if_present(artifact: dict[str, Any], field: str) -> None:
    target, candidate = Path(artifact["target"]), Path(artifact[field])
    with _pinned_directory(target.parent, uid=_current_uid()) as parent_fd:
        if _entry_exists(parent_fd, candidate.name):
            _remove_managed_at(parent_fd, candidate.name, kind=artifact["kind"], parent=target.parent)


def _require_terminal_transaction(payload: dict[str, Any]) -> None:
    """Refuse commit unless both manifest and durable filesystem are terminal."""
    for artifact in payload["artifacts"]:
        if not bool(artifact["installed"]) or (bool(artifact["had_previous"]) and not bool(artifact["backed_up"])):
            raise LauncherArtifactError("Launcher transaction is incomplete and cannot be committed")
        target, staged, backup = (Path(artifact[key]) for key in ("target", "staged", "backup"))
        with _pinned_directory(target.parent, uid=_current_uid()) as parent_fd:
            if not _artifact_is_managed_at(parent_fd, target.name, kind=artifact["kind"]):
                raise LauncherArtifactError("Launcher transaction target is not terminal")
            if _entry_exists(parent_fd, staged.name):
                raise LauncherArtifactError("Launcher transaction staging artifact remains")
            if bool(artifact["had_previous"]):
                if not _artifact_is_managed_at(parent_fd, backup.name, kind=artifact["kind"]):
                    raise LauncherArtifactError("Launcher transaction backup is not terminal")
            elif _entry_exists(parent_fd, backup.name):
                raise LauncherArtifactError("Launcher transaction has an unexpected backup")


def _write_staged_shim(artifact: dict[str, Any], text: str, *, uid: int) -> None:
    target, staged = Path(artifact["target"]), Path(artifact["staged"])
    with _pinned_directory(target.parent, uid=uid) as parent_fd:
        _write_file_at(parent_fd, staged.name, text.encode("utf-8"), 0o755)


def _build_staged_app(artifact: dict[str, Any], *, shim_path: Path, log_path: Path, uid: int) -> None:
    target, staged = Path(artifact["target"]), Path(artifact["staged"])
    with _pinned_directory(target.parent, uid=uid) as parent_fd:
        os.mkdir(staged.name, 0o755, dir_fd=parent_fd)
        with _open_dir_at(parent_fd, staged.name, uid=uid) as app_fd:
            _build_macos_app_at(app_fd, shim_path=shim_path, log_path=log_path)


def _shim_text(kestrel_home: Path) -> str:
    executable = kestrel_home / ".venv" / "bin" / "kestrel"
    return ("#!/bin/bash\n" f"# {SHIM_MARKER}\n" "set -euo pipefail\n"
            f"export KESTREL_HOME={shlex.quote(str(kestrel_home))}\n"
            f"exec {shlex.quote(str(executable))} \"$@\"\n")


def _build_macos_app(app_path: Path, *, shim_path: Path, log_path: Path) -> None:
    contents, macos_dir, resources_dir = app_path / "Contents", app_path / "Contents" / "MacOS", app_path / "Contents" / "Resources"
    macos_dir.mkdir(parents=True, mode=0o755)
    resources_dir.mkdir(parents=True, mode=0o755)
    with (contents / "Info.plist").open("wb") as handle:
        plistlib.dump({"CFBundleDevelopmentRegion": "en", "CFBundleDisplayName": "Kestrel",
                       "CFBundleExecutable": "Kestrel", "CFBundleIdentifier": "com.kestrel.local-launcher",
                       "CFBundleInfoDictionaryVersion": "6.0", "CFBundleName": "Kestrel",
                       "CFBundlePackageType": "APPL", "CFBundleShortVersionString": "1.0",
                       "CFBundleVersion": "1", "LSMinimumSystemVersion": "12.0"}, handle, sort_keys=True)
    (resources_dir / APP_MARKER_FILENAME).write_text(f"{APP_MARKER}\n", encoding="utf-8")
    (resources_dir / APP_MARKER_FILENAME).chmod(0o644)
    recovery = f"Kestrel could not start. See {log_path}. Run: {shim_path} doctor"
    applescript = 'display alert "Kestrel could not start" message ' + f'"{_escape_applescript(recovery)}" as critical'
    _write_executable(macos_dir / "Kestrel", "#!/bin/bash\n" f"# {APP_MARKER}\nset -u\n"
                      f"if ! {shlex.quote(str(shim_path))} open; then\n"
                      f"  /usr/bin/osascript -e {shlex.quote(applescript)} >/dev/null 2>&1 || true\n  exit 1\nfi\n")


def _build_macos_app_at(app_fd: int, *, shim_path: Path, log_path: Path) -> None:
    """Build the app entirely below an already pinned staging-directory fd."""
    os.mkdir("Contents", 0o755, dir_fd=app_fd)
    with _open_dir_at(app_fd, "Contents", uid=_current_uid()) as contents_fd:
        os.mkdir("MacOS", 0o755, dir_fd=contents_fd)
        os.mkdir("Resources", 0o755, dir_fd=contents_fd)
        plist = {"CFBundleDevelopmentRegion": "en", "CFBundleDisplayName": "Kestrel",
                 "CFBundleExecutable": "Kestrel", "CFBundleIdentifier": "com.kestrel.local-launcher",
                 "CFBundleInfoDictionaryVersion": "6.0", "CFBundleName": "Kestrel",
                 "CFBundlePackageType": "APPL", "CFBundleShortVersionString": "1.0",
                 "CFBundleVersion": "1", "LSMinimumSystemVersion": "12.0"}
        import io
        plist_buffer = io.BytesIO()
        plistlib.dump(plist, plist_buffer, sort_keys=True)
        _write_file_at(contents_fd, "Info.plist", plist_buffer.getvalue(), 0o644)
        with _open_dir_at(contents_fd, "Resources", uid=_current_uid()) as resources_fd:
            _write_file_at(resources_fd, APP_MARKER_FILENAME, f"{APP_MARKER}\n".encode(), 0o644)
        recovery = f"Kestrel could not start. See {log_path}. Run: {shim_path} doctor"
        applescript = 'display alert "Kestrel could not start" message ' + f'"{_escape_applescript(recovery)}" as critical'
        executable = ("#!/bin/bash\n" f"# {APP_MARKER}\nset -u\n"
                      f"if ! {shlex.quote(str(shim_path))} open; then\n"
                      f"  /usr/bin/osascript -e {shlex.quote(applescript)} >/dev/null 2>&1 || true\n  exit 1\nfi\n")
        with _open_dir_at(contents_fd, "MacOS", uid=_current_uid()) as macos_fd:
            _write_file_at(macos_fd, "Kestrel", executable.encode(), 0o755)


def _codesign_app(app_path: Path, *, which: Callable[[str], str | None]) -> str:
    codesign = which("codesign")
    if codesign is None:
        return "unavailable"
    try:
        subprocess.run([codesign, "--force", "--deep", "--sign", "-", str(app_path)], check=True, capture_output=True, text=True)
        subprocess.run([codesign, "--verify", "--deep", "--strict", str(app_path)], check=True, capture_output=True, text=True)
    except subprocess.CalledProcessError as exc:
        raise LauncherArtifactError("Generated Kestrel.app failed ad-hoc signing verification") from exc
    return "verified"


def _preflight_artifact(path: Path, *, kind: str) -> bool:
    if _path_has_symlink(path):
        raise LauncherArtifactError(f"Refusing symbolic-link launcher target: {path}")
    if not path.exists():
        return False
    if not _artifact_is_managed(path, kind=kind):
        raise LauncherArtifactError(f"Refusing to overwrite unrelated existing launcher: {path}")
    return True


def _artifact_is_managed(path: Path, *, kind: str) -> bool:
    if path.is_symlink():
        return False
    try:
        if kind == "shim":
            lines = path.read_text(encoding="utf-8").splitlines()
            return len(lines) >= 5 and lines[0] == "#!/bin/bash" and lines[1] == f"# {SHIM_MARKER}" and lines[2] == "set -euo pipefail" and lines[-1].startswith("exec ")
        if kind == "app":
            marker = path / "Contents" / "Resources" / APP_MARKER_FILENAME
            executable = path / "Contents" / "MacOS" / "Kestrel"
            plist = path / "Contents" / "Info.plist"
            if marker.is_symlink() or executable.is_symlink() or plist.is_symlink():
                return False
            lines = executable.read_text(encoding="utf-8").splitlines()
            with plist.open("rb") as handle:
                data = plistlib.load(handle)
            return (marker.read_text(encoding="utf-8") == f"{APP_MARKER}\n" and
                    data.get("CFBundleIdentifier") == "com.kestrel.local-launcher" and
                    data.get("CFBundleExecutable") == "Kestrel" and len(lines) >= 2 and
                    lines[0] == "#!/bin/bash" and lines[1] == f"# {APP_MARKER}")
    except (OSError, UnicodeDecodeError, plistlib.InvalidFileException):
        return False
    raise LauncherArtifactError(f"Unknown launcher artifact kind: {kind}")


def _artifact_is_managed_at(parent_fd: int, name: str, *, kind: str) -> bool:
    try:
        if kind == "shim":
            lines = _read_file_at(parent_fd, name).decode("utf-8").splitlines()
            return len(lines) >= 5 and lines[0] == "#!/bin/bash" and lines[1] == f"# {SHIM_MARKER}" and lines[2] == "set -euo pipefail" and lines[-1].startswith("exec ")
        if kind != "app":
            raise LauncherArtifactError(f"Unknown launcher artifact kind: {kind}")
        with _open_dir_at(parent_fd, name, uid=_current_uid()) as app_fd:
            with _open_dir_at(app_fd, "Contents", uid=_current_uid()) as contents_fd:
                with _open_dir_at(contents_fd, "Resources", uid=_current_uid()) as resources_fd:
                    marker = _read_file_at(resources_fd, APP_MARKER_FILENAME).decode("utf-8")
                with _open_dir_at(contents_fd, "MacOS", uid=_current_uid()) as macos_fd:
                    executable = _read_file_at(macos_fd, "Kestrel").decode("utf-8").splitlines()
                data = plistlib.loads(_read_file_at(contents_fd, "Info.plist"))
        return (marker == f"{APP_MARKER}\n" and data.get("CFBundleIdentifier") == "com.kestrel.local-launcher" and
                data.get("CFBundleExecutable") == "Kestrel" and len(executable) >= 2 and
                executable[0] == "#!/bin/bash" and executable[1] == f"# {APP_MARKER}")
    except (OSError, UnicodeDecodeError, plistlib.InvalidFileException):
        return False


def _remove_managed_at(parent_fd: int, name: str, *, kind: str, parent: Path) -> None:
    with _verified_artifact_fd(parent_fd, name, kind=kind) as verified_fd:
        quarantine = f".kestrel-quarantine-{uuid4().hex}-{kind}"
        _final_replace(parent, parent_fd, name, quarantine)
        try:
            _assert_entry_matches_fd(parent_fd, quarantine, verified_fd)
            _before_quarantine_delete(parent)
            _assert_entry_matches_fd(parent_fd, quarantine, verified_fd)
            if kind == "shim":
                os.unlink(quarantine, dir_fd=parent_fd)
            else:
                _remove_tree_at(parent_fd, quarantine)
        except Exception as exc:
            raise LauncherArtifactError(
                f"Refusing to delete changed launcher artifact; quarantined at {parent / quarantine}"
            ) from exc


def _remove_tree_at(parent_fd: int, name: str) -> None:
    with _open_dir_at(parent_fd, name, uid=_current_uid()) as child_fd:
        with os.scandir(child_fd) as entries:
            names = [(entry.name, entry.is_dir(follow_symlinks=False), entry.is_symlink()) for entry in entries]
        for child_name, is_dir, is_link in names:
            if is_link:
                raise LauncherArtifactError("Refusing symlink in managed app artifact")
            if is_dir:
                _remove_tree_at(child_fd, child_name)
            else:
                os.unlink(child_name, dir_fd=child_fd)
    os.rmdir(name, dir_fd=parent_fd)


def _validate_payload(payload: dict[str, Any], *, manifest_path: Path, uid: int) -> None:
    required = {"schema", "transaction_id", "kestrel_home", "user_home", "platform", "bin_dir", "artifacts", "codesign"}
    if not isinstance(payload, dict) or set(payload) != required or payload.get("schema") != MANIFEST_SCHEMA:
        raise LauncherArtifactError("Launcher transaction manifest has an unknown schema")
    transaction_id = payload.get("transaction_id")
    platform = payload.get("platform")
    if not isinstance(transaction_id, str) or len(transaction_id) != 32 or any(char not in "0123456789abcdef" for char in transaction_id) or platform not in {"linux", "darwin"}:
        raise LauncherArtifactError("Launcher transaction manifest has invalid transaction metadata")
    if not all(isinstance(payload.get(key), str) for key in ("kestrel_home", "user_home", "bin_dir", "codesign")):
        raise LauncherArtifactError("Launcher transaction manifest has invalid path metadata")
    home = _validate_kestrel_home(_canonical_path(Path(payload["kestrel_home"])), uid=uid)
    user = _validated_user_home(_canonical_path(Path(payload["user_home"])), uid=uid)
    bin_dir = _validate_bin_directory(_canonical_path(Path(payload["bin_dir"])), user_home=user, uid=uid)
    if str(home) != payload["kestrel_home"] or str(user) != payload["user_home"] or str(bin_dir) != payload["bin_dir"]:
        raise LauncherArtifactError("Launcher transaction manifest paths are not canonical")
    artifacts = payload.get("artifacts")
    expected = _derived_artifacts(bin_dir, user, platform, transaction_id)
    if not isinstance(artifacts, list) or len(artifacts) != len(expected):
        raise LauncherArtifactError("Launcher transaction manifest has invalid artifacts")
    for actual, derived in zip(artifacts, expected, strict=True):
        if not isinstance(actual, dict) or set(actual) != set(derived):
            raise LauncherArtifactError("Launcher transaction manifest has invalid artifact schema")
        for key in ("kind", "target", "staged", "backup"):
            if actual.get(key) != derived[key]:
                raise LauncherArtifactError("Launcher transaction manifest artifact paths do not match transaction")
        if not all(isinstance(actual.get(key), bool) for key in ("had_previous", "backed_up", "installed")):
            raise LauncherArtifactError("Launcher transaction manifest has invalid artifact state")
    allowed_codesign = {"not_applicable"} if platform == "linux" else {"unavailable", "verified"}
    if payload["codesign"] not in allowed_codesign:
        raise LauncherArtifactError("Launcher transaction manifest has invalid signing state")
    # Bind validation to the supplied manifest's own safe, pinned parent too.
    _validate_manifest_parent(manifest_path, uid=uid)


def _read_manifest(path: Path) -> dict[str, Any]:
    _validate_manifest_parent(path, uid=_current_uid())
    if path.is_symlink() or not path.is_file():
        raise LauncherArtifactError(f"Launcher transaction manifest is missing or unsafe: {path}")
    metadata = path.stat()
    if metadata.st_uid != _current_uid() or stat.S_IMODE(metadata.st_mode) != 0o600 or metadata.st_size > _MAX_MANIFEST_BYTES:
        raise LauncherArtifactError(f"Launcher transaction manifest has unsafe metadata: {path}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise LauncherArtifactError("Launcher transaction manifest is invalid") from exc
    if not isinstance(payload, dict):
        raise LauncherArtifactError("Launcher transaction manifest has an unknown schema")
    _validate_payload(payload, manifest_path=path, uid=_current_uid())
    return payload


def _write_manifest(path: Path, payload: Mapping[str, Any]) -> None:
    raw = (json.dumps(payload, sort_keys=True) + "\n").encode("utf-8")
    with _pinned_directory(path.parent, uid=_current_uid()) as parent_fd:
        temporary = f".{path.name}.{uuid4().hex}.tmp"
        _write_file_at(parent_fd, temporary, raw, 0o600)
        _final_replace(path.parent, parent_fd, temporary, path.name)


def _unlink_manifest(path: Path, *, expected_payload: Mapping[str, Any] | None = None) -> None:
    with _pinned_directory(path.parent, uid=_current_uid()) as parent_fd:
        with _verified_manifest_fd(parent_fd, path.name, expected_payload=expected_payload) as verified_fd:
            quarantine = f".kestrel-quarantine-{uuid4().hex}-manifest"
            _final_replace(path.parent, parent_fd, path.name, quarantine)
            try:
                _assert_entry_matches_fd(parent_fd, quarantine, verified_fd)
                _before_quarantine_delete(path.parent)
                _assert_entry_matches_fd(parent_fd, quarantine, verified_fd)
                os.unlink(quarantine, dir_fd=parent_fd)
            except Exception as exc:
                raise LauncherArtifactError(
                    f"Refusing to delete changed transaction manifest; quarantined at {path.parent / quarantine}"
                ) from exc


def _write_file_at(parent_fd: int, name: str, data: bytes, mode: int) -> None:
    descriptor = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _CLOEXEC | _NOFOLLOW, mode, dir_fd=parent_fd)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = -1
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _read_file_at(parent_fd: int, name: str) -> bytes:
    descriptor = os.open(name, os.O_RDONLY | _CLOEXEC | _NOFOLLOW, dir_fd=parent_fd)
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise OSError("not a regular file")
        chunks: list[bytes] = []
        while chunk := os.read(descriptor, 65536):
            chunks.append(chunk)
        return b"".join(chunks)
    finally:
        os.close(descriptor)


@contextmanager
def _verified_artifact_fd(parent_fd: int, name: str, *, kind: str) -> Iterator[int]:
    flags = os.O_RDONLY | _CLOEXEC | _NOFOLLOW
    if kind == "app":
        flags |= _DIRECTORY
    fd = os.open(name, flags, dir_fd=parent_fd)
    try:
        metadata = os.fstat(fd)
        if (kind == "shim" and not stat.S_ISREG(metadata.st_mode)) or (kind == "app" and not stat.S_ISDIR(metadata.st_mode)):
            raise LauncherArtifactError("Launcher artifact has an unsafe type")
        if not _artifact_is_managed_at(parent_fd, name, kind=kind):
            raise LauncherArtifactError(f"Refusing to remove changed or unrelated launcher artifact: {name}")
        # Re-open by fd-relative identity to bind the validation to this inode.
        if not _artifact_matches_open_fd(fd, kind=kind):
            raise LauncherArtifactError(f"Refusing to remove changed launcher artifact: {name}")
        yield fd
    finally:
        os.close(fd)


def _artifact_matches_open_fd(fd: int, *, kind: str) -> bool:
    try:
        if kind == "shim":
            os.lseek(fd, 0, os.SEEK_SET)
            text = b"".join(iter(lambda: os.read(fd, 65536), b"")).decode("utf-8")
            lines = text.splitlines()
            return len(lines) >= 5 and lines[0] == "#!/bin/bash" and lines[1] == f"# {SHIM_MARKER}" and lines[2] == "set -euo pipefail" and lines[-1].startswith("exec ")
        if kind == "app":
            with _open_dir_at(fd, "Contents", uid=_current_uid()) as contents_fd:
                with _open_dir_at(contents_fd, "Resources", uid=_current_uid()) as resources_fd:
                    marker = _read_file_at(resources_fd, APP_MARKER_FILENAME).decode("utf-8")
                with _open_dir_at(contents_fd, "MacOS", uid=_current_uid()) as macos_fd:
                    executable = _read_file_at(macos_fd, "Kestrel").decode("utf-8").splitlines()
                data = plistlib.loads(_read_file_at(contents_fd, "Info.plist"))
            return marker == f"{APP_MARKER}\n" and data.get("CFBundleIdentifier") == "com.kestrel.local-launcher" and data.get("CFBundleExecutable") == "Kestrel" and len(executable) >= 2 and executable[0] == "#!/bin/bash" and executable[1] == f"# {APP_MARKER}"
    except (OSError, UnicodeDecodeError, plistlib.InvalidFileException):
        return False
    raise LauncherArtifactError(f"Unknown launcher artifact kind: {kind}")


@contextmanager
def _verified_manifest_fd(parent_fd: int, name: str, *, expected_payload: Mapping[str, Any] | None) -> Iterator[int]:
    fd = os.open(name, os.O_RDONLY | _CLOEXEC | _NOFOLLOW, dir_fd=parent_fd)
    try:
        metadata = os.fstat(fd)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != _current_uid() or stat.S_IMODE(metadata.st_mode) != 0o600:
            raise LauncherArtifactError("Launcher transaction manifest changed or is unsafe")
        if expected_payload is not None:
            os.lseek(fd, 0, os.SEEK_SET)
            raw = b"".join(iter(lambda: os.read(fd, 65536), b""))
            expected = (json.dumps(expected_payload, sort_keys=True) + "\n").encode("utf-8")
            if raw != expected:
                raise LauncherArtifactError("Launcher transaction manifest changed before removal")
        yield fd
    finally:
        os.close(fd)


def _assert_entry_matches_fd(parent_fd: int, name: str, fd: int) -> None:
    current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    verified = os.fstat(fd)
    if (current.st_dev, current.st_ino) != (verified.st_dev, verified.st_ino):
        raise LauncherArtifactError("Launcher artifact changed at final deletion boundary")


def _write_executable(path: Path, text: str) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _CLOEXEC | _NOFOLLOW, 0o755)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = -1
            handle.write(text.encode("utf-8")); handle.flush(); os.fsync(handle.fileno())
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _final_replace(parent: Path, parent_fd: int, source: str, destination: str) -> None:
    _assert_directory_current(parent, parent_fd)
    _before_final_mutation(parent)
    _assert_directory_current(parent, parent_fd)
    os.replace(source, destination, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)


def _before_final_mutation(parent: Path) -> None:
    """Test seam immediately before an irreversible directory-fd mutation."""


def _before_quarantine_delete(parent: Path) -> None:
    """Test seam after quarantine identity verification and before deletion."""


@contextmanager
def _pinned_directory(path: Path, *, uid: int) -> Iterator[int]:
    if _path_has_symlink(path):
        raise LauncherArtifactError(f"Launcher target parent uses a symbolic link: {path}")
    fd = os.open(path, os.O_RDONLY | _DIRECTORY | _CLOEXEC | _NOFOLLOW)
    try:
        metadata = os.fstat(fd)
        if not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != uid:
            raise LauncherArtifactError(f"Expected a safe current-user directory: {path}")
        yield fd
    finally:
        os.close(fd)


@contextmanager
def _open_dir_at(parent_fd: int, name: str, *, uid: int) -> Iterator[int]:
    fd = os.open(name, os.O_RDONLY | _DIRECTORY | _CLOEXEC | _NOFOLLOW, dir_fd=parent_fd)
    try:
        metadata = os.fstat(fd)
        if not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != uid:
            raise LauncherArtifactError("Expected a safe current-user artifact directory")
        yield fd
    finally:
        os.close(fd)


def _assert_directory_current(path: Path, fd: int) -> None:
    try:
        current = os.lstat(path)
    except OSError as exc:
        raise LauncherArtifactError(f"Launcher target parent changed: {path}") from exc
    pinned = os.fstat(fd)
    if stat.S_ISLNK(current.st_mode) or (current.st_dev, current.st_ino) != (pinned.st_dev, pinned.st_ino):
        raise LauncherArtifactError(f"Launcher target parent changed: {path}")


def _entry_exists(parent_fd: int, name: str) -> bool:
    try:
        os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return False
    return True


def _validate_kestrel_home(path: Path, *, uid: int) -> Path:
    if not path.is_absolute() or _path_has_symlink(path):
        raise LauncherArtifactError("Kestrel home must be absolute and must not use symbolic links")
    _validate_owned_directory(path, uid=uid)
    executable = path / ".venv" / "bin" / "kestrel"
    if executable.is_symlink() or not executable.is_file() or executable.stat().st_uid != uid or not os.access(executable, os.X_OK):
        raise LauncherArtifactError(f"Kestrel executable is unavailable or unsafe: {executable}")
    return path


def _validated_user_home(path: Path, *, uid: int) -> Path:
    if not path.is_absolute() or _path_has_symlink(path):
        raise LauncherArtifactError("User home must be absolute and must not use symbolic links")
    _validate_owned_directory(path, uid=uid)
    return path


def _validate_bin_directory(path: Path, *, user_home: Path, uid: int) -> Path:
    if not path.is_absolute() or _path_has_symlink(path):
        raise LauncherArtifactError(f"Launcher bin directory must not use symbolic links: {path}")
    parts = {part.lower() for part in path.parts}
    if ".venv" in parts or "venv" in parts or any(part.endswith(".app") for part in parts):
        raise LauncherArtifactError(f"Launcher bin directory is inside a transient runtime or app: {path}")
    system_temp = _canonical_path(Path(tempfile.gettempdir()))
    if _is_relative_to(path, system_temp) and not _is_relative_to(path, user_home):
        raise LauncherArtifactError(f"Launcher bin directory is temporary: {path}")
    _validate_owned_directory(path, uid=uid)
    if not os.access(path, os.W_OK | os.X_OK):
        raise LauncherArtifactError(f"Launcher bin directory is not user-writable: {path}")
    return path


def _create_bin_directory(path: Path, *, user_home: Path, uid: int) -> None:
    if path.exists():
        if not path.is_dir():
            raise LauncherArtifactError(f"Launcher bin directory is not a directory: {path}")
        return
    if not path.is_absolute() or _path_has_symlink(path):
        raise LauncherArtifactError(f"Launcher bin directory must not use symbolic links: {path}")
    if not _is_relative_to(path, user_home):
        parent = _nearest_existing_parent(path)
        _validate_owned_directory(parent, uid=uid)
        if not os.access(parent, os.W_OK | os.X_OK):
            raise LauncherArtifactError(f"Cannot create launcher bin directory: {path}")
    path.mkdir(parents=True, mode=0o700)


def _create_user_artifact_directory(path: Path, *, user_home: Path, uid: int) -> None:
    if not path.is_absolute() or not _is_relative_to(path, user_home) or _path_has_symlink(path):
        raise LauncherArtifactError(f"User launcher directory must remain safe inside the user home: {path}")
    path.mkdir(parents=True, mode=0o700, exist_ok=True)
    _validate_owned_directory(path, uid=uid)


def _prepare_manifest_path(path: Path, *, uid: int) -> Path:
    if not path.is_absolute() or path.is_symlink() or path.exists() or _path_has_symlink(path.parent):
        raise LauncherArtifactError("Launcher transaction manifest path is unsafe or already exists")
    path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    _validate_manifest_parent(path, uid=uid)
    return path


def _validate_manifest_parent(path: Path, *, uid: int) -> None:
    if not path.is_absolute() or _path_has_symlink(path.parent):
        raise LauncherArtifactError("Launcher transaction directory uses a symbolic link")
    _validate_owned_directory(path.parent, uid=uid)


def _validate_owned_directory(path: Path, *, uid: int) -> None:
    if path.is_symlink() or not path.is_dir():
        raise LauncherArtifactError(f"Expected a safe directory: {path}")
    if path.stat().st_uid != uid:
        raise LauncherArtifactError(f"Directory is not owned by the current user: {path}")


def _canonical_path(path: Path) -> Path:
    """Lexically canonicalize Darwin's documented /var compatibility alias.

    This is deliberately not Path.resolve(): user-controlled symlinks remain
    visible to the no-follow validation rather than being silently accepted.
    """
    normalized = Path(os.path.normpath(str(path)))
    if _has_darwin_var_alias() and (normalized == Path("/var") or _is_relative_to(normalized, Path("/var"))):
        return Path("/private") / normalized.relative_to("/")
    return normalized


def _has_darwin_var_alias() -> bool:
    """Recognize only the host's real /var -> /private/var compatibility alias."""
    try:
        return os.path.islink("/var") and os.path.realpath("/var") == "/private/var"
    except OSError:
        return False


def _path_has_symlink(path: Path) -> bool:
    if not path.is_absolute():
        return True
    current = Path(path.anchor)
    for component in path.parts[1:]:
        current /= component
        try:
            if stat.S_ISLNK(os.lstat(current).st_mode):
                return True
        except FileNotFoundError:
            continue
    return False


def _nearest_existing_parent(path: Path) -> Path:
    candidate = path
    while not candidate.exists():
        if candidate.parent == candidate:
            raise LauncherArtifactError(f"Cannot find a safe parent for launcher directory: {path}")
        candidate = candidate.parent
    return candidate


def _escape_applescript(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def _current_uid() -> int:
    getuid = getattr(os, "getuid", None)
    return int(getuid()) if callable(getuid) else 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Create or roll back Kestrel user launch artifacts.")
    subparsers = parser.add_subparsers(dest="command", required=True)
    prepare = subparsers.add_parser("prepare")
    prepare.add_argument("--kestrel-home", type=Path, required=True); prepare.add_argument("--user-home", type=Path, required=True)
    prepare.add_argument("--manifest", type=Path, required=True); prepare.add_argument("--bin-dir", type=Path)
    prepare.add_argument("--platform", choices=("darwin", "linux"))
    for command in ("commit", "rollback"):
        action = subparsers.add_parser(command); action.add_argument("--manifest", type=Path, required=True)
    return parser


def run(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "prepare":
            result = prepare_launchers(kestrel_home=args.kestrel_home, user_home=args.user_home,
                                       manifest_path=args.manifest, bin_dir=args.bin_dir, platform=args.platform)
        elif args.command == "commit": result = commit_launchers(args.manifest)
        else: result = rollback_launchers(args.manifest)
    except LauncherArtifactError as exc:
        sys.stderr.write(f"ERROR: {exc}\n"); return 1
    sys.stdout.write(json.dumps(result, sort_keys=True) + "\n"); return 0


def main() -> NoReturn:
    raise SystemExit(run())


if __name__ == "__main__":
    main()
