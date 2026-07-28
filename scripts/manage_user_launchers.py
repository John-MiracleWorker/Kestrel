#!/usr/bin/env python3
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
from pathlib import Path
from typing import Any, NoReturn
from uuid import uuid4

SHIM_MARKER = "KESTREL_MANAGED_COMMAND_SHIM_V1"
APP_MARKER = "KESTREL_MANAGED_MACOS_APP_V1"
APP_MARKER_FILENAME = "kestrel-managed-launcher-v1"
MANIFEST_SCHEMA = "kestrel.user_launchers.v1"
_MAX_MANIFEST_BYTES = 1_000_000


class LauncherArtifactError(RuntimeError):
    pass


def select_bin_directory(
    *,
    explicit: str | Path | None,
    user_home: str | Path,
    environ: Mapping[str, str] | None = None,
    current_uid: int | None = None,
) -> Path:
    environment = os.environ if environ is None else environ
    uid = _current_uid() if current_uid is None else current_uid
    home = _validated_user_home(Path(user_home), uid=uid)
    if explicit is not None:
        requested = Path(explicit)
        if not requested.is_absolute():
            raise LauncherArtifactError(
                "KESTREL_BIN_DIR must be an absolute directory path"
            )
        _create_bin_directory(requested, user_home=home, uid=uid)
        return _validate_bin_directory(
            requested,
            user_home=home,
            uid=uid,
        )
    for raw_entry in environment.get("PATH", "").split(os.pathsep):
        if not raw_entry:
            continue
        candidate = Path(raw_entry)
        if not candidate.is_absolute() or not candidate.is_dir():
            continue
        try:
            return _validate_bin_directory(
                candidate,
                user_home=home,
                uid=uid,
            )
        except LauncherArtifactError:
            continue
    fallback = home / ".local" / "bin"
    _create_bin_directory(fallback, user_home=home, uid=uid)
    return _validate_bin_directory(fallback, user_home=home, uid=uid)


def prepare_launchers(
    *,
    kestrel_home: str | Path,
    user_home: str | Path,
    manifest_path: str | Path,
    bin_dir: str | Path | None = None,
    platform: str | None = None,
    environ: Mapping[str, str] | None = None,
    which: Callable[[str], str | None] = shutil.which,
) -> dict[str, Any]:
    environment = os.environ if environ is None else environ
    uid = _current_uid()
    home = _validate_kestrel_home(Path(kestrel_home), uid=uid)
    user = _validated_user_home(Path(user_home), uid=uid)
    manifest = _prepare_manifest_path(Path(manifest_path), uid=uid)
    selected_platform = (platform or sys.platform).lower()
    if selected_platform not in {"darwin", "linux"}:
        raise LauncherArtifactError(
            f"Unsupported launcher platform: {selected_platform}"
        )
    selected_bin = select_bin_directory(
        explicit=bin_dir,
        user_home=user,
        environ=environment,
        current_uid=uid,
    )
    shim_target = selected_bin / "kestrel"
    app_target = (
        user / "Applications" / "Kestrel.app"
        if selected_platform == "darwin"
        else None
    )
    if app_target is not None:
        _create_user_artifact_directory(
            app_target.parent,
            user_home=user,
            uid=uid,
        )
    shim_previous = _preflight_artifact(shim_target, kind="shim")
    app_previous = (
        _preflight_artifact(app_target, kind="app")
        if app_target is not None
        else False
    )
    transaction_id = uuid4().hex
    staging_dir = manifest.parent / f".launcher-stage-{transaction_id}"
    backup_dir = manifest.parent / f".launcher-backups-{transaction_id}"
    _create_private_directory(staging_dir, uid=uid)
    _create_private_directory(backup_dir, uid=uid)
    staged_shim = staging_dir / "kestrel"
    _write_executable(staged_shim, _shim_text(home))
    artifacts: list[dict[str, Any]] = [
        {
            "kind": "shim",
            "target": str(shim_target),
            "staged": str(staged_shim),
            "backup": str(backup_dir / "shim.previous"),
            "had_previous": shim_previous,
            "backed_up": False,
            "installed": False,
        }
    ]
    codesign_status = "not_applicable"
    if app_target is not None:
        staged_app = staging_dir / "Kestrel.app"
        _build_macos_app(
            staged_app,
            shim_path=shim_target,
            log_path=home / ".nest" / "server.log",
        )
        codesign_status = _codesign_app(staged_app, which=which)
        artifacts.append(
            {
                "kind": "app",
                "target": str(app_target),
                "staged": str(staged_app),
                "backup": str(backup_dir / "app.previous"),
                "had_previous": app_previous,
                "backed_up": False,
                "installed": False,
            }
        )
    payload: dict[str, Any] = {
        "schema": MANIFEST_SCHEMA,
        "kestrel_home": str(home),
        "user_home": str(user),
        "platform": selected_platform,
        "bin_dir": str(selected_bin),
        "backup_dir": str(backup_dir),
        "artifacts": artifacts,
        "codesign": codesign_status,
    }
    _write_manifest(manifest, payload)
    try:
        for artifact in artifacts:
            _install_prepared_artifact(
                artifact,
                payload=payload,
                manifest_path=manifest,
            )
    except Exception as exc:
        try:
            _rollback_payload(
                payload,
                manifest_path=manifest,
                remove_manifest=True,
            )
        except Exception as rollback_exc:
            raise LauncherArtifactError(
                "Launcher installation failed and rollback could not be proven: "
                f"{rollback_exc}"
            ) from exc
        raise LauncherArtifactError(
            f"Launcher installation failed and was rolled back: {exc}"
        ) from exc
    finally:
        _remove_staging_directory(staging_dir)
    path_entries = {
        str(Path(entry).resolve())
        for entry in environment.get("PATH", "").split(os.pathsep)
        if entry and Path(entry).is_absolute()
    }
    return {
        "shim_path": str(shim_target.resolve()),
        "app_path": str(app_target.resolve()) if app_target is not None else None,
        "manifest_path": str(manifest.resolve()),
        "bin_on_path": str(selected_bin) in path_entries,
        "codesign": codesign_status,
    }


