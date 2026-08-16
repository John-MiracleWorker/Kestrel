#!/usr/bin/env python3
"""Authenticate the recovery controller environment before importing it."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import runpy
import shutil
import stat
import subprocess
import sys
import tempfile
import zipfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

REPOSITORY = "John-MiracleWorker/Kestrel"
EXPECTED_PLATFORM = "linux"
EXPECTED_MACHINE = "x86_64"
EXPECTED_PYTHON_VERSION = "3.11.14"
EXPECTED_PYTHON_SHA256 = "dcd2d22a91c5adb37fa3f54a3a16d2ed7616b84931eb606c0fdfbca38395dab8"
EXPECTED_RUNTIME_INVENTORY_SHA256 = (
    "4180c03100ad4a58d4786eb10c3ba2cb3ac88dc5a30f7100410afef6b1e5ab2f"
)
EXPECTED_GH_SHA256 = "141507c337e8b202ad398550c3b73d72f5af92e86f71665214538a81efd4c409"
EXPECTED_GH_VERSION = "gh version 2.97.0 (2026-02-26)"
MAX_JSON_BYTES = 64 * 1024 * 1024
MAX_ARCHIVE_BYTES = 1024 * 1024 * 1024
MAX_ARCHIVE_MEMBERS = 100_000
MAX_ARCHIVE_MEMBER_BYTES = 512 * 1024 * 1024
MAX_ARCHIVE_TOTAL_BYTES = 2 * 1024 * 1024 * 1024
MAX_SITE_PACKAGES_FILES = 100_000
MAX_SITE_PACKAGES_FILE_BYTES = 512 * 1024 * 1024
MAX_SITE_PACKAGES_TOTAL_BYTES = 2 * 1024 * 1024 * 1024
SCRIPT_PATH = Path(__file__).resolve()
LOCAL_IMPORT_NAMES = (
    "bootstrap_recovery.py",
    "recovery_launcher.py",
    "release_candidate_manifest.py",
    "release_control_receipt.py",
    "release_promotion_transaction.py",
)


class BootstrapError(ValueError):
    """Stable fail-closed outer-bootstrap error."""


@dataclass(frozen=True)
class InitialBootstrapRequest:
    source_root: Path
    source_sha: str
    staging_run_id: int
    staging_artifact_id: int
    staging_artifact_digest: str
    pinned_gh: Path
    bootstrap_root: Path
    controller_arguments: tuple[str, ...]
    prepare_only: bool = False


@dataclass(frozen=True)
class ValidatedStaging:
    root: Path
    requirements: Path
    wheelhouse: Path
    receipt: dict[str, object]
    receipt_raw: bytes


def _canonical(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _sha256_bytes(raw: bytes) -> str:
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def _read_regular(path: Path, *, label: str, max_bytes: int) -> bytes:
    if path.is_symlink() or not path.is_file():
        raise BootstrapError(f"{label} is not a real regular file")
    size = path.stat().st_size
    if size <= 0 or size > max_bytes:
        raise BootstrapError(f"{label} size is invalid")
    raw = path.read_bytes()
    if len(raw) != size:
        raise BootstrapError(f"{label} changed while being read")
    return raw


def _canonical_object(raw: bytes, *, label: str) -> dict[str, object]:
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BootstrapError(f"{label} is not canonical JSON") from exc
    if type(value) is not dict or _canonical(value) != raw:
        raise BootstrapError(f"{label} is not one canonical JSON object")
    return value


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _clear_write_once_scratch(parent: Path, *, name: str) -> None:
    prefix = f".{name}.tmp-"
    for path in tuple(parent.iterdir()):
        if not path.name.startswith(prefix):
            continue
        if path.is_symlink() or not path.is_file():
            raise BootstrapError("bootstrap receipt write-once scratch is invalid")
        path.unlink()
    _fsync_directory(parent)


def _write_once(path: Path, raw: bytes, *, mode: int = 0o600) -> None:
    if type(raw) is not bytes or not raw:
        raise BootstrapError("bootstrap output must be nonempty exact bytes")
    if path.parent.is_symlink() or not path.parent.is_dir():
        raise BootstrapError("bootstrap output parent is invalid")
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.tmp-",
        dir=path.parent,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            os.fchmod(handle.fileno(), mode)
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError:
            raise BootstrapError(f"bootstrap output conflict: {path}") from None
        _fsync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def _validated_sha256(value: object, *, label: str) -> str:
    if type(value) is not str or re.fullmatch(r"sha256:[0-9a-f]{64}", value) is None:
        raise BootstrapError(f"{label} is not one SHA-256 digest")
    return value


def _validated_git_sha(value: object, *, label: str) -> str:
    if type(value) is not str or re.fullmatch(r"[0-9a-f]{40}", value) is None:
        raise BootstrapError(f"{label} is not one Git commit SHA")
    return value


def _validated_positive_integer(value: object, *, label: str) -> int:
    if type(value) is not int or value <= 0 or value > 9_007_199_254_740_991:
        raise BootstrapError(f"{label} is not one positive safe integer")
    return value


def site_packages_identity(environment_root: Path) -> tuple[int, int, str]:
    """Return the canonical identity of every installed importable file."""

    site_packages = environment_root / "lib/python3.11/site-packages"
    for root in (
        environment_root,
        environment_root / "lib",
        environment_root / "lib/python3.11",
        site_packages,
    ):
        if root.is_symlink() or not root.is_dir():
            raise BootstrapError("installed site-packages root is unsafe")
    records: list[dict[str, object]] = []
    total = 0
    for path in sorted(
        site_packages.rglob("*"),
        key=lambda item: item.relative_to(site_packages).as_posix().encode("utf-8"),
    ):
        if path.is_symlink():
            raise BootstrapError("installed site-packages contains a link")
        if path.is_dir():
            continue
        if not path.is_file():
            raise BootstrapError("installed site-packages contains a special file")
        size = path.stat().st_size
        total += size
        if (
            size < 0
            or size > MAX_SITE_PACKAGES_FILE_BYTES
            or total > MAX_SITE_PACKAGES_TOTAL_BYTES
            or len(records) >= MAX_SITE_PACKAGES_FILES
        ):
            raise BootstrapError("installed site-packages is too large")
        mode = 0o755 if path.stat().st_mode & 0o111 else 0o644
        records.append(
            {
                "mode": f"{mode:04o}",
                "path": path.relative_to(site_packages).as_posix(),
                "sha256": _sha256_path(path),
                "size_bytes": size,
            }
        )
    if not records:
        raise BootstrapError("installed site-packages is empty")
    return len(records), total, _sha256_bytes(_canonical(records))


def _safe_zip_member(name: str) -> tuple[str, ...]:
    if type(name) is not str or not name or name.startswith("/") or "\\" in name or "\x00" in name:
        raise BootstrapError("Actions artifact contains an unsafe member path")
    stripped = name[:-1] if name.endswith("/") else name
    parts = tuple(stripped.split("/"))
    if not parts or any(part in {"", ".", ".."} for part in parts):
        raise BootstrapError("Actions artifact contains an unsafe member path")
    return parts


def safe_extract_actions_artifact(archive: Path, output: Path) -> None:
    """Extract one bounded regular-file-only Actions artifact atomically."""

    if output.exists() or output.is_symlink():
        raise BootstrapError("Actions artifact output must be absent")
    if not output.parent.is_dir() or output.parent.is_symlink():
        raise BootstrapError("Actions artifact output parent is unsafe")
    if archive.is_symlink() or not archive.is_file():
        raise BootstrapError("Actions artifact archive is unsafe")
    staging = Path(tempfile.mkdtemp(prefix=f".{output.name}-", dir=output.parent))
    try:
        names: set[str] = set()
        total = 0
        with zipfile.ZipFile(archive, mode="r") as handle:
            infos = handle.infolist()
            if not infos or len(infos) > MAX_ARCHIVE_MEMBERS:
                raise BootstrapError("Actions artifact member count is invalid")
            for info in infos:
                parts = _safe_zip_member(info.filename)
                normalized = "/".join(parts)
                if normalized in names:
                    raise BootstrapError("Actions artifact has a duplicate member")
                names.add(normalized)
                if info.flag_bits & 0x1:
                    raise BootstrapError("Actions artifact has an encrypted member")
                raw_mode = info.external_attr >> 16
                kind = stat.S_IFMT(raw_mode)
                is_directory = info.is_dir()
                if is_directory:
                    if kind not in {0, stat.S_IFDIR}:
                        raise BootstrapError("Actions artifact has a special member")
                    target = staging.joinpath(*parts)
                    target.mkdir(parents=True, exist_ok=True, mode=0o755)
                    continue
                if kind not in {0, stat.S_IFREG}:
                    raise BootstrapError("Actions artifact has a special member")
                if (
                    info.file_size <= 0
                    or info.file_size > MAX_ARCHIVE_MEMBER_BYTES
                    or info.compress_size < 0
                ):
                    raise BootstrapError("Actions artifact member size is invalid")
                total += info.file_size
                if total > MAX_ARCHIVE_TOTAL_BYTES:
                    raise BootstrapError("Actions artifact is too large")
                target = staging.joinpath(*parts)
                target.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
                flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
                descriptor = os.open(target, flags, 0o600)
                written = 0
                with (
                    os.fdopen(descriptor, "wb") as destination,
                    handle.open(info, mode="r") as source,
                ):
                    while chunk := source.read(1024 * 1024):
                        written += len(chunk)
                        if written > info.file_size:
                            raise BootstrapError("Actions artifact member expanded beyond its size")
                        destination.write(chunk)
                if written != info.file_size:
                    raise BootstrapError("Actions artifact member size changed")
                target.chmod(0o600)
        os.replace(staging, output)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def _run_git(source_root: Path, *arguments: str) -> bytes:
    git_raw = shutil.which("git")
    if git_raw is None:
        raise BootstrapError("Git executable is unavailable")
    git = Path(git_raw).resolve(strict=True)
    if git.is_symlink() or not git.is_file() or not os.access(git, os.X_OK):
        raise BootstrapError("Git executable is unsafe")
    completed = subprocess.run(
        [str(git), "-C", str(source_root), *arguments],
        capture_output=True,
        check=False,
        timeout=30,
        env={"LANG": "C", "LC_ALL": "C"},
    )
    if completed.returncode != 0 or len(completed.stdout) > 64 * 1024 * 1024:
        raise BootstrapError("Git source inspection failed")
    return completed.stdout


def _source_tree_identity(source_root: Path) -> str:
    names = _run_git(source_root, "ls-files", "-z").split(b"\0")
    if names and names[-1] == b"":
        names.pop()
    if not names or names != sorted(names) or len(names) != len(set(names)):
        raise BootstrapError("tracked source inventory is invalid")
    records: list[dict[str, object]] = []
    for raw_name in names:
        try:
            name = raw_name.decode("utf-8", "strict")
        except UnicodeDecodeError as exc:
            raise BootstrapError("tracked source path is not UTF-8") from exc
        path = source_root.joinpath(*name.split("/"))
        if path.is_symlink() or not path.is_file():
            raise BootstrapError("tracked source contains a link or special file")
        records.append(
            {
                "path": name,
                "sha256": _sha256_path(path),
                "size_bytes": path.stat().st_size,
            }
        )
    return _sha256_bytes(_canonical(records))


def _require_source_identity(request: InitialBootstrapRequest) -> str:
    source_root = request.source_root
    expected_sha = _validated_git_sha(request.source_sha, label="source SHA")
    expected_script = source_root / "scripts" / SCRIPT_PATH.name
    if (
        not source_root.is_absolute()
        or source_root.is_symlink()
        or not source_root.is_dir()
        or source_root.resolve(strict=True) != source_root
        or SCRIPT_PATH != expected_script.resolve(strict=True)
    ):
        raise BootstrapError("bootstrap executing source root mismatch")
    top = _run_git(source_root, "rev-parse", "--show-toplevel").strip()
    head = _run_git(source_root, "rev-parse", "HEAD^{commit}").strip()
    status = _run_git(
        source_root,
        "status",
        "--porcelain=v1",
        "--untracked-files=all",
    )
    if top != os.fsencode(source_root) or head != expected_sha.encode("ascii") or status:
        raise BootstrapError("bootstrap source is not the exact clean commit")
    return _source_tree_identity(source_root)


def _runtime_inventory(root: Path) -> bytes:
    runtime = root / "runtime"
    candidates = [runtime / "bin/python3.11"]
    candidates.extend((runtime / "lib").rglob("*"))
    entries = sorted(
        (path for path in candidates if path.is_file() or path.is_symlink()),
        key=lambda path: path.relative_to(runtime).as_posix().encode("utf-8"),
    )
    lines: list[bytes] = []
    for path in entries:
        relative = path.relative_to(runtime).as_posix()
        if path.is_symlink():
            lines.append(f"link\t{relative}\t{os.readlink(path)}\n".encode())
            continue
        mode = format(stat.S_IMODE(path.stat().st_mode), "o")
        lines.append(
            (
                f"file\t{mode}\t{path.stat().st_size}\t"
                f"{_sha256_path(path).removeprefix('sha256:')}\t{relative}\n"
            ).encode()
        )
    if not lines:
        raise BootstrapError("bootstrap runtime inventory is empty")
    return b"".join(lines)


def _runtime_content_inventory(raw: bytes) -> bytes:
    """Drop archive modes while retaining the pinned path/content/link identity."""

    normalized: list[bytes] = []
    for line in raw.splitlines(keepends=False):
        fields = line.split(b"\t")
        if len(fields) == 5 and fields[0] == b"file":
            normalized.append(b"\t".join((fields[0], *fields[2:])) + b"\n")
        elif len(fields) == 3 and fields[0] == b"link":
            normalized.append(line + b"\n")
        else:
            raise BootstrapError("bootstrap runtime inventory record is invalid")
    if not normalized:
        raise BootstrapError("bootstrap runtime inventory is empty")
    return b"".join(normalized)


def _runtime_has_frozen_modes(root: Path) -> bool:
    runtime = root / "runtime"
    python = runtime / "bin/python3.11"
    if stat.S_IMODE(python.stat().st_mode) != 0o500:
        return False
    for path in (runtime / "lib").rglob("*"):
        if path.is_symlink():
            continue
        if path.is_dir():
            if stat.S_IMODE(path.stat().st_mode) != 0o555:
                return False
        elif path.is_file() and stat.S_IMODE(path.stat().st_mode) != 0o444:
            return False
    for path in (runtime / "bin", runtime / "lib"):
        if stat.S_IMODE(path.stat().st_mode) != 0o555:
            return False
    return True


def _require_preimport_runtime(request: InitialBootstrapRequest) -> None:
    if (
        sys.platform != EXPECTED_PLATFORM
        or platform.machine() != EXPECTED_MACHINE
        or platform.python_implementation() != "CPython"
        or platform.python_version() != EXPECTED_PYTHON_VERSION
        or sys.flags.isolated != 1
        or sys.flags.no_site != 1
        or sys.flags.dont_write_bytecode != 1
        or Path("/etc/ld.so.preload").exists()
    ):
        raise BootstrapError("bootstrap requires isolated Ubuntu 24.04 x86_64 CPython 3.11.14")
    os_release = Path("/etc/os-release").read_text(encoding="utf-8")
    fields = {
        line.partition("=")[0]: line.partition("=")[2].strip().strip('"')
        for line in os_release.splitlines()
        if line and not line.startswith("#") and "=" in line
    }
    runtime_python = request.bootstrap_root / "runtime/bin/python3.11"
    inventory_path = request.bootstrap_root / "runtime-tree.inventory"
    if (
        fields.get("ID") != "ubuntu"
        or fields.get("VERSION_ID") != "24.04"
        or Path(sys.executable).resolve(strict=True) != runtime_python.resolve(strict=True)
        or runtime_python.is_symlink()
        or _sha256_path(runtime_python) != "sha256:" + EXPECTED_PYTHON_SHA256
    ):
        raise BootstrapError("bootstrap Python identity mismatch")
    observed_inventory = _runtime_inventory(request.bootstrap_root)
    committed_inventory = _read_regular(
        inventory_path,
        label="bootstrap runtime inventory",
        max_bytes=64 * 1024 * 1024,
    )
    if (
        _runtime_content_inventory(observed_inventory)
        != _runtime_content_inventory(committed_inventory)
        or _sha256_bytes(committed_inventory) != "sha256:" + EXPECTED_RUNTIME_INVENTORY_SHA256
        or not _runtime_has_frozen_modes(request.bootstrap_root)
    ):
        raise BootstrapError("bootstrap Python runtime tree mismatch")


def _require_pinned_gh(path: Path) -> None:
    if (
        not path.is_absolute()
        or path.is_symlink()
        or not path.is_file()
        or not os.access(path, os.X_OK)
        or _sha256_path(path) != "sha256:" + EXPECTED_GH_SHA256
    ):
        raise BootstrapError("bootstrap GitHub CLI identity mismatch")
    completed = subprocess.run(
        [str(path), "--version"],
        capture_output=True,
        check=False,
        timeout=30,
        env={"GH_PROMPT_DISABLED": "1", "NO_COLOR": "1"},
    )
    if completed.returncode != 0 or completed.stdout.splitlines()[:1] != [
        EXPECTED_GH_VERSION.encode()
    ]:
        raise BootstrapError("bootstrap GitHub CLI version mismatch")


def _external_json(raw: bytes, *, label: str) -> object:
    if not raw or len(raw) > MAX_JSON_BYTES:
        raise BootstrapError(f"{label} size is invalid")
    try:
        return json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BootstrapError(f"{label} is invalid JSON") from exc


def _gh_api_command(path: Path, endpoint: str, *, paginate: bool = False) -> list[str]:
    command = [
        str(path),
        "api",
        "--method",
        "GET",
        "-H",
        "Accept: application/vnd.github+json",
        "-H",
        "X-GitHub-Api-Version: 2026-03-10",
    ]
    if paginate:
        command.extend(("--paginate", "--slurp"))
    command.append(endpoint)
    return command


def _staging_artifact_projection(
    value: Mapping[str, object],
) -> dict[str, object]:
    workflow_run = value.get("workflow_run")
    if type(workflow_run) is not dict:
        raise BootstrapError("staging artifact workflow identity is missing")
    size = _validated_positive_integer(value.get("size_in_bytes"), label="staging artifact size")
    return {
        "id": _validated_positive_integer(value.get("id"), label="staging artifact server ID"),
        "name": value.get("name"),
        "size_in_bytes": size,
        "expired": value.get("expired"),
        "digest": _validated_sha256(value.get("digest"), label="staging artifact server digest"),
        "workflow_run": {
            "id": workflow_run.get("id"),
            "repository_id": workflow_run.get("repository_id"),
            "head_repository_id": workflow_run.get("head_repository_id"),
            "head_branch": workflow_run.get("head_branch"),
            "head_sha": workflow_run.get("head_sha"),
        },
    }


def _validate_staging_server_evidence(
    request: InitialBootstrapRequest,
    *,
    run_raw: bytes,
    pages_raw: bytes,
    direct_raw: bytes,
) -> dict[str, object]:
    run_id = _validated_positive_integer(request.staging_run_id, label="staging workflow run ID")
    artifact_id = _validated_positive_integer(
        request.staging_artifact_id, label="staging artifact ID"
    )
    source_sha = _validated_git_sha(request.source_sha, label="staging source SHA")
    expected_digest = _validated_sha256(
        request.staging_artifact_digest,
        label="staging artifact API digest",
    )
    run_value = _external_json(run_raw, label="staging workflow run")
    pages_value = _external_json(pages_raw, label="staging artifact pages")
    direct_value = _external_json(direct_raw, label="staging artifact metadata")
    if type(run_value) is not dict or type(direct_value) is not dict:
        raise BootstrapError("staging artifact metadata shape is invalid")
    repository = run_value.get("repository")
    if (
        type(repository) is not dict
        or run_value.get("id") != run_id
        or run_value.get("path") != ".github/workflows/recovery-dependency-staging.yml"
        or run_value.get("event") != "workflow_dispatch"
        or run_value.get("run_attempt") != 1
        or run_value.get("head_sha") != source_sha
        or run_value.get("status") != "completed"
        or run_value.get("conclusion") != "success"
        or repository.get("full_name") != REPOSITORY
    ):
        raise BootstrapError("staging workflow run identity mismatch")
    if type(pages_value) is not list or not pages_value:
        raise BootstrapError("staging artifact page shape is invalid")
    artifacts: list[dict[str, object]] = []
    for page in pages_value:
        if type(page) is not dict or type(page.get("artifacts")) is not list:
            raise BootstrapError("staging artifact page shape is invalid")
        for item in page["artifacts"]:
            if type(item) is not dict:
                raise BootstrapError("staging artifact item shape is invalid")
            artifacts.append(item)
    expected_name = f"kestrel-recovery-dependencies-{source_sha}"
    matches = [item for item in artifacts if item.get("name") == expected_name]
    if len(matches) != 1:
        raise BootstrapError("staging artifact name cardinality mismatch")
    listed_projection = _staging_artifact_projection(matches[0])
    direct_projection = _staging_artifact_projection(direct_value)
    workflow = listed_projection["workflow_run"]
    assert type(workflow) is dict
    if (
        listed_projection != direct_projection
        or listed_projection["id"] != artifact_id
        or listed_projection["name"] != expected_name
        or listed_projection["expired"] is not False
        or listed_projection["digest"] != expected_digest
        or workflow.get("id") != run_id
        or workflow.get("head_sha") != source_sha
        or workflow.get("head_branch") != "main"
        or workflow.get("repository_id") != repository.get("id")
        or workflow.get("head_repository_id") != repository.get("id")
    ):
        raise BootstrapError("staging artifact metadata identity mismatch")
    return listed_projection


def _directory_identity(root: Path, *, label: str) -> bytes:
    if root.is_symlink() or not root.is_dir():
        raise BootstrapError(f"{label} root is unsafe")
    records: list[dict[str, object]] = []
    total = 0
    for path in sorted(
        root.rglob("*"),
        key=lambda item: item.relative_to(root).as_posix().encode("utf-8"),
    ):
        relative = path.relative_to(root).as_posix()
        if path.is_symlink():
            raise BootstrapError(f"{label} contains a link")
        if path.is_dir():
            records.append({"kind": "directory", "path": relative})
            continue
        if not path.is_file():
            raise BootstrapError(f"{label} contains a special file")
        size = path.stat().st_size
        total += size
        if (
            size <= 0
            or size > MAX_ARCHIVE_MEMBER_BYTES
            or total > MAX_ARCHIVE_TOTAL_BYTES
            or len(records) >= MAX_ARCHIVE_MEMBERS
        ):
            raise BootstrapError(f"{label} inventory is invalid")
        records.append(
            {
                "kind": "file",
                "path": relative,
                "sha256": _sha256_path(path),
                "size_bytes": size,
            }
        )
    if not records:
        raise BootstrapError(f"{label} is empty")
    return _canonical(records)


def _load_acquired_staging_artifact(
    request: InitialBootstrapRequest,
    *,
    output_root: Path,
) -> Path:
    if output_root.is_symlink() or not output_root.is_dir():
        raise BootstrapError("existing staging artifact output is unsafe")
    if {entry.name for entry in output_root.iterdir()} != {
        "acquisition-receipt.json",
        "artifact.zip",
        "contents",
        "evidence",
    }:
        raise BootstrapError("existing staging artifact inventory is invalid")
    evidence = output_root / "evidence"
    if (
        evidence.is_symlink()
        or not evidence.is_dir()
        or {entry.name for entry in evidence.iterdir()}
        != {
            "artifact-metadata.json",
            "artifact-pages.json",
            "workflow-run.json",
        }
    ):
        raise BootstrapError("existing staging evidence inventory is invalid")
    run_raw = _read_regular(
        evidence / "workflow-run.json",
        label="existing staging workflow run",
        max_bytes=MAX_JSON_BYTES,
    )
    pages_raw = _read_regular(
        evidence / "artifact-pages.json",
        label="existing staging artifact pages",
        max_bytes=MAX_JSON_BYTES,
    )
    direct_raw = _read_regular(
        evidence / "artifact-metadata.json",
        label="existing staging artifact metadata",
        max_bytes=MAX_JSON_BYTES,
    )
    projection = _validate_staging_server_evidence(
        request,
        run_raw=run_raw,
        pages_raw=pages_raw,
        direct_raw=direct_raw,
    )
    receipt = _canonical_object(
        _read_regular(
            output_root / "acquisition-receipt.json",
            label="existing staging acquisition receipt",
            max_bytes=MAX_JSON_BYTES,
        ),
        label="existing staging acquisition receipt",
    )
    expected_receipt = {
        "schema": "kestrel.recovery_controller_staging_acquisition.v1",
        "run_id": request.staging_run_id,
        "artifact_id": request.staging_artifact_id,
        "artifact_digest": request.staging_artifact_digest,
        "source_sha": request.source_sha,
        "workflow_path": ".github/workflows/recovery-dependency-staging.yml",
        "validation_status": "validated",
    }
    archive = output_root / "artifact.zip"
    if (
        receipt != expected_receipt
        or archive.is_symlink()
        or not archive.is_file()
        or archive.stat().st_size != projection["size_in_bytes"]
        or _sha256_path(archive) != request.staging_artifact_digest
    ):
        raise BootstrapError("existing staging artifact identity mismatch")
    comparison_root = Path(
        tempfile.mkdtemp(prefix=f".{output_root.name}-replay-", dir=output_root.parent)
    )
    try:
        extracted = comparison_root / "contents"
        safe_extract_actions_artifact(archive, extracted)
        if _directory_identity(extracted, label="replayed staging artifact") != _directory_identity(
            output_root / "contents", label="existing staging artifact"
        ):
            raise BootstrapError("existing staging artifact contents changed")
    finally:
        shutil.rmtree(comparison_root, ignore_errors=True)
    return output_root / "contents"


def acquire_staging_artifact(
    request: InitialBootstrapRequest,
    *,
    token: bytes,
    output_root: Path,
    runner: Callable[..., subprocess.CompletedProcess[bytes]] = subprocess.run,
) -> Path:
    """Acquire the exact completed staging artifact by immutable server ID."""

    if (
        type(token) is not bytes
        or not token
        or len(token) > 4096
        or any(byte < 0x21 or byte > 0x7E for byte in token)
    ):
        raise BootstrapError("staging artifact credential bytes are invalid")
    run_id = _validated_positive_integer(request.staging_run_id, label="staging workflow run ID")
    artifact_id = _validated_positive_integer(
        request.staging_artifact_id, label="staging artifact ID"
    )
    source_sha = _validated_git_sha(request.source_sha, label="staging source SHA")
    expected_digest = _validated_sha256(
        request.staging_artifact_digest,
        label="staging artifact API digest",
    )
    if output_root.exists() or output_root.is_symlink():
        return _load_acquired_staging_artifact(request, output_root=output_root)
    if not output_root.parent.is_dir() or output_root.parent.is_symlink():
        raise BootstrapError("staging artifact output parent is unsafe")
    staging = Path(tempfile.mkdtemp(prefix=f".{output_root.name}-", dir=output_root.parent))
    environment = {
        "GH_TOKEN": token.decode("ascii"),
        "GH_PROMPT_DISABLED": "1",
        "NO_COLOR": "1",
    }

    def metadata(endpoint: str, *, paginate: bool = False) -> bytes:
        completed = runner(
            _gh_api_command(request.pinned_gh, endpoint, paginate=paginate),
            capture_output=True,
            check=False,
            timeout=30,
            env=environment,
        )
        if (
            completed.returncode != 0
            or not completed.stdout
            or len(completed.stdout) > MAX_JSON_BYTES
        ):
            raise BootstrapError("staging artifact metadata transport failed")
        return completed.stdout

    try:
        run_raw = metadata(f"/repos/{REPOSITORY}/actions/runs/{run_id}")
        pages_raw = metadata(
            f"/repos/{REPOSITORY}/actions/runs/{run_id}/artifacts?per_page=100",
            paginate=True,
        )
        direct_raw = metadata(f"/repos/{REPOSITORY}/actions/artifacts/{artifact_id}")
        listed_projection = _validate_staging_server_evidence(
            request,
            run_raw=run_raw,
            pages_raw=pages_raw,
            direct_raw=direct_raw,
        )
        evidence = staging / "evidence"
        evidence.mkdir(mode=0o700)
        for name, raw in (
            ("workflow-run.json", run_raw),
            ("artifact-pages.json", pages_raw),
            ("artifact-metadata.json", direct_raw),
        ):
            _write_once(evidence / name, raw)
        archive_path = staging / "artifact.zip"
        descriptor = os.open(
            archive_path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        try:
            with os.fdopen(descriptor, "wb") as destination:
                descriptor = -1
                completed = runner(
                    _gh_api_command(
                        request.pinned_gh,
                        f"/repos/{REPOSITORY}/actions/artifacts/{artifact_id}/zip",
                    ),
                    stdout=destination,
                    stderr=subprocess.PIPE,
                    check=False,
                    timeout=300,
                    env=environment,
                )
                destination.flush()
                os.fsync(destination.fileno())
        finally:
            if descriptor >= 0:
                os.close(descriptor)
        if (
            completed.returncode != 0
            or archive_path.stat().st_size != listed_projection["size_in_bytes"]
            or _sha256_path(archive_path) != expected_digest
        ):
            raise BootstrapError("staging artifact archive identity mismatch")
        contents = staging / "contents"
        safe_extract_actions_artifact(archive_path, contents)
        receipt = {
            "schema": "kestrel.recovery_controller_staging_acquisition.v1",
            "run_id": run_id,
            "artifact_id": artifact_id,
            "artifact_digest": expected_digest,
            "source_sha": source_sha,
            "workflow_path": ".github/workflows/recovery-dependency-staging.yml",
            "validation_status": "validated",
        }
        _write_once(staging / "acquisition-receipt.json", _canonical(receipt))
        os.replace(staging, output_root)
        staging = output_root
        return output_root / "contents"
    except BaseException:
        if staging.exists() and staging != output_root:
            shutil.rmtree(staging, ignore_errors=True)
        raise


def validate_staging_artifact(
    root: Path,
    *,
    source_root: Path,
    source_sha: str,
) -> ValidatedStaging:
    """Validate every staged byte needed before allowing offline pip."""

    checked_sha = _validated_git_sha(source_sha, label="staging source SHA")
    recovery = root / "recovery"
    if (
        root.is_symlink()
        or not root.is_dir()
        or recovery.is_symlink()
        or not recovery.is_dir()
        or {entry.name for entry in root.iterdir()} != {"recovery", "recovery-smoke-report.json"}
    ):
        raise BootstrapError("staging artifact root inventory is invalid")
    receipt_path = recovery / "dependency-staging-receipt.json"
    receipt_raw = _read_regular(
        receipt_path,
        label="staging dependency receipt",
        max_bytes=MAX_JSON_BYTES,
    )
    receipt = _canonical_object(receipt_raw, label="staging dependency receipt")
    if set(receipt) != {
        "schema",
        "inputs",
        "outputs",
        "provenance",
        "confidence",
        "validation_status",
        "receipt_digest",
    }:
        raise BootstrapError("staging dependency receipt shape is invalid")
    projection = dict(receipt)
    claimed_receipt_digest = projection.pop("receipt_digest")
    inputs = receipt.get("inputs")
    outputs = receipt.get("outputs")
    provenance = receipt.get("provenance")
    if (
        type(inputs) is not dict
        or type(outputs) is not dict
        or type(provenance) is not dict
        or receipt.get("schema") != "kestrel.recovery_dependency_staging.v1"
        or receipt.get("confidence") != 1
        or receipt.get("validation_status") != "validated"
        or claimed_receipt_digest != _sha256_bytes(_canonical(projection))
        or inputs.get("source_sha") != checked_sha
        or inputs.get("python_version") != EXPECTED_PYTHON_VERSION
        or inputs.get("python_abi") != "cp311"
        or inputs.get("wheel_platform") != "manylinux2014_x86_64"
        or provenance
        != {
            "method": "checksum-pinned-recovery-dependency-staging",
            "producer": "scripts/stage_recovery_dependencies.py",
            "provider": "github.com+archive.ubuntu.com+pypi.org",
        }
    ):
        raise BootstrapError("staging dependency receipt identity mismatch")

    requirements = recovery / "requirements.txt"
    requirements_raw = _read_regular(
        requirements,
        label="staging requirements lock",
        max_bytes=1024 * 1024,
    )
    source_requirements = _read_regular(
        source_root / "config/recovery-requirements.txt",
        label="source recovery requirements lock",
        max_bytes=1024 * 1024,
    )
    logical_requirements = [
        line.strip()
        for line in requirements_raw.decode("utf-8", "strict").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    if (
        requirements_raw != source_requirements
        or inputs.get("requirements_sha256") != _sha256_bytes(requirements_raw)
        or not logical_requirements
        or not any("--hash=sha256:" in line for line in logical_requirements)
    ):
        raise BootstrapError("staging requirements lock identity mismatch")

    wheel_manifest_path = recovery / "wheelhouse-manifest.json"
    wheel_manifest_raw = _read_regular(
        wheel_manifest_path,
        label="staging wheelhouse manifest",
        max_bytes=MAX_JSON_BYTES,
    )
    wheel_manifest = _canonical_object(wheel_manifest_raw, label="staging wheelhouse manifest")
    wheels = wheel_manifest.get("wheels")
    wheelhouse = recovery / "wheelhouse"
    if (
        set(wheel_manifest) != {"schema", "wheels"}
        or wheel_manifest.get("schema") != "kestrel.recovery_wheelhouse.v1"
        or type(wheels) is not list
        or not wheels
        or wheelhouse.is_symlink()
        or not wheelhouse.is_dir()
    ):
        raise BootstrapError("staging wheelhouse manifest shape is invalid")
    wheel_names: list[str] = []
    for raw_wheel in wheels:
        if type(raw_wheel) is not dict or set(raw_wheel) != {
            "filename",
            "sha256",
            "size_bytes",
        }:
            raise BootstrapError("staging wheel identity shape is invalid")
        filename = raw_wheel.get("filename")
        if (
            type(filename) is not str
            or not filename.endswith(".whl")
            or "/" in filename
            or "\\" in filename
            or filename in {"", ".", ".."}
        ):
            raise BootstrapError("staging wheel filename is unsafe")
        path = wheelhouse / filename
        size = _validated_positive_integer(raw_wheel.get("size_bytes"), label="staging wheel size")
        digest = _validated_sha256(raw_wheel.get("sha256"), label="staging wheel digest")
        if (
            path.is_symlink()
            or not path.is_file()
            or path.stat().st_size != size
            or _sha256_path(path) != digest
        ):
            raise BootstrapError("staging wheel byte identity mismatch")
        wheel_names.append(filename)
    if (
        wheel_names != sorted(wheel_names)
        or len(wheel_names) != len(set(wheel_names))
        or {entry.name for entry in wheelhouse.iterdir()} != set(wheel_names)
        or outputs.get("wheel_count") != len(wheel_names)
        or outputs.get("wheelhouse_manifest_sha256") != _sha256_bytes(wheel_manifest_raw)
    ):
        raise BootstrapError("staging wheelhouse inventory mismatch")

    runtime_manifest_path = recovery / "runtime-manifest.json"
    runtime_manifest_raw = _read_regular(
        runtime_manifest_path,
        label="staging runtime manifest",
        max_bytes=MAX_JSON_BYTES,
    )
    runtime_manifest = _canonical_object(runtime_manifest_raw, label="staging runtime manifest")
    runtime_files = runtime_manifest.get("files")
    if (
        runtime_manifest.get("schema") != "kestrel.recovery_runtime.v1"
        or runtime_manifest.get("platform") != "ubuntu-24.04-x86_64"
        or runtime_manifest.get("python_version") != EXPECTED_PYTHON_VERSION
        or type(runtime_files) is not list
        or not runtime_files
    ):
        raise BootstrapError("staging runtime manifest shape is invalid")
    runtime_names: list[str] = []
    for raw_file in runtime_files:
        if type(raw_file) is not dict:
            raise BootstrapError("staging runtime file shape is invalid")
        asset_path = raw_file.get("asset_path")
        if (
            type(asset_path) is not str
            or not asset_path.startswith("recovery/runtime/")
            or "\\" in asset_path
            or any(part in {"", ".", ".."} for part in asset_path.split("/"))
        ):
            raise BootstrapError("staging runtime asset path is unsafe")
        path = root.joinpath(*asset_path.split("/"))
        size = _validated_positive_integer(
            raw_file.get("size_bytes"), label="staging runtime file size"
        )
        digest = _validated_sha256(raw_file.get("sha256"), label="staging runtime file digest")
        if (
            path.is_symlink()
            or not path.is_file()
            or path.stat().st_size != size
            or _sha256_path(path) != digest
        ):
            raise BootstrapError("staging runtime file identity mismatch")
        runtime_names.append(asset_path)
    if (
        runtime_names != sorted(runtime_names)
        or len(runtime_names) != len(set(runtime_names))
        or outputs.get("runtime_file_count") != len(runtime_names)
        or outputs.get("runtime_manifest_sha256") != _sha256_bytes(runtime_manifest_raw)
    ):
        raise BootstrapError("staging runtime inventory mismatch")

    python_manifest_path = recovery / "python-runtime-manifest.json"
    python_manifest_raw = _read_regular(
        python_manifest_path,
        label="staging Python runtime manifest",
        max_bytes=MAX_JSON_BYTES,
    )
    python_manifest = _canonical_object(
        python_manifest_raw, label="staging Python runtime manifest"
    )
    python_archive = recovery / "python-runtime.tar.gz"
    python_archive_raw = _read_regular(
        python_archive,
        label="staging Python runtime archive",
        max_bytes=MAX_ARCHIVE_BYTES,
    )
    if (
        python_manifest.get("schema") != "kestrel.recovery_python_runtime.v1"
        or python_manifest.get("platform") != "ubuntu-24.04-x86_64"
        or python_manifest.get("python_version") != EXPECTED_PYTHON_VERSION
        or python_manifest.get("python_abi") != "cp311"
        or python_manifest.get("runtime_archive_path") != "recovery/python-runtime.tar.gz"
        or python_manifest.get("runtime_archive_size_bytes") != len(python_archive_raw)
        or python_manifest.get("runtime_archive_sha256") != _sha256_bytes(python_archive_raw)
        or outputs.get("python_runtime_manifest_sha256") != _sha256_bytes(python_manifest_raw)
        or outputs.get("python_runtime_archive_sha256") != _sha256_bytes(python_archive_raw)
    ):
        raise BootstrapError("staging Python runtime identity mismatch")

    bwrap = recovery / "bin/bwrap"
    bwrap_raw = _read_regular(bwrap, label="staging bubblewrap", max_bytes=256 * 1024 * 1024)
    smoke = _canonical_object(
        _read_regular(
            root / "recovery-smoke-report.json",
            label="staging recovery smoke report",
            max_bytes=MAX_JSON_BYTES,
        ),
        label="staging recovery smoke report",
    )
    if (
        outputs.get("bubblewrap_sha256") != _sha256_bytes(bwrap_raw)
        or smoke.get("schema") != "kestrel.recovery_capsule_smoke.v1"
        or smoke.get("source_sha") != checked_sha
        or smoke.get("validation_status") != "validated"
    ):
        raise BootstrapError("staging executable or smoke identity mismatch")

    expected_files = {
        "bin/bwrap",
        "dependency-staging-receipt.json",
        "python-runtime-manifest.json",
        "python-runtime.tar.gz",
        "requirements.txt",
        "runtime-manifest.json",
        "wheelhouse-manifest.json",
        *(f"wheelhouse/{name}" for name in wheel_names),
        *(Path(name).relative_to("recovery").as_posix() for name in runtime_names),
    }
    observed_files = {
        path.relative_to(recovery).as_posix()
        for path in recovery.rglob("*")
        if path.is_file() and not path.is_symlink()
    }
    if observed_files != expected_files:
        raise BootstrapError("staging artifact file inventory is not exact")
    return ValidatedStaging(
        root=recovery,
        requirements=requirements,
        wheelhouse=wheelhouse,
        receipt=receipt,
        receipt_raw=receipt_raw,
    )


def _controller_environment_projection(
    environment_root: Path,
    *,
    base_python: Path,
    reported_root: Path | None = None,
) -> dict[str, object]:
    if (
        environment_root.is_symlink()
        or not environment_root.is_dir()
        or base_python.is_symlink()
        or not base_python.is_file()
        or not os.access(base_python, os.X_OK)
    ):
        raise BootstrapError("controller environment identity is unsafe")
    base_digest = _sha256_path(base_python)
    environment_python = environment_root / "bin/python"
    site_packages = environment_root / "lib/python3.11/site-packages"
    if (
        environment_python.is_symlink()
        or not environment_python.is_file()
        or not os.access(environment_python, os.X_OK)
        or _sha256_path(environment_python) != base_digest
        or stat.S_IMODE(environment_python.stat().st_mode) != 0o500
    ):
        raise BootstrapError("controller environment Python identity mismatch")
    count, total, tree_digest = site_packages_identity(environment_root)
    for path in site_packages.rglob("*"):
        if path.is_dir() and stat.S_IMODE(path.stat().st_mode) != 0o555:
            raise BootstrapError("controller environment directory is mutable")
        if path.is_file() and stat.S_IMODE(path.stat().st_mode) not in {0o444, 0o555}:
            raise BootstrapError("controller environment file is mutable")
    if stat.S_IMODE(site_packages.stat().st_mode) != 0o555:
        raise BootstrapError("controller environment import root is mutable")
    bound_root = environment_root if reported_root is None else reported_root
    return {
        "root": str(bound_root),
        "python_path": str(bound_root / "bin/python"),
        "python_sha256": base_digest,
        "site_packages_path": str(bound_root / "lib/python3.11/site-packages"),
        "site_packages_file_count": count,
        "site_packages_total_size_bytes": total,
        "site_packages_tree_sha256": tree_digest,
    }


def _load_controller_environment(
    staging: ValidatedStaging,
    *,
    environment_root: Path,
    base_python: Path,
) -> dict[str, object]:
    projection = _controller_environment_projection(
        environment_root,
        base_python=base_python,
    )
    receipt_path = environment_root / "controller-environment-receipt.json"
    receipt_raw = _read_regular(
        receipt_path,
        label="controller environment build receipt",
        max_bytes=MAX_JSON_BYTES,
    )
    receipt = _canonical_object(
        receipt_raw,
        label="controller environment build receipt",
    )
    if (
        stat.S_IMODE(receipt_path.stat().st_mode) != 0o400
        or set(receipt)
        != {
            "schema",
            "staging_dependency_receipt_digest",
            "environment",
            "validation_status",
        }
        or receipt.get("schema") != "kestrel.recovery_controller_environment.v1"
        or receipt.get("staging_dependency_receipt_digest") != _sha256_bytes(staging.receipt_raw)
        or receipt.get("environment") != projection
        or receipt.get("validation_status") != "validated"
    ):
        raise BootstrapError("controller environment build receipt mismatch")
    return projection


def build_controller_environment(
    staging: ValidatedStaging,
    *,
    environment_root: Path,
    base_python: Path,
    runner: Callable[..., subprocess.CompletedProcess[bytes]] = subprocess.run,
) -> dict[str, object]:
    """Install the hash-locked wheel closure offline and freeze its import tree."""

    if base_python.is_symlink() or not base_python.is_file() or not os.access(base_python, os.X_OK):
        raise BootstrapError("controller environment base Python is unsafe")
    if environment_root.exists() or environment_root.is_symlink():
        return _load_controller_environment(
            staging,
            environment_root=environment_root,
            base_python=base_python,
        )
    if not environment_root.parent.is_dir() or environment_root.parent.is_symlink():
        raise BootstrapError("controller environment parent is unsafe")
    base_digest = _sha256_path(base_python)
    temporary = Path(
        tempfile.mkdtemp(prefix=f".{environment_root.name}-", dir=environment_root.parent)
    )
    built = temporary / "environment"
    clean_environment = {
        "LANG": "C.UTF-8",
        "LD_LIBRARY_PATH": str(base_python.parent.parent / "lib"),
        "LC_ALL": "C.UTF-8",
        "PIP_CONFIG_FILE": os.devnull,
        "PIP_DISABLE_PIP_VERSION_CHECK": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
    }

    def run(command: list[str], *, timeout: int) -> None:
        completed = runner(
            command,
            capture_output=True,
            check=False,
            timeout=timeout,
            env=clean_environment,
        )
        if (
            completed.returncode != 0
            or len(completed.stdout) > MAX_JSON_BYTES
            or len(completed.stderr) > MAX_JSON_BYTES
        ):
            raise BootstrapError("controller environment command failed")

    try:
        run(
            [
                str(base_python),
                "-I",
                "-B",
                "-m",
                "venv",
                "--copies",
                str(built),
            ],
            timeout=300,
        )
        environment_python = built / "bin/python"
        if (
            environment_python.is_symlink()
            or not environment_python.is_file()
            or _sha256_path(environment_python) != base_digest
        ):
            raise BootstrapError("controller environment Python identity mismatch")
        run(
            [
                str(environment_python),
                "-I",
                "-B",
                "-m",
                "pip",
                "--isolated",
                "install",
                "--no-compile",
                "--no-index",
                "--find-links",
                str(staging.wheelhouse),
                "--require-hashes",
                "--only-binary=:all:",
                "--requirement",
                str(staging.requirements),
            ],
            timeout=900,
        )
        run(
            [
                str(environment_python),
                "-I",
                "-B",
                "-m",
                "pip",
                "--isolated",
                "check",
            ],
            timeout=300,
        )
        site_packages_identity(built)
        site_packages = built / "lib/python3.11/site-packages"
        for path in sorted(
            site_packages.rglob("*"),
            key=lambda item: len(item.parts),
            reverse=True,
        ):
            if path.is_file():
                path.chmod(0o555 if path.stat().st_mode & 0o111 else 0o444)
            elif path.is_dir():
                path.chmod(0o555)
        site_packages.chmod(0o555)
        environment_python.chmod(0o500)
        projection = _controller_environment_projection(
            built,
            base_python=base_python,
            reported_root=environment_root,
        )
        environment_receipt = {
            "schema": "kestrel.recovery_controller_environment.v1",
            "staging_dependency_receipt_digest": _sha256_bytes(staging.receipt_raw),
            "environment": projection,
            "validation_status": "validated",
        }
        _write_once(
            built / "controller-environment-receipt.json",
            _canonical(environment_receipt),
            mode=0o400,
        )
        os.replace(built, environment_root)
        shutil.rmtree(temporary)
        return _load_controller_environment(
            staging,
            environment_root=environment_root,
            base_python=base_python,
        )
    except BaseException:
        shutil.rmtree(temporary, ignore_errors=True)
        raise


def authorize_inner_gate(
    *,
    receipt_path: Path,
    source_root: Path,
    controller_arguments: Sequence[str],
    executing_script: Path = SCRIPT_PATH,
    executing_python: Path | None = None,
    require_source_git: bool = True,
    require_external_bindings: bool = True,
) -> tuple[Path, Path]:
    """Reauthenticate all import authority before adding site-packages."""

    receipt = _canonical_object(
        _read_regular(
            receipt_path,
            label="controller bootstrap receipt",
            max_bytes=MAX_JSON_BYTES,
        ),
        label="controller bootstrap receipt",
    )
    if (
        set(receipt)
        != {
            "schema",
            "source",
            "runtime",
            "environment",
            "pinned_gh",
            "staging_artifact",
            "controller_arguments_digest",
            "validation_status",
        }
        or receipt.get("schema") != "kestrel.recovery_controller_bootstrap.v1"
        or receipt.get("validation_status") != "validated"
    ):
        raise BootstrapError("controller bootstrap receipt shape is invalid")
    source = receipt.get("source")
    runtime = receipt.get("runtime")
    environment = receipt.get("environment")
    pinned_gh = receipt.get("pinned_gh")
    staging_artifact = receipt.get("staging_artifact")
    if (
        type(source) is not dict
        or type(runtime) is not dict
        or type(environment) is not dict
        or type(pinned_gh) is not dict
        or type(staging_artifact) is not dict
    ):
        raise BootstrapError("controller bootstrap receipt sections are invalid")
    controller = source_root / "scripts/recovery_capsule_controller.py"
    site_packages = Path(str(environment.get("site_packages_path")))
    environment_root = Path(str(environment.get("root")))
    python = Path(sys.executable) if executing_python is None else executing_python
    expected_imports = [
        {
            "path": str(source_root / "scripts" / name),
            "sha256": _sha256_path(source_root / "scripts" / name),
        }
        for name in LOCAL_IMPORT_NAMES
    ]
    if (
        set(source)
        != {
            "root",
            "sha",
            "tree_sha256",
            "bootstrap_path",
            "bootstrap_sha256",
            "controller_path",
            "controller_sha256",
            "local_imports",
        }
        or set(runtime)
        != {
            "bootstrap_python_path",
            "bootstrap_python_sha256",
            "bootstrap_runtime_tree_sha256",
            "python_path",
            "python_sha256",
        }
        or set(environment)
        != {
            "root",
            "python_path",
            "python_sha256",
            "site_packages_path",
            "site_packages_file_count",
            "site_packages_total_size_bytes",
            "site_packages_tree_sha256",
        }
        or set(pinned_gh) != {"path", "sha256", "version"}
        or set(staging_artifact)
        != {
            "root",
            "run_id",
            "artifact_id",
            "artifact_digest",
            "acquisition_receipt_digest",
            "dependency_receipt_digest",
        }
        or source.get("root") != str(source_root)
        or source.get("sha")
        != _validated_git_sha(source.get("sha"), label="bootstrap receipt source SHA")
        or source.get("tree_sha256")
        != _validated_sha256(source.get("tree_sha256"), label="bootstrap receipt source tree")
        or source.get("bootstrap_path") != str(executing_script)
        or source.get("controller_path") != str(controller)
        or _sha256_path(executing_script) != source.get("bootstrap_sha256")
        or _sha256_path(controller) != source.get("controller_sha256")
        or source.get("local_imports") != expected_imports
        or runtime.get("python_path") != str(python)
        or _sha256_path(python) != runtime.get("python_sha256")
        or environment.get("python_path") != str(python)
        or environment.get("python_sha256") != runtime.get("python_sha256")
        or environment_root / "lib/python3.11/site-packages" != site_packages
        or receipt.get("controller_arguments_digest")
        != _sha256_bytes(_canonical(list(controller_arguments)))
    ):
        raise BootstrapError("controller bootstrap receipt identity mismatch")
    count, total, digest = site_packages_identity(environment_root)
    if (
        environment.get("site_packages_file_count") != count
        or environment.get("site_packages_total_size_bytes") != total
        or environment.get("site_packages_tree_sha256") != digest
    ):
        raise BootstrapError("installed site-packages identity mismatch")
    if require_external_bindings:
        gh_path = Path(str(pinned_gh.get("path")))
        staging_root = Path(str(staging_artifact.get("root")))
        if (
            sys.platform != EXPECTED_PLATFORM
            or platform.machine() != EXPECTED_MACHINE
            or platform.python_implementation() != "CPython"
            or platform.python_version() != EXPECTED_PYTHON_VERSION
            or sys.flags.isolated != 1
            or sys.flags.no_site != 1
            or sys.flags.dont_write_bytecode != 1
            or runtime.get("bootstrap_python_sha256") != "sha256:" + EXPECTED_PYTHON_SHA256
            or runtime.get("bootstrap_runtime_tree_sha256")
            != "sha256:" + EXPECTED_RUNTIME_INVENTORY_SHA256
            or runtime.get("python_sha256") != "sha256:" + EXPECTED_PYTHON_SHA256
            or pinned_gh
            != {
                "path": str(gh_path),
                "sha256": "sha256:" + EXPECTED_GH_SHA256,
                "version": EXPECTED_GH_VERSION,
            }
            or staging_root != environment_root.parent / "staging-artifact"
        ):
            raise BootstrapError("inner gate external runtime binding mismatch")
        _require_pinned_gh(gh_path)
        acquisition_raw = _read_regular(
            staging_root / "acquisition-receipt.json",
            label="inner gate staging acquisition receipt",
            max_bytes=MAX_JSON_BYTES,
        )
        dependency_raw = _read_regular(
            staging_root / "contents/recovery/dependency-staging-receipt.json",
            label="inner gate staging dependency receipt",
            max_bytes=MAX_JSON_BYTES,
        )
        if (
            staging_artifact.get("run_id")
            != _validated_positive_integer(
                staging_artifact.get("run_id"), label="inner gate staging run ID"
            )
            or staging_artifact.get("artifact_id")
            != _validated_positive_integer(
                staging_artifact.get("artifact_id"),
                label="inner gate staging artifact ID",
            )
            or staging_artifact.get("artifact_digest")
            != _validated_sha256(
                staging_artifact.get("artifact_digest"),
                label="inner gate staging artifact digest",
            )
            or staging_artifact.get("acquisition_receipt_digest") != _sha256_bytes(acquisition_raw)
            or staging_artifact.get("dependency_receipt_digest") != _sha256_bytes(dependency_raw)
        ):
            raise BootstrapError("inner gate staging artifact binding mismatch")
    if require_source_git:
        head = _run_git(source_root, "rev-parse", "HEAD^{commit}").strip()
        status = _run_git(
            source_root,
            "status",
            "--porcelain=v1",
            "--untracked-files=all",
        )
        if (
            head != str(source["sha"]).encode("ascii")
            or status
            or _source_tree_identity(source_root) != source.get("tree_sha256")
        ):
            raise BootstrapError("inner gate source identity changed")
    return controller, site_packages


def _acquire_staging_artifact(_request: InitialBootstrapRequest) -> Path:
    token = os.environ.get("GH_TOKEN")
    if token is None:
        raise BootstrapError("staging artifact owner credential is unavailable")
    try:
        token_bytes = token.encode("ascii", "strict")
    except UnicodeEncodeError as exc:
        raise BootstrapError("staging artifact owner credential is not ASCII") from exc
    return acquire_staging_artifact(
        _request,
        token=token_bytes,
        output_root=_request.bootstrap_root / "staging-artifact",
    )


def _exec_inner_gate(
    request: InitialBootstrapRequest,
    receipt_path: Path,
) -> None:
    receipt = _canonical_object(
        _read_regular(
            receipt_path,
            label="controller bootstrap receipt",
            max_bytes=MAX_JSON_BYTES,
        ),
        label="controller bootstrap receipt",
    )
    environment = receipt.get("environment")
    if type(environment) is not dict:
        raise BootstrapError("controller bootstrap environment is missing")
    python = Path(str(environment.get("python_path")))
    required_environment = {}
    for name in ("GH_TOKEN", "RELEASE_RECOVERY_READER_TOKEN"):
        value = os.environ.get(name)
        if value is None or not value:
            raise BootstrapError(f"controller bootstrap requires {name}")
        required_environment[name] = value
    required_environment.update(
        {
            "GH_PROMPT_DISABLED": "1",
            "KESTREL_PINNED_GH": str(request.pinned_gh),
            "KESTREL_RECOVERY_CONTROLLER_BOOTSTRAP_RECEIPT": str(receipt_path),
            "LANG": "C.UTF-8",
            "LD_LIBRARY_PATH": str(request.bootstrap_root / "runtime/lib"),
            "LC_ALL": "C.UTF-8",
            "NO_COLOR": "1",
            "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
            "PYTHONDONTWRITEBYTECODE": "1",
        }
    )
    arguments = [
        str(python),
        "-I",
        "-S",
        "-B",
        str(SCRIPT_PATH),
        "--inner-gate",
        "--bootstrap-receipt",
        str(receipt_path),
        "--bootstrap-root",
        str(request.bootstrap_root),
        "--pinned-gh",
        str(request.pinned_gh),
        *request.controller_arguments,
        *(["--prepare-only"] if request.prepare_only else []),
    ]
    os.execve(str(python), arguments, required_environment)


def run_initial_bootstrap(request: InitialBootstrapRequest) -> None:
    """Run preflight in authority order, then prepare the import environment."""

    _require_preimport_runtime(request)
    source_tree_digest = _require_source_identity(request)
    _require_pinned_gh(request.pinned_gh)
    receipt_path = request.bootstrap_root / "bootstrap-receipt.json"
    _clear_write_once_scratch(request.bootstrap_root, name=receipt_path.name)
    if receipt_path.exists() or receipt_path.is_symlink():
        if receipt_path.is_symlink() or not receipt_path.is_file():
            raise BootstrapError("existing bootstrap receipt is not a regular file")
        try:
            receipt = _canonical_object(
                _read_regular(
                    receipt_path,
                    label="controller bootstrap receipt",
                    max_bytes=MAX_JSON_BYTES,
                ),
                label="controller bootstrap receipt",
            )
        except BootstrapError:
            receipt_path.unlink()
            _fsync_directory(request.bootstrap_root)
        else:
            environment = receipt.get("environment")
            if type(environment) is not dict:
                raise BootstrapError("existing bootstrap receipt environment is invalid")
            authorize_inner_gate(
                receipt_path=receipt_path,
                source_root=request.source_root,
                controller_arguments=request.controller_arguments,
                executing_script=SCRIPT_PATH,
                executing_python=Path(str(environment.get("python_path"))),
            )
            _exec_inner_gate(request, receipt_path)
            return
    artifact_contents = _acquire_staging_artifact(request)
    validated = validate_staging_artifact(
        artifact_contents,
        source_root=request.source_root,
        source_sha=request.source_sha,
    )
    environment_root = request.bootstrap_root / "controller-environment"
    environment = build_controller_environment(
        validated,
        environment_root=environment_root,
        base_python=request.bootstrap_root / "runtime/bin/python3.11",
    )
    if _source_tree_identity(request.source_root) != source_tree_digest:
        raise BootstrapError("bootstrap source changed while preparing the environment")
    controller = request.source_root / "scripts/recovery_capsule_controller.py"
    local_imports = [
        {
            "path": str(request.source_root / "scripts" / name),
            "sha256": _sha256_path(request.source_root / "scripts" / name),
        }
        for name in LOCAL_IMPORT_NAMES
    ]
    acquisition_receipt = _read_regular(
        artifact_contents.parent / "acquisition-receipt.json",
        label="staging acquisition receipt",
        max_bytes=MAX_JSON_BYTES,
    )
    environment_python = Path(str(environment["python_path"]))
    receipt = {
        "schema": "kestrel.recovery_controller_bootstrap.v1",
        "source": {
            "root": str(request.source_root),
            "sha": request.source_sha,
            "tree_sha256": source_tree_digest,
            "bootstrap_path": str(SCRIPT_PATH),
            "bootstrap_sha256": _sha256_path(SCRIPT_PATH),
            "controller_path": str(controller),
            "controller_sha256": _sha256_path(controller),
            "local_imports": local_imports,
        },
        "runtime": {
            "bootstrap_python_path": str(request.bootstrap_root / "runtime/bin/python3.11"),
            "bootstrap_python_sha256": ("sha256:" + EXPECTED_PYTHON_SHA256),
            "bootstrap_runtime_tree_sha256": ("sha256:" + EXPECTED_RUNTIME_INVENTORY_SHA256),
            "python_path": str(environment_python),
            "python_sha256": _sha256_path(environment_python),
        },
        "environment": environment,
        "pinned_gh": {
            "path": str(request.pinned_gh),
            "sha256": "sha256:" + EXPECTED_GH_SHA256,
            "version": EXPECTED_GH_VERSION,
        },
        "staging_artifact": {
            "root": str(request.bootstrap_root / "staging-artifact"),
            "run_id": request.staging_run_id,
            "artifact_id": request.staging_artifact_id,
            "artifact_digest": request.staging_artifact_digest,
            "acquisition_receipt_digest": _sha256_bytes(acquisition_receipt),
            "dependency_receipt_digest": _sha256_bytes(validated.receipt_raw),
        },
        "controller_arguments_digest": _sha256_bytes(
            _canonical(list(request.controller_arguments))
        ),
        "validation_status": "validated",
    }
    _write_once(receipt_path, _canonical(receipt))
    _exec_inner_gate(request, receipt_path)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--inner-gate", action="store_true")
    parser.add_argument("--bootstrap-receipt", type=Path)
    parser.add_argument("--bootstrap-root", type=Path, required=True)
    parser.add_argument("--pinned-gh", type=Path, required=True)
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--source-sha", required=True)
    parser.add_argument("--staging-run-id", type=int, required=True)
    parser.add_argument("--staging-artifact-id", type=int, required=True)
    parser.add_argument("--staging-artifact-digest", required=True)
    parser.add_argument("--prepare-only", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    args, remaining = parser.parse_known_args(argv)
    source_root = args.source_root.resolve(strict=True)
    controller_arguments = (
        "--source-root",
        str(source_root),
        "--source-sha",
        args.source_sha,
        "--staging-run-id",
        str(args.staging_run_id),
        "--staging-artifact-id",
        str(args.staging_artifact_id),
        "--staging-artifact-digest",
        args.staging_artifact_digest,
        *remaining,
    )
    try:
        if args.inner_gate:
            if args.bootstrap_receipt is None:
                raise BootstrapError("inner gate requires a bootstrap receipt")
            controller, site_packages = authorize_inner_gate(
                receipt_path=args.bootstrap_receipt.resolve(strict=True),
                source_root=source_root,
                controller_arguments=controller_arguments,
            )
            sys.path[:] = [str(site_packages), str(source_root), *sys.path]
            sys.argv = [
                str(controller),
                *controller_arguments,
                *(["--prepare-only"] if args.prepare_only else []),
            ]
            runpy.run_path(str(controller), run_name="__main__")
            return 0
        request = InitialBootstrapRequest(
            source_root=source_root,
            source_sha=args.source_sha,
            staging_run_id=args.staging_run_id,
            staging_artifact_id=args.staging_artifact_id,
            staging_artifact_digest=args.staging_artifact_digest,
            pinned_gh=args.pinned_gh.resolve(strict=True),
            bootstrap_root=args.bootstrap_root.resolve(strict=True),
            controller_arguments=controller_arguments,
            prepare_only=args.prepare_only,
        )
        run_initial_bootstrap(request)
    except (BootstrapError, OSError, subprocess.SubprocessError) as exc:
        parser.error(str(exc))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