def commit_launchers(manifest_path: str | Path) -> dict[str, Any]:
    manifest = Path(manifest_path)
    payload = _read_manifest(manifest)
    for artifact in payload["artifacts"]:
        backup = Path(artifact["backup"])
        if backup.exists():
            _remove_managed_artifact(backup, kind=str(artifact["kind"]))
    backup_dir = Path(payload["backup_dir"])
    _remove_empty_directory(backup_dir)
    manifest.unlink()
    return {"committed": True, "manifest_path": str(manifest)}


def rollback_launchers(manifest_path: str | Path) -> dict[str, Any]:
    manifest = Path(manifest_path)
    payload = _read_manifest(manifest)
    _rollback_payload(
        payload,
        manifest_path=manifest,
        remove_manifest=True,
    )
    return {"rolled_back": True, "manifest_path": str(manifest)}


def _install_prepared_artifact(
    artifact: dict[str, Any],
    *,
    payload: dict[str, Any],
    manifest_path: Path,
) -> None:
    target = Path(artifact["target"])
    staged = Path(artifact["staged"])
    backup = Path(artifact["backup"])
    if _path_has_symlink(target.parent):
        raise LauncherArtifactError(
            f"Launcher target parent uses a symbolic link: {target.parent}"
        )
    _validate_owned_directory(target.parent, uid=_current_uid())
    if bool(artifact["had_previous"]):
        if not _preflight_artifact(target, kind=str(artifact["kind"])):
            raise LauncherArtifactError(
                f"Managed launcher disappeared before replacement: {target}"
            )
        if backup.exists():
            raise LauncherArtifactError(
                f"Launcher backup path already exists: {backup}"
            )
        os.replace(target, backup)
        artifact["backed_up"] = True
        _write_manifest(manifest_path, payload)
    elif os.path.lexists(target):
        raise LauncherArtifactError(
            f"Launcher target appeared before installation: {target}"
        )
    os.replace(staged, target)
    artifact["installed"] = True
    _write_manifest(manifest_path, payload)


def _rollback_payload(
    payload: dict[str, Any],
    *,
    manifest_path: Path,
    remove_manifest: bool,
) -> None:
    for artifact in reversed(payload["artifacts"]):
        target = Path(artifact["target"])
        staged = Path(artifact["staged"])
        backup = Path(artifact["backup"])
        kind = str(artifact["kind"])
        target_present = os.path.lexists(target)
        if target_present and target.is_symlink():
            raise LauncherArtifactError(
                f"Rollback target changed to a symbolic link: {target}"
            )
        inferred_installed = (
            target_present
            and _artifact_is_managed(target, kind=kind)
            and (
                bool(artifact.get("installed"))
                or not staged.exists()
                or backup.exists()
            )
        )
        if inferred_installed:
            _remove_managed_artifact(target, kind=kind)
        if backup.exists():
            if os.path.lexists(target):
                raise LauncherArtifactError(
                    f"Rollback target changed after launcher installation: {target}"
                )
            os.replace(backup, target)
    backup_dir = Path(payload["backup_dir"])
    _remove_empty_directory(backup_dir)
    if remove_manifest and manifest_path.exists():
        manifest_path.unlink()


def _shim_text(kestrel_home: Path) -> str:
    executable = kestrel_home / ".venv" / "bin" / "kestrel"
    return (
        "#!/usr/bin/env bash\n"
        f"# {SHIM_MARKER}\n"
        "set -euo pipefail\n"
        f"export KESTREL_HOME={shlex.quote(str(kestrel_home))}\n"
        f"exec {shlex.quote(str(executable))} \"$@\"\n"
    )


def _build_macos_app(
    app_path: Path,
    *,
    shim_path: Path,
    log_path: Path,
) -> None:
    contents = app_path / "Contents"
    macos_dir = contents / "MacOS"
    resources_dir = contents / "Resources"
    macos_dir.mkdir(parents=True, mode=0o755)
    resources_dir.mkdir(parents=True, mode=0o755)
    plist = {
        "CFBundleDevelopmentRegion": "en",
        "CFBundleDisplayName": "Kestrel",
        "CFBundleExecutable": "Kestrel",
        "CFBundleIdentifier": "com.kestrel.local-launcher",
        "CFBundleInfoDictionaryVersion": "6.0",
        "CFBundleName": "Kestrel",
        "CFBundlePackageType": "APPL",
        "CFBundleShortVersionString": "1.0",
        "CFBundleVersion": "1",
        "LSMinimumSystemVersion": "12.0",
    }
    with (contents / "Info.plist").open("wb") as handle:
        plistlib.dump(plist, handle, sort_keys=True)
    marker = resources_dir / APP_MARKER_FILENAME
    marker.write_text(f"{APP_MARKER}\n", encoding="utf-8")
    marker.chmod(0o644)
    recovery = (
        f"Kestrel could not start. See {log_path}. "
        f"Run: {shim_path} doctor"
    )
    applescript = (
        'display alert "Kestrel could not start" message '
        f'"{_escape_applescript(recovery)}" as critical'
    )
    executable_text = (
        "#!/usr/bin/env bash\n"
        f"# {APP_MARKER}\n"
        "set -u\n"
        f"if ! {shlex.quote(str(shim_path))} open; then\n"
        f"  /usr/bin/osascript -e {shlex.quote(applescript)} "
        ">/dev/null 2>&1 || true\n"
        "  exit 1\n"
        "fi\n"
    )
    _write_executable(macos_dir / "Kestrel", executable_text)


def _codesign_app(
    app_path: Path,
    *,
    which: Callable[[str], str | None],
) -> str:
    codesign = which("codesign")
    if codesign is None:
        return "unavailable"
    try:
        subprocess.run(
            [codesign, "--force", "--deep", "--sign", "-", str(app_path)],
            check=True,
            capture_output=True,
            text=True,
        )
        subprocess.run(
            [codesign, "--verify", "--deep", "--strict", str(app_path)],
            check=True,
            capture_output=True,
            text=True,
        )
    except subprocess.CalledProcessError as exc:
        raise LauncherArtifactError(
            "Generated Kestrel.app failed ad-hoc signing verification"
        ) from exc
    return "verified"


def _preflight_artifact(path: Path | None, *, kind: str) -> bool:
    if path is None:
        return False
    if path.is_symlink():
        raise LauncherArtifactError(
            f"Refusing symbolic-link launcher target: {path}"
        )
    if not path.exists():
        return False
    if not _artifact_is_managed(path, kind=kind):
        raise LauncherArtifactError(
            f"Refusing to overwrite unrelated existing launcher: {path}"
        )
    return True


def _artifact_is_managed(path: Path, *, kind: str) -> bool:
    if kind == "shim":
        if not path.is_file():
            return False
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return False
        return SHIM_MARKER in text
    if kind == "app":
        if not path.is_dir():
            return False
        marker = path / "Contents" / "Resources" / APP_MARKER_FILENAME
        if marker.is_symlink() or not marker.is_file():
            return False
        try:
            return marker.read_text(encoding="utf-8").strip() == APP_MARKER
        except (OSError, UnicodeDecodeError):
            return False
    raise LauncherArtifactError(f"Unknown launcher artifact kind: {kind}")


def _remove_managed_artifact(path: Path, *, kind: str) -> None:
    if path.is_symlink() or not _artifact_is_managed(path, kind=kind):
        raise LauncherArtifactError(
            f"Refusing to remove changed or unrelated launcher artifact: {path}"
        )
    if kind == "shim":
        path.unlink()
        return
    shutil.rmtree(path)


def _validate_kestrel_home(path: Path, *, uid: int) -> Path:
    if not path.is_absolute():
        raise LauncherArtifactError("Kestrel home must be absolute")
    if _path_has_symlink(path):
        raise LauncherArtifactError("Kestrel home must not use symbolic links")
    resolved = path.resolve()
    _validate_owned_directory(resolved, uid=uid)
    executable = resolved / ".venv" / "bin" / "kestrel"
    if (
        executable.is_symlink()
        or not executable.is_file()
        or executable.stat().st_uid != uid
        or not os.access(executable, os.X_OK)
    ):
        raise LauncherArtifactError(
            f"Kestrel executable is unavailable or unsafe: {executable}"
        )
    return resolved


def _validated_user_home(path: Path, *, uid: int) -> Path:
    if not path.is_absolute():
        raise LauncherArtifactError("User home must be absolute")
    if _path_has_symlink(path):
        raise LauncherArtifactError("User home must not use symbolic links")
    resolved = path.resolve()
    _validate_owned_directory(resolved, uid=uid)
    return resolved


def _validate_bin_directory(
    path: Path,
    *,
    user_home: Path,
    uid: int,
) -> Path:
    if not path.is_absolute():
        raise LauncherArtifactError("Launcher bin directory must be absolute")
    if _path_has_symlink(path):
        raise LauncherArtifactError(
            f"Launcher bin directory must not use symbolic links: {path}"
        )
    resolved = path.resolve()
    unsafe_components = {
        component.lower() for component in resolved.parts
    }
    if (
        ".venv" in unsafe_components
        or "venv" in unsafe_components
        or any(component.lower().endswith(".app") for component in resolved.parts)
    ):
        raise LauncherArtifactError(
            f"Launcher bin directory is inside a transient runtime or app: {resolved}"
        )
    system_temp = Path(tempfile.gettempdir()).resolve()
    if (
        _is_relative_to(resolved, system_temp)
        and not _is_relative_to(resolved, user_home)
    ):
        raise LauncherArtifactError(
            f"Launcher bin directory is temporary: {resolved}"
        )
    _validate_owned_directory(resolved, uid=uid)
    if not os.access(resolved, os.W_OK | os.X_OK):
        raise LauncherArtifactError(
            f"Launcher bin directory is not user-writable: {resolved}"
        )
    return resolved


def _create_bin_directory(
    path: Path,
    *,
    user_home: Path,
    uid: int,
) -> None:
    if path.exists():
        return
    if not path.is_absolute():
        raise LauncherArtifactError("Launcher bin directory must be absolute")
    if _path_has_symlink(path):
        raise LauncherArtifactError(
            f"Launcher bin directory must not use symbolic links: {path}"
        )
    if not _is_relative_to(path, user_home):
        parent = _nearest_existing_parent(path)
        _validate_owned_directory(parent, uid=uid)
        if not os.access(parent, os.W_OK | os.X_OK):
            raise LauncherArtifactError(
                f"Cannot create launcher bin directory: {path}"
            )
    path.mkdir(parents=True, mode=0o700)


def _create_user_artifact_directory(
    path: Path,
    *,
    user_home: Path,
    uid: int,
) -> None:
    if not path.is_absolute() or not _is_relative_to(path, user_home):
        raise LauncherArtifactError(
            f"User launcher directory must remain inside the user home: {path}"
        )
    if _path_has_symlink(path):
        raise LauncherArtifactError(
            f"User launcher directory must not use symbolic links: {path}"
        )
    path.mkdir(parents=True, mode=0o700, exist_ok=True)
    _validate_owned_directory(path, uid=uid)


def _prepare_manifest_path(path: Path, *, uid: int) -> Path:
    if not path.is_absolute():
        raise LauncherArtifactError("Launcher manifest path must be absolute")
    if path.is_symlink():
        raise LauncherArtifactError(
            f"Launcher transaction manifest must not be a symbolic link: {path}"
        )
    if path.exists():
        raise LauncherArtifactError(
            f"Launcher transaction manifest already exists: {path}"
        )
    if _path_has_symlink(path.parent):
        raise LauncherArtifactError(
            f"Launcher transaction directory must not use symbolic links: {path.parent}"
        )
    path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    _validate_owned_directory(path.parent.resolve(), uid=uid)
    return path


def _create_private_directory(path: Path, *, uid: int) -> None:
    path.mkdir(mode=0o700)
    _validate_owned_directory(path, uid=uid)
    path.chmod(0o700)


def _write_executable(path: Path, text: str) -> None:
    descriptor = os.open(
        path,
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0),
        0o755,
    )
    try:
        payload = text.encode("utf-8")
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = -1
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        path.chmod(0o755)
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _write_manifest(path: Path, payload: Mapping[str, Any]) -> None:
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    descriptor = os.open(
        temporary,
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            descriptor = -1
            json.dump(payload, handle, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        temporary.chmod(0o600)
        os.replace(temporary, path)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if temporary.exists():
            temporary.unlink()


def _read_manifest(path: Path) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise LauncherArtifactError(
            f"Launcher transaction manifest is missing or unsafe: {path}"
        )
    metadata = path.stat()
    if (
        metadata.st_uid != _current_uid()
        or stat.S_IMODE(metadata.st_mode) != 0o600
        or metadata.st_size > _MAX_MANIFEST_BYTES
    ):
        raise LauncherArtifactError(
            f"Launcher transaction manifest has unsafe metadata: {path}"
        )
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise LauncherArtifactError(
            f"Launcher transaction manifest is invalid: {path}"
        ) from exc
    if (
        not isinstance(payload, dict)
        or payload.get("schema") != MANIFEST_SCHEMA
        or not isinstance(payload.get("artifacts"), list)
        or not isinstance(payload.get("backup_dir"), str)
    ):
        raise LauncherArtifactError(
            f"Launcher transaction manifest has an unknown schema: {path}"
        )
    return payload


def _validate_owned_directory(path: Path, *, uid: int) -> None:
    if path.is_symlink() or not path.is_dir():
        raise LauncherArtifactError(f"Expected a safe directory: {path}")
    metadata = path.stat()
    if metadata.st_uid != uid:
        raise LauncherArtifactError(
            f"Directory is not owned by the current user: {path}"
        )


def _remove_staging_directory(path: Path) -> None:
    if not path.exists():
        return
    if path.is_symlink() or not path.is_dir():
        raise LauncherArtifactError(f"Unsafe launcher staging path: {path}")
    shutil.rmtree(path)


def _remove_empty_directory(path: Path) -> None:
    if not path.exists():
        return
    if path.is_symlink() or not path.is_dir():
        raise LauncherArtifactError(f"Unsafe launcher backup path: {path}")
    try:
        path.rmdir()
    except OSError as exc:
        raise LauncherArtifactError(
            f"Launcher backup directory is not empty: {path}"
        ) from exc


def _nearest_existing_parent(path: Path) -> Path:
    candidate = path
    while not candidate.exists():
        if candidate.parent == candidate:
            raise LauncherArtifactError(
                f"Cannot find a safe parent for launcher directory: {path}"
            )
        candidate = candidate.parent
    return candidate


def _path_has_symlink(path: Path) -> bool:
    if not path.is_absolute():
        return True
    current = Path(path.anchor)
    for component in path.parts[1:]:
        current = current / component
        try:
            metadata = os.lstat(current)
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(metadata.st_mode):
            return True
    return False


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
    parser = argparse.ArgumentParser(
        description="Create or roll back Kestrel user launch artifacts."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    prepare = subparsers.add_parser("prepare")
    prepare.add_argument("--kestrel-home", type=Path, required=True)
    prepare.add_argument("--user-home", type=Path, required=True)
    prepare.add_argument("--manifest", type=Path, required=True)
    prepare.add_argument("--bin-dir", type=Path)
    prepare.add_argument("--platform", choices=("darwin", "linux"))
    for command in ("commit", "rollback"):
        action = subparsers.add_parser(command)
        action.add_argument("--manifest", type=Path, required=True)
    return parser


def run(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "prepare":
            result = prepare_launchers(
                kestrel_home=args.kestrel_home,
                user_home=args.user_home,
                manifest_path=args.manifest,
                bin_dir=args.bin_dir,
                platform=args.platform,
            )
        elif args.command == "commit":
            result = commit_launchers(args.manifest)
        else:
            result = rollback_launchers(args.manifest)
    except LauncherArtifactError as exc:
        sys.stderr.write(f"ERROR: {exc}\n")
        return 1
    sys.stdout.write(json.dumps(result, sort_keys=True) + "\n")
    return 0


def main() -> NoReturn:
    raise SystemExit(run())


if __name__ == "__main__":
    main()
