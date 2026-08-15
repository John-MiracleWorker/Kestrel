#!/usr/bin/env python3
"""Verify and launch only the executable closure frozen in a recovery capsule."""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
import platform
import subprocess  # nosec B404
import sys
import tarfile
from collections.abc import Mapping, Sequence
from pathlib import Path, PurePosixPath
from typing import cast
from urllib.parse import SplitResult, urlsplit

if __name__ == "__main__" and not (
    sys.flags.isolated and sys.flags.no_site and sys.flags.dont_write_bytecode
):
    sys.stderr.write("recovery launcher requires Python -I -S -B isolation\n")
    raise SystemExit(2)

from scripts import release_control_receipt as receipts

CLOSURE_SCHEMA = "kestrel.recovery_execution_closure.v1"
MAX_CLOSURE_BYTES = 16 * 1024 * 1024
MAX_MEMBER_BYTES = 128 * 1024 * 1024
MAX_ARCHIVE_MEMBERS = 8192
MAX_ARCHIVE_MEMBER_BYTES = 2_147_483_648
MAX_ARCHIVE_TOTAL_BYTES = 2_147_483_648
ISOLATED_PYTHON_BOOTSTRAP = (
    "import json,os,runpy,sys;"
    "allowed={'LANG','LC_ALL','LD_LIBRARY_PATH','PATH','PYTHONNOUSERSITE',"
    "'PYTHONSAFEPATH','__CF_USER_TEXT_ENCODING'};"
    "unexpected=set(os.environ)-allowed;"
    "len(unexpected)==0 or (_ for _ in ()).throw(RuntimeError('ambient recovery environment'));"
    "sys.path[:]=json.loads(sys.argv.pop(1));"
    "target=sys.argv.pop(1);"
    "runpy.run_path(target,run_name='__main__')"
)
TRUSTED_RECOVERY_PYTHON_IDENTITIES: Mapping[
    tuple[str, str], frozenset[tuple[str, str, str, str, str]]
] = {
    ("linux", "x86_64"): frozenset(
        {
            (
                "sha256:dcd2d22a91c5adb37fa3f54a3a16d2ed7616b84931eb606c0fdfbca38395dab8",
                "Python 3.11.14",
                "CPython",
                "3.11.14",
                "cp311",
            )
        }
    )
}
TRUSTED_OS_SANDBOX_IDENTITIES: Mapping[tuple[str, str], frozenset[tuple[str, str]]] = {
    ("linux", "x86_64"): frozenset(
        {
            (
                "sha256:52231e1caf55bcbc667b269f49c63599a6f7db4767ae6a039580d0ff853db712",
                "bubblewrap 0.9.0",
            )
        }
    )
}


def _closure(raw: bytes) -> receipts.JSONObject:
    value = receipts.strict_canonical_json(raw, label="recovery execution closure")
    closure = receipts._object(value, label="recovery execution closure")  # noqa: SLF001
    receipts._validate_schema(  # noqa: SLF001
        CLOSURE_SCHEMA, closure, label="recovery execution closure"
    )
    return closure


def _relative_member(value: object, *, label: str) -> PurePosixPath:
    text = receipts._validate_string(value, label=label)  # noqa: SLF001
    path = PurePosixPath(text)
    if (
        path.is_absolute()
        or not path.parts
        or any(part in {"", ".", ".."} for part in path.parts)
        or "\\" in text
        or "\x00" in text
    ):
        raise receipts.ReleaseControlError(f"{label} is not a safe relative path")
    return path


def _regular_member(
    root: Path,
    relative: PurePosixPath,
    *,
    label: str,
    max_bytes: int = MAX_MEMBER_BYTES,
) -> Path:
    path = root.joinpath(*relative.parts)
    try:
        resolved_root = root.resolve(strict=True)
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise receipts.ReleaseControlError(f"{label} is missing") from exc
    if (
        resolved_root not in (resolved, *resolved.parents)
        or not path.is_file()
        or path.is_symlink()
        or path.stat().st_size > max_bytes
    ):
        raise receipts.ReleaseControlError(f"{label} is not a bounded regular member")
    return path


def _member_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def _member_table(
    closure: receipts.JSONObject,
    *,
    field: str,
    capsule_root: Path,
) -> dict[str, tuple[Path, str]]:
    table: dict[str, tuple[Path, str]] = {}
    previous = ""
    for raw_item in receipts._array(closure.get(field), label=field):  # noqa: SLF001
        item = receipts._object(raw_item, label=f"{field} item")  # noqa: SLF001
        relative = _relative_member(item.get("path"), label=f"{field} path")
        relative_text = relative.as_posix()
        if relative_text <= previous or relative_text in table:
            raise receipts.ReleaseControlError(f"{field} is not sorted unique")
        previous = relative_text
        expected = receipts._digest(  # noqa: SLF001
            item.get("sha256"), label=f"{field} digest"
        )
        path = _regular_member(capsule_root, relative, label=f"{field} member {relative_text}")
        if _member_digest(path) != expected:
            raise receipts.ReleaseControlError(f"{field} member digest mismatch")
        table[relative_text] = (path, expected)
    return table


def _module_name(path: str) -> str:
    relative = PurePosixPath(path)
    parts = list(relative.with_suffix("").parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _local_static_imports(
    *, member_path: str, source: bytes, modules: Mapping[str, str]
) -> set[tuple[str, str, str]]:
    try:
        tree = ast.parse(source, filename=member_path)
    except (SyntaxError, ValueError) as exc:
        raise receipts.ReleaseControlError(
            f"recovery Python member {member_path} cannot be parsed"
        ) from exc
    found: set[tuple[str, str, str]] = set()
    for node in ast.walk(tree):
        candidates: list[str] = []
        if isinstance(node, ast.Import):
            candidates.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            candidates.append(node.module)
            candidates.extend(f"{node.module}.{alias.name}" for alias in node.names)
        for module in candidates:
            target = modules.get(module)
            if target is not None:
                found.add((member_path, module, target))
    return found


def _literal_dynamic_imports(*, member_path: str, source: bytes) -> set[tuple[str, str]]:
    tree = ast.parse(source, filename=member_path)
    found: set[tuple[str, str]] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        is_dynamic = isinstance(node.func, ast.Name) and node.func.id == "__import__"
        is_dynamic = is_dynamic or (
            isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "importlib"
            and node.func.attr == "import_module"
        )
        if not is_dynamic:
            continue
        if (
            not node.args
            or not isinstance(node.args[0], ast.Constant)
            or not isinstance(node.args[0].value, str)
            or not node.args[0].value
        ):
            raise receipts.ReleaseControlError(
                f"recovery dynamic import in {member_path} is not a literal string"
            )
        found.add((member_path, node.args[0].value))
    return found


def _edge_table(
    closure: receipts.JSONObject,
    *,
    field: str,
    members: Mapping[str, tuple[Path, str]],
) -> set[tuple[str, str, str]]:
    edges: set[tuple[str, str, str]] = set()
    previous: tuple[str, str, str] | None = None
    for raw_edge in receipts._array(closure.get(field), label=field):  # noqa: SLF001
        edge = receipts._object(raw_edge, label=f"{field} edge")  # noqa: SLF001
        importer = _relative_member(edge.get("importer"), label=f"{field} importer").as_posix()
        module = receipts._validate_string(  # noqa: SLF001
            edge.get("module"), label=f"{field} module"
        )
        target = _relative_member(edge.get("member_path"), label=f"{field} member path").as_posix()
        key = (importer, module, target)
        if previous is not None and key <= previous:
            raise receipts.ReleaseControlError(f"{field} is not sorted unique")
        previous = key
        if importer not in members or target not in members:
            raise receipts.ReleaseControlError(f"{field} references an unlisted member")
        if edge.get("member_sha256") != members[target][1]:
            raise receipts.ReleaseControlError(f"{field} member digest mismatch")
        edges.add(key)
    return edges


def _verify_member_inventory(
    capsule_root: Path,
    *,
    python_members: Mapping[str, tuple[Path, str]],
    shell_helpers: Mapping[str, tuple[Path, str]],
    data_resources: Mapping[str, tuple[Path, str]],
    ignored_roots: Sequence[Path] = (),
) -> None:
    resolved_ignored = tuple(path.resolve(strict=True) for path in ignored_roots)

    def ignored(path: Path) -> bool:
        resolved = path.resolve(strict=True)
        return any(root in (resolved, *resolved.parents) for root in resolved_ignored)

    actual_python: set[str] = set()
    actual_shell: set[str] = set()
    actual_resources: set[str] = set()
    declared_resources = set(data_resources)
    nonclosure_assets = set(receipts._RECOVERY_CAPSULE_FIXED_ASSETS) | {  # noqa: SLF001
        "recovery-capsule-manifest.json",
        "recovery/requirements.txt",
        "recovery/wheelhouse-manifest.json",
    }
    for path in capsule_root.rglob("*"):
        if path.is_symlink():
            raise receipts.ReleaseControlError(
                "recovery capsule member inventory contains a symbolic link"
            )
        if not path.is_file():
            continue
        if ignored(path):
            continue
        relative = path.relative_to(capsule_root).as_posix()
        if path.suffix == ".py":
            actual_python.add(relative)
        elif path.suffix == ".sh":
            actual_shell.add(relative)
        elif relative in declared_resources:
            actual_resources.add(relative)
        else:
            relative_path = PurePosixPath(relative)
            is_wheel = (
                len(relative_path.parts) == 3
                and relative_path.parts[:2] == ("recovery", "wheelhouse")
                and relative_path.name.endswith(".whl")
            )
            if relative not in nonclosure_assets and not is_wheel:
                actual_resources.add(relative)
    if actual_python != set(python_members):
        raise receipts.ReleaseControlError(
            "recovery Python member inventory has missing or extra files"
        )
    if actual_shell != set(shell_helpers):
        raise receipts.ReleaseControlError(
            "recovery shell helper inventory has missing or extra files"
        )
    if actual_resources != set(data_resources):
        raise receipts.ReleaseControlError(
            "recovery data resource inventory has missing or extra files"
        )


def _verify_dependency_lock(closure: receipts.JSONObject, capsule_root: Path) -> None:
    lock = receipts._object(  # noqa: SLF001
        closure.get("dependency_lock"), label="recovery dependency lock"
    )
    requirements_relative = _relative_member(
        lock.get("requirements_path"), label="recovery requirements path"
    )
    requirements = _regular_member(
        capsule_root, requirements_relative, label="recovery requirements lock"
    )
    if _member_digest(requirements) != lock.get("requirements_sha256"):
        raise receipts.ReleaseControlError("recovery requirements digest mismatch")
    wheelhouse = _regular_member(
        capsule_root,
        PurePosixPath("recovery/wheelhouse-manifest.json"),
        label="recovery wheelhouse manifest",
    )
    if _member_digest(wheelhouse) != lock.get("wheelhouse_manifest_sha256"):
        raise receipts.ReleaseControlError("recovery wheelhouse manifest digest mismatch")
    runtime_digest = lock.get("runtime_manifest_sha256")
    if runtime_digest is not None:
        runtime_manifest = _regular_member(
            capsule_root,
            PurePosixPath("recovery/runtime-manifest.json"),
            label="recovery runtime manifest",
        )
        if _member_digest(runtime_manifest) != runtime_digest:
            raise receipts.ReleaseControlError("recovery runtime manifest digest mismatch")
    for field, relative, label in (
        (
            "python_runtime_manifest_sha256",
            PurePosixPath("recovery/python-runtime-manifest.json"),
            "recovery Python runtime manifest",
        ),
        (
            "python_runtime_archive_sha256",
            PurePosixPath("recovery/python-runtime.tar.gz"),
            "recovery Python runtime archive",
        ),
    ):
        expected = lock.get(field)
        if expected is None:
            continue
        member = _regular_member(
            capsule_root,
            relative,
            label=label,
            max_bytes=(
                MAX_ARCHIVE_TOTAL_BYTES
                if field == "python_runtime_archive_sha256"
                else MAX_MEMBER_BYTES
            ),
        )
        if _member_digest(member) != expected:
            raise receipts.ReleaseControlError(f"{label} digest mismatch")


def _python_runtime_tree_identity(root: Path) -> tuple[int, int, str]:
    """Re-authenticate the extracted link-free Python tree before every launch."""

    if root.is_symlink() or not root.is_dir():
        raise receipts.ReleaseControlError("recovery Python runtime tree root is unsafe")
    records: list[dict[str, object]] = []
    total = 0
    for path in sorted(root.rglob("*"), key=lambda item: item.relative_to(root).as_posix()):
        if path.is_symlink():
            raise receipts.ReleaseControlError("recovery Python runtime tree contains a link")
        if path.is_dir():
            continue
        if not path.is_file():
            raise receipts.ReleaseControlError(
                "recovery Python runtime tree contains a special file"
            )
        size = path.stat().st_size
        total += size
        if size < 0 or size > MAX_ARCHIVE_MEMBER_BYTES:
            raise receipts.ReleaseControlError("recovery Python runtime file is too large")
        if total > MAX_ARCHIVE_TOTAL_BYTES or len(records) >= MAX_ARCHIVE_MEMBERS * 2:
            raise receipts.ReleaseControlError("recovery Python runtime tree is too large")
        mode = 0o755 if path.stat().st_mode & 0o111 else 0o644
        records.append(
            {
                "mode": f"{mode:04o}",
                "path": path.relative_to(root).as_posix(),
                "sha256": _member_digest(path),
                "size_bytes": size,
            }
        )
    if not records:
        raise receipts.ReleaseControlError("recovery Python runtime tree is empty")
    digest = receipts._sha256(  # noqa: SLF001
        json.dumps(records, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode()
    )
    return len(records), total, digest


def _verify_extracted_python_runtime(
    *, closure: bytes, capsule_root: Path
) -> Path | None:
    """Verify the staged base tree and return its private library root."""

    value = _closure(closure)
    lock = receipts._object(  # noqa: SLF001
        value.get("dependency_lock"), label="recovery dependency lock"
    )
    manifest_digest = lock.get("python_runtime_manifest_sha256")
    archive_digest = lock.get("python_runtime_archive_sha256")
    if manifest_digest is None and archive_digest is None:
        return None
    if not isinstance(manifest_digest, str) or not isinstance(archive_digest, str):
        raise receipts.ReleaseControlError("recovery Python runtime lock is incomplete")
    _verify_dependency_lock(value, capsule_root)
    manifest_path = _regular_member(
        capsule_root,
        PurePosixPath("recovery/python-runtime-manifest.json"),
        label="recovery Python runtime manifest",
    )
    manifest = receipts._object(  # noqa: SLF001
        receipts.strict_canonical_json(
            manifest_path.read_bytes(), label="recovery Python runtime manifest"
        ),
        label="recovery Python runtime manifest",
    )
    receipts._validate_schema(  # noqa: SLF001
        "kestrel.recovery_python_runtime.v1",
        manifest,
        label="recovery Python runtime manifest",
    )
    python = resolve_external_executable(closure=closure, name="python")
    runtime_root = capsule_root.resolve(strict=True).parent / "recovery-runtime"
    expected_python = runtime_root / "environment" / "bin" / "python"
    if python != expected_python.resolve(strict=True):
        raise receipts.ReleaseControlError("recovery Python runtime path is not exact")
    base_root = runtime_root / "base"
    count, total, tree_digest = _python_runtime_tree_identity(base_root)
    if (
        count != manifest.get("runtime_file_count")
        or total != manifest.get("runtime_total_size_bytes")
        or tree_digest != manifest.get("runtime_tree_sha256")
        or _member_digest(base_root / "bin" / "python3.11")
        != manifest.get("python_executable_sha256")
    ):
        raise receipts.ReleaseControlError("recovery Python runtime tree identity mismatch")
    library_root = (base_root / "lib").resolve(strict=True)
    _authorize_io_path(value, path=library_root)
    return library_root


def _validated_runtime_files(
    closure: receipts.JSONObject,
) -> tuple[tuple[Path, Path], ...]:
    raw_runtime = closure.get("runtime_files")
    if raw_runtime is None:
        return ()
    sys_path = receipts._array(closure.get("sys_path"), label="recovery sys.path")  # noqa: SLF001
    capsule_root = Path(
        receipts._validate_string(  # noqa: SLF001
            sys_path[0], label="recovery capsule sys.path"
        )
    ).resolve(strict=True)
    resources = _member_table(closure, field="data_resources", capsule_root=capsule_root)
    runtime_files: list[tuple[Path, Path]] = []
    previous = ""
    for raw_item in receipts._array(raw_runtime, label="recovery runtime files"):  # noqa: SLF001
        item = receipts._object(raw_item, label="recovery runtime file")  # noqa: SLF001
        asset_name = _relative_member(
            item.get("asset_path"), label="recovery runtime asset path"
        ).as_posix()
        sandbox_name = receipts._validate_string(  # noqa: SLF001
            item.get("sandbox_path"), label="recovery runtime sandbox path"
        )
        sandbox_path = Path(sandbox_name)
        source_entry = resources.get(asset_name)
        if (
            source_entry is None
            or not sandbox_path.is_absolute()
            or sandbox_path.as_posix() != sandbox_name
            or any(part in {".", ".."} for part in sandbox_path.parts)
            or sandbox_name <= previous
            or sandbox_name.startswith(("/dev/", "/proc/", "/sys/", "/tmp/"))  # nosec B108
        ):
            raise receipts.ReleaseControlError("recovery runtime file binding is unsafe")
        source, member_digest = source_entry
        expected_digest = receipts._digest(  # noqa: SLF001
            item.get("sha256"), label="recovery runtime file digest"
        )
        expected_size = receipts._safe_integer(  # noqa: SLF001
            item.get("size_bytes"), label="recovery runtime file size", positive=True
        )
        if (
            member_digest != expected_digest
            or _member_digest(source) != expected_digest
            or source.stat().st_size != expected_size
            or source.is_symlink()
            or not source.is_file()
        ):
            raise receipts.ReleaseControlError("recovery runtime file identity mismatch")
        runtime_files.append((source.resolve(strict=True), sandbox_path))
        previous = sandbox_name
    return tuple(runtime_files)


def _ensure_no_global_loader_preload(
    *, preload_path: Path = Path("/etc/ld.so.preload")
) -> None:
    """Fail before a global loader preload can cross the recovery TCB boundary."""

    if preload_path.exists() or preload_path.is_symlink():
        raise receipts.ReleaseControlError(
            "global dynamic-loader preload is outside the recovery closure"
        )


def private_loader_command(
    *,
    closure: bytes,
    executable: Path,
    arguments: Sequence[str],
    additional_library_roots: Sequence[Path] = (),
    preload_path: Path = Path("/etc/ld.so.preload"),
) -> list[str]:
    """Preflight and build an invocation resolved only by digest-bound libraries."""

    _ensure_no_global_loader_preload(preload_path=preload_path)
    value = _closure(closure)
    checked_executable = executable.resolve(strict=True)
    external = {
        resolve_external_executable(closure=closure, name=cast(str, item["name"]))
        for raw_item in receipts._array(  # noqa: SLF001
            value.get("external_executables"), label="recovery external executables"
        )
        if (item := receipts._object(raw_item, label="recovery external executable"))  # noqa: SLF001
    }
    if checked_executable not in external:
        raise receipts.ReleaseControlError(
            "private-loader executable is outside the exact recovery closure"
        )
    runtime_files = _validated_runtime_files(value)
    if not runtime_files:
        raise receipts.ReleaseControlError("private recovery runtime is empty")
    runtime_roots = {source.parent for source, _target in runtime_files}
    if len(runtime_roots) != 1:
        raise receipts.ReleaseControlError("private recovery runtime directory is ambiguous")
    runtime_root = next(iter(runtime_roots)).resolve(strict=True)
    runtime_names: set[str] = set()
    loaders: list[Path] = []
    for source, target in runtime_files:
        if (
            source.parent != runtime_root
            or source.name != target.name
            or source.name in runtime_names
        ):
            raise receipts.ReleaseControlError(
                "private recovery runtime has a basename collision"
            )
        runtime_names.add(source.name)
        if target.name.startswith("ld-linux-"):
            loaders.append(source)
    if len(loaders) != 1:
        raise receipts.ReleaseControlError("private recovery dynamic loader is ambiguous")
    if any(path.is_dir() for path in runtime_root.iterdir()):
        raise receipts.ReleaseControlError(
            "private recovery runtime contains an unexpected loader search directory"
        )
    library_roots = [runtime_root]
    for raw_root in additional_library_roots:
        if raw_root.is_symlink() or not raw_root.is_dir():
            raise receipts.ReleaseControlError("private recovery library root is unsafe")
        root = raw_root.resolve(strict=True)
        if root in library_roots:
            raise receipts.ReleaseControlError("private recovery library root is duplicated")
        library_roots.append(root)
    allowed_files = {
        path.resolve(strict=True)
        for root in library_roots
        for path in root.iterdir()
        if path.is_file() and not path.is_symlink()
    }
    loader = loaders[0]
    library_path = ":".join(str(path) for path in library_roots)
    preflight = subprocess.run(  # noqa: S603  # nosec B603
        [
            str(loader),
            "--inhibit-cache",
            "--library-path",
            library_path,
            "--list",
            str(checked_executable),
        ],
        check=False,
        capture_output=True,
        text=True,
        env={"LANG": "C.UTF-8", "LC_ALL": "C.UTF-8"},
        timeout=10,
    )
    combined = "\n".join(part for part in (preflight.stdout, preflight.stderr) if part)
    if (
        preflight.returncode != 0
        or "not found" in combined
        or len(combined.encode()) > 1024 * 1024
    ):
        raise receipts.ReleaseControlError("private recovery loader preflight failed")
    resolved_dependencies: set[Path] = set()
    for raw_line in preflight.stdout.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("linux-vdso.so"):
            continue
        token = (
            line.split("=>", 1)[1].strip().split(None, 1)[0]
            if "=>" in line
            else line.split(None, 1)[0]
        )
        path = Path(token)
        if not path.is_absolute():
            raise receipts.ReleaseControlError(
                "private recovery loader reported an ambient dependency"
            )
        try:
            resolved = path.resolve(strict=True)
        except OSError as exc:
            raise receipts.ReleaseControlError(
                "private recovery loader dependency is missing"
            ) from exc
        if resolved not in allowed_files or resolved.parent not in library_roots:
            raise receipts.ReleaseControlError(
                "private recovery loader resolved an ambient dependency"
            )
        resolved_dependencies.add(resolved)
    if loader not in resolved_dependencies:
        raise receipts.ReleaseControlError(
            "private recovery loader did not resolve its own exact identity"
        )
    return [
        str(loader),
        "--inhibit-cache",
        "--library-path",
        library_path,
        str(checked_executable),
        *arguments,
    ]


def resolve_external_executable(*, closure: bytes, name: str) -> Path:
    value = _closure(closure)
    matches: list[Path] = []
    for raw_item in receipts._array(  # noqa: SLF001
        value.get("external_executables"), label="recovery external executables"
    ):
        item = receipts._object(raw_item, label="recovery external executable")  # noqa: SLF001
        if item.get("name") != name:
            continue
        path = Path(
            receipts._validate_string(  # noqa: SLF001
                item.get("path"), label="recovery external executable path"
            )
        )
        if (
            not path.is_absolute()
            or not path.is_file()
            or path.is_symlink()
            or not os.access(path, os.X_OK)
            or _member_digest(path) != item.get("sha256")
        ):
            raise receipts.ReleaseControlError("recovery external executable identity mismatch")
        matches.append(path)
    if len(matches) != 1:
        raise receipts.ReleaseControlError("recovery external executable is absent or ambiguous")
    return matches[0]


def resolve_trusted_os_sandbox(*, closure: bytes) -> Path:
    """Resolve only a closure member also pinned by Kestrel for this platform."""

    value = _closure(closure)
    matches = [
        receipts._object(item, label="recovery sandbox executable")  # noqa: SLF001
        for item in receipts._array(  # noqa: SLF001
            value.get("external_executables"), label="recovery external executables"
        )
        if receipts._object(  # noqa: SLF001
            item, label="recovery external executable"
        ).get("name")
        == "sandbox"
    ]
    if len(matches) != 1:
        raise receipts.ReleaseControlError("Kestrel-trusted recovery sandbox is absent")
    sandbox = matches[0]
    identity = (
        receipts._digest(  # noqa: SLF001
            sandbox.get("sha256"), label="recovery sandbox executable digest"
        ),
        receipts._validate_string(  # noqa: SLF001
            sandbox.get("version"), label="recovery sandbox executable version"
        ),
    )
    platform_identities = TRUSTED_OS_SANDBOX_IDENTITIES.get(
        (sys.platform, platform.machine()), frozenset()
    )
    if identity not in platform_identities:
        raise receipts.ReleaseControlError(
            "recovery sandbox is not a Kestrel-trusted platform identity"
        )
    return resolve_external_executable(closure=closure, name="sandbox")


def resolve_trusted_recovery_python(*, closure: bytes) -> Path:
    """Resolve only the independently pinned recovery interpreter identity."""

    value = _closure(closure)
    matches = [
        receipts._object(item, label="recovery Python executable")  # noqa: SLF001
        for item in receipts._array(  # noqa: SLF001
            value.get("external_executables"), label="recovery external executables"
        )
        if receipts._object(  # noqa: SLF001
            item, label="recovery external executable"
        ).get("name")
        == "python"
    ]
    if len(matches) != 1:
        raise receipts.ReleaseControlError(
            "Kestrel-trusted recovery Python is absent"
        )
    python_item = matches[0]
    runtime = receipts._object(  # noqa: SLF001
        value.get("python_runtime"), label="recovery Python runtime"
    )
    identity = (
        receipts._digest(  # noqa: SLF001
            python_item.get("sha256"), label="recovery Python executable digest"
        ),
        receipts._validate_string(  # noqa: SLF001
            python_item.get("version"), label="recovery Python executable version"
        ),
        receipts._validate_string(  # noqa: SLF001
            runtime.get("implementation"), label="recovery Python implementation"
        ),
        receipts._validate_string(  # noqa: SLF001
            runtime.get("version"), label="recovery Python runtime version"
        ),
        receipts._validate_string(  # noqa: SLF001
            runtime.get("abi"), label="recovery Python ABI"
        ),
    )
    platform_identities = TRUSTED_RECOVERY_PYTHON_IDENTITIES.get(
        (sys.platform, platform.machine()), frozenset()
    )
    if identity not in platform_identities:
        raise receipts.ReleaseControlError(
            "recovery Python is not a Kestrel-trusted platform identity"
        )
    return resolve_external_executable(closure=closure, name="python")


def resolve_dynamic_import(
    *, closure: bytes, capsule_root: Path, importer: str, module: str
) -> Path:
    value = _closure(closure)
    members = _member_table(value, field="python_members", capsule_root=capsule_root)
    edges = _edge_table(value, field="dynamic_imports", members=members)
    matches = [target for source, name, target in edges if (source, name) == (importer, module)]
    if len(matches) != 1:
        raise receipts.ReleaseControlError(
            "recovery dynamic import is absent from the exact allowlist"
        )
    return members[matches[0]][0]


def build_isolated_environment(*, closure: bytes) -> dict[str, str]:
    value = _closure(closure)
    directories: list[str] = []
    for raw_item in receipts._array(  # noqa: SLF001
        value.get("external_executables"), label="recovery external executables"
    ):
        item = receipts._object(raw_item, label="recovery external executable")  # noqa: SLF001
        parent = str(Path(cast(str, item["path"])).parent)
        if parent not in directories:
            directories.append(parent)
    return {
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PATH": os.pathsep.join(directories),
        "PYTHONNOUSERSITE": "1",
        "PYTHONSAFEPATH": "1",
    }


def _validated_io_roots(
    closure: receipts.JSONObject,
) -> tuple[tuple[Path, str], ...]:
    roots: list[tuple[Path, str]] = []
    declared_paths: list[str] = []
    for raw_item in receipts._array(  # noqa: SLF001
        closure.get("io_roots"), label="recovery I/O roots"
    ):
        item = receipts._object(raw_item, label="recovery I/O root")  # noqa: SLF001
        raw_path = receipts._validate_string(  # noqa: SLF001
            item.get("path"), label="recovery I/O root path"
        )
        access = receipts._validate_string(  # noqa: SLF001
            item.get("access"), label="recovery I/O root access"
        )
        path = Path(raw_path)
        try:
            resolved = path.resolve(strict=True)
        except OSError as exc:
            raise receipts.ReleaseControlError("recovery I/O root is missing") from exc
        if (
            not path.is_absolute()
            or path.is_symlink()
            or not path.is_dir()
            or str(resolved) != raw_path
            or access not in {"read", "read_write"}
        ):
            raise receipts.ReleaseControlError("recovery I/O root is not an exact real directory")
        declared_paths.append(raw_path)
        roots.append((resolved, access))
    if declared_paths != sorted(set(declared_paths)):
        raise receipts.ReleaseControlError("recovery I/O roots are not sorted unique")
    return tuple(roots)


def _authorize_io_path(
    closure: receipts.JSONObject, *, path: Path, require_write: bool = False
) -> Path:
    if not path.is_absolute():
        raise receipts.ReleaseControlError("recovery I/O path is not absolute")
    resolved = path.resolve(strict=False)
    for root, access in _validated_io_roots(closure):
        if (resolved == root or root in resolved.parents) and (
            not require_write or access == "read_write"
        ):
            return resolved
    raise receipts.ReleaseControlError("recovery I/O path is outside the exact allowed roots")


def _network_endpoint(value: object, *, label: str) -> tuple[str, SplitResult]:
    endpoint = receipts._validate_string(value, label=label)  # noqa: SLF001
    parsed = urlsplit(endpoint)
    try:
        port = parsed.port
    except ValueError as exc:
        raise receipts.ReleaseControlError(f"{label} has an invalid port") from exc
    if (
        parsed.scheme != "https"
        or parsed.hostname is None
        or parsed.hostname != parsed.hostname.lower()
        or parsed.username is not None
        or parsed.password is not None
        or port is not None
        or parsed.fragment
        or parsed.netloc != parsed.hostname
        or any(part in {".", ".."} for part in PurePosixPath(parsed.path).parts)
    ):
        raise receipts.ReleaseControlError(f"{label} is not a canonical HTTPS endpoint")
    return endpoint, parsed


def authorize_network_endpoint(*, closure: bytes, endpoint: str) -> str:
    """Authorize one exact HTTPS target against the deny-first frozen policy."""

    value = _closure(closure)
    policy = receipts._object(  # noqa: SLF001
        value.get("network_policy"), label="recovery network policy"
    )
    if policy.get("default_deny") is not True:
        raise receipts.ReleaseControlError("recovery network policy is not deny-first")
    checked_endpoint, parsed_endpoint = _network_endpoint(
        endpoint, label="recovery network endpoint"
    )
    allowed_values = [
        receipts._validate_string(item, label="allowed recovery network endpoint")  # noqa: SLF001
        for item in receipts._array(  # noqa: SLF001
            policy.get("allowed_endpoints"), label="allowed recovery network endpoints"
        )
    ]
    if allowed_values != sorted(set(allowed_values)):
        raise receipts.ReleaseControlError(
            "allowed recovery network endpoints are not sorted unique"
        )
    for raw_allowed in allowed_values:
        allowed, parsed_allowed = _network_endpoint(
            raw_allowed, label="allowed recovery network endpoint"
        )
        if checked_endpoint == allowed:
            return checked_endpoint
        if (
            parsed_allowed.path in {"", "/"}
            and not parsed_allowed.query
            and parsed_endpoint.scheme == parsed_allowed.scheme
            and parsed_endpoint.hostname == parsed_allowed.hostname
        ):
            return checked_endpoint
    raise receipts.ReleaseControlError(
        "recovery network endpoint is absent from the exact allowlist"
    )


def authorize_launch_arguments(*, closure: bytes, arguments: Sequence[str]) -> tuple[str, ...]:
    """Reject network and filesystem operands outside the frozen closure policy."""

    value = _closure(closure)
    checked: list[str] = []
    for raw_argument in arguments:
        argument = receipts._validate_string(  # noqa: SLF001
            raw_argument, label="recovery launch argument"
        )
        operand = argument.split("=", 1)[1] if "=" in argument else argument
        if operand.startswith("@"):
            operand = operand[1:]
        if "://" in operand:
            authorize_network_endpoint(closure=closure, endpoint=operand)
        else:
            path = Path(operand)
            if path.is_absolute():
                _authorize_io_path(value, path=path)
            elif ".." in PurePosixPath(operand).parts:
                raise receipts.ReleaseControlError(
                    "recovery launch argument contains path traversal"
                )
        checked.append(argument)
    return tuple(checked)


def build_os_sandbox_arguments(
    *,
    closure: bytes,
    sandbox: Path,
    command: Sequence[str],
    declared_endpoints: Sequence[str],
) -> list[str]:
    """Build the fixed bubblewrap profile for one offline recovery command."""

    value = _closure(closure)
    if not command:
        raise receipts.ReleaseControlError("recovery sandbox command is empty")
    if declared_endpoints:
        raise receipts.ReleaseControlError(
            "recovery network sandbox has no endpoint-filtering authority"
        )
    checked_command = [
        receipts._validate_string(item, label="recovery sandbox command")  # noqa: SLF001
        for item in command
    ]
    target = Path(checked_command[0])
    external_paths = {
        Path(
            receipts._validate_string(  # noqa: SLF001
                receipts._object(  # noqa: SLF001
                    item, label="recovery external executable"
                ).get("path"),
                label="recovery external executable path",
            )
        ).resolve(strict=True)
        for item in receipts._array(  # noqa: SLF001
            value.get("external_executables"), label="recovery external executables"
        )
    }
    resolved_target = target.resolve(strict=True)
    roots = _validated_io_roots(value)
    runtime_files = _validated_runtime_files(value)
    if not any(resolved_target == root or root in resolved_target.parents for root, _ in roots):
        if resolved_target not in external_paths:
            raise receipts.ReleaseControlError(
                "recovery sandbox target is outside the execution closure"
            )
    resolved_sandbox = sandbox.resolve(strict=True)
    if resolved_sandbox not in external_paths:
        raise receipts.ReleaseControlError(
            "recovery sandbox executable is outside the execution closure"
        )

    arguments = [
        str(resolved_sandbox),
        "--die-with-parent",
        "--new-session",
        "--unshare-user",
        "--unshare-ipc",
        "--unshare-pid",
        "--unshare-uts",
        "--unshare-cgroup-try",
        "--unshare-net",
        "--proc",
        "/proc",
        "--dev",
        "/dev",
        "--tmpfs",
        "/tmp",  # nosec B108
    ]
    runtime_directories = {
        parent
        for _source, target in runtime_files
        for parent in target.parents
        if parent != Path("/")
    }
    for directory in sorted(runtime_directories, key=lambda item: (len(item.parts), str(item))):
        arguments.extend(("--dir", str(directory)))
    for root, access in roots:
        operation = "--bind" if access == "read_write" else "--ro-bind"
        arguments.extend((operation, str(root), str(root)))
    for executable in sorted(external_paths, key=str):
        containing_roots = [
            access
            for root, access in roots
            if executable == root or root in executable.parents
        ]
        if containing_roots and all(access == "read" for access in containing_roots):
            continue
        arguments.extend(("--ro-bind", str(executable), str(executable)))
    for source, target in runtime_files:
        if any(
            access == "read_write"
            and (source == root or root in source.parents)
            for root, access in roots
        ):
            arguments.extend(("--ro-bind", str(source), str(source)))
        arguments.extend(("--ro-bind", str(source), str(target)))
    environment = build_isolated_environment(closure=closure)
    if runtime_files:
        base_library_root = _verify_extracted_python_runtime(
            closure=closure,
            capsule_root=Path(
                receipts._validate_string(  # noqa: SLF001
                    receipts._array(  # noqa: SLF001
                        value.get("sys_path"), label="recovery sys.path"
                    )[0],
                    label="recovery capsule sys.path",
                )
            ),
        )
        if base_library_root is not None:
            private_roots = sorted(
                {target.parent for _source, target in runtime_files}, key=str
            )
            environment["LD_LIBRARY_PATH"] = ":".join(
                [*(str(path) for path in private_roots), str(base_library_root)]
            )
    arguments.append("--clearenv")
    for name, setting in sorted(environment.items()):
        arguments.extend(("--setenv", name, setting))
    capsule_root = Path(
        receipts._validate_string(  # noqa: SLF001
            receipts._array(value.get("sys_path"), label="recovery sys.path")[0],  # noqa: SLF001
            label="recovery capsule sys.path",
        )
    ).resolve(strict=True)
    arguments.extend(("--chdir", str(capsule_root), "--", *checked_command))
    return arguments


def materialize_candidate_from_capsule(
    *,
    closure: bytes,
    capsule_root: Path,
    destination: Path,
) -> None:
    """Extract only the candidate archive whose bytes are frozen in the closure."""

    value = _closure(closure)
    resources = _member_table(value, field="data_resources", capsule_root=capsule_root)
    archive_entry = resources.get("candidate-archive.tar")
    if archive_entry is None:
        raise receipts.ReleaseControlError(
            "recovery capsule candidate archive is absent from the closure"
        )
    archive, expected_digest = archive_entry
    expected_archive = capsule_root.resolve(strict=True) / "candidate-archive.tar"
    if archive.resolve(strict=True) != expected_archive or _member_digest(archive) != expected_digest:
        raise receipts.ReleaseControlError(
            "recovery capsule candidate archive identity mismatch"
        )
    if destination.exists() or destination.is_symlink():
        raise receipts.ReleaseControlError(
            "recovery candidate materialization destination must be absent"
        )
    if not destination.parent.is_dir() or destination.parent.is_symlink():
        raise receipts.ReleaseControlError(
            "recovery candidate materialization parent is invalid"
        )
    _authorize_io_path(value, path=destination.parent.resolve(strict=True), require_write=True)
    _extract_deterministic_archive(archive=archive, destination=destination)


def _extract_deterministic_archive(*, archive: Path, destination: Path) -> None:
    """Extract authenticated regular members without importing a checkout helper."""

    if not archive.is_file() or archive.is_symlink():
        raise receipts.ReleaseControlError("recovery candidate archive is not regular")
    if destination.exists() or destination.is_symlink():
        raise receipts.ReleaseControlError(
            "recovery candidate destination must be absent"
        )
    if not destination.parent.is_dir() or destination.parent.is_symlink():
        raise receipts.ReleaseControlError(
            "recovery candidate destination parent is invalid"
        )
    destination.mkdir(mode=0o700)
    seen: set[str] = set()
    total = 0
    with tarfile.open(archive, mode="r:") as source:
        members = source.getmembers()
        if not members or len(members) > MAX_ARCHIVE_MEMBERS:
            raise receipts.ReleaseControlError(
                "recovery candidate archive member cardinality is invalid"
            )
        for member in members:
            relative = _relative_member(
                member.name, label="recovery candidate archive member"
            )
            normalized = relative.as_posix()
            if normalized in seen:
                raise receipts.ReleaseControlError(
                    "recovery candidate archive has a duplicate member"
                )
            seen.add(normalized)
            if member.pax_headers or member.sparse is not None:
                raise receipts.ReleaseControlError(
                    "recovery candidate archive metadata is not deterministic"
                )
            if member.uid != 0 or member.gid != 0 or member.mtime != 0:
                raise receipts.ReleaseControlError(
                    "recovery candidate archive ownership or time is not deterministic"
                )
            target = destination.joinpath(*relative.parts)
            if member.isdir():
                if member.mode != 0o755:
                    raise receipts.ReleaseControlError(
                        "recovery candidate archive directory mode is invalid"
                    )
                target.mkdir(mode=0o755, parents=True, exist_ok=True)
                target.chmod(0o755)
                continue
            if not member.isreg() or member.mode != 0o644:
                raise receipts.ReleaseControlError(
                    "recovery candidate archive has a link, special file, or invalid mode"
                )
            if member.size < 0 or member.size > MAX_ARCHIVE_MEMBER_BYTES:
                raise receipts.ReleaseControlError(
                    "recovery candidate archive member size is invalid"
                )
            total += member.size
            if total > MAX_ARCHIVE_TOTAL_BYTES:
                raise receipts.ReleaseControlError(
                    "recovery candidate archive exceeds the total size limit"
                )
            target.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
            extracted = source.extractfile(member)
            if extracted is None:
                raise receipts.ReleaseControlError(
                    "recovery candidate archive member body is absent"
                )
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
            flags |= getattr(os, "O_NOFOLLOW", 0)
            descriptor = os.open(target, flags, 0o644)
            written = 0
            try:
                with os.fdopen(descriptor, "wb", closefd=True) as output:
                    while chunk := extracted.read(1024 * 1024):
                        written += len(chunk)
                        if written > member.size:
                            raise receipts.ReleaseControlError(
                                "recovery candidate archive member exceeded its size"
                            )
                        output.write(chunk)
                    if written != member.size:
                        raise receipts.ReleaseControlError(
                            "recovery candidate archive member ended early"
                        )
                    output.flush()
                    os.fsync(output.fileno())
            finally:
                extracted.close()
            target.chmod(0o644)


def inspect_isolated_python(
    executable: Path,
    *,
    closure: bytes | None = None,
    additional_library_roots: Sequence[Path] = (),
) -> tuple[list[str], dict[str, str]]:
    """Read the exact isolated target runtime and its hash-locked dependency path."""

    probe = (
        "import json,platform,sys;"
        "print(json.dumps({'implementation':platform.python_implementation(),"
        "'version':platform.python_version(),"
        "'abi':f'cp{sys.version_info.major}{sys.version_info.minor}',"
        "'sys_path':sys.path},sort_keys=True,separators=(',',':')))"
    )
    arguments = ["-I", "-B", "-c", probe]
    command = (
        [str(executable), *arguments]
        if closure is None
        else private_loader_command(
            closure=closure,
            executable=executable,
            arguments=arguments,
            additional_library_roots=additional_library_roots,
        )
    )
    completed = subprocess.run(  # noqa: S603  # nosec B603
        command,
        capture_output=True,
        check=False,
        env={"LANG": "C.UTF-8", "LC_ALL": "C.UTF-8", "PYTHONNOUSERSITE": "1"},
        timeout=10,
    )
    if completed.returncode != 0 or len(completed.stdout) > 1024 * 1024:
        raise receipts.ReleaseControlError("isolated recovery Python probe failed")
    parsed = receipts._object(  # noqa: SLF001
        receipts.parse_external_json_bytes(
            completed.stdout, label="isolated recovery Python probe"
        ),
        label="isolated recovery Python probe",
    )
    receipts._require_exact_fields(  # noqa: SLF001
        parsed,
        frozenset({"implementation", "version", "abi", "sys_path"}),
        label="isolated recovery Python probe",
    )
    paths = [
        receipts._validate_string(item, label="isolated recovery Python sys.path")  # noqa: SLF001
        for item in receipts._array(parsed.get("sys_path"), label="isolated Python sys.path")  # noqa: SLF001
    ]
    runtime = {
        field: receipts._validate_string(  # noqa: SLF001
            parsed.get(field), label=f"isolated recovery Python {field}"
        )
        for field in ("implementation", "version", "abi")
    }
    return paths, runtime


def effective_recovery_sys_path(
    *, capsule_root: Path, interpreter_sys_path: Sequence[str]
) -> list[str]:
    """Compose the exact executable path from the capsule and a real isolated probe."""

    try:
        resolved_capsule = capsule_root.resolve(strict=True)
    except OSError as exc:
        raise receipts.ReleaseControlError("recovery capsule sys.path root is missing") from exc
    if not resolved_capsule.is_dir() or capsule_root.is_symlink():
        raise receipts.ReleaseControlError("recovery capsule sys.path root is invalid")
    effective = [str(resolved_capsule)]
    observed: set[str] = set()
    for raw_entry in interpreter_sys_path:
        entry = receipts._validate_string(  # noqa: SLF001
            raw_entry, label="isolated recovery Python sys.path"
        )
        path = Path(entry)
        if not path.is_absolute():
            raise receipts.ReleaseControlError(
                "isolated recovery Python sys.path contains a relative entry"
            )
        if entry in observed:
            raise receipts.ReleaseControlError(
                "isolated recovery Python sys.path is duplicated"
            )
        observed.add(entry)
        try:
            resolved = path.resolve(strict=True)
        except OSError:
            # The effective launch path is replaced explicitly. A conventional
            # nonexistent pythonXY.zip probe entry therefore grants no authority.
            continue
        if (
            path.is_symlink()
            or str(resolved) != entry
            or (not resolved.is_dir() and not resolved.is_file())
        ):
            raise receipts.ReleaseControlError(
                "isolated recovery Python sys.path entry is not exact"
            )
        if entry in effective:
            raise receipts.ReleaseControlError(
                "isolated recovery Python sys.path duplicates the capsule"
            )
        effective.append(entry)
    return effective


def _validate_active_sys_path(*, expected: Sequence[str], active: Sequence[str]) -> None:
    if not active or len(active) != len(set(active)):
        raise receipts.ReleaseControlError("active recovery sys.path is empty or duplicated")
    for entry in active:
        if not Path(entry).is_absolute():
            raise receipts.ReleaseControlError(
                "active sys.path contains ambient paths outside the recovery closure"
            )
    if list(active) != list(expected):
        raise receipts.ReleaseControlError(
            "active recovery sys.path is not the complete ordered closure"
        )


def verify_execution_closure(
    *,
    closure: bytes,
    capsule_root: Path,
    active_sys_path: Sequence[str],
    executable_versions: Mapping[str, str] | None = None,
    active_python_runtime: Mapping[str, str] | None = None,
) -> receipts.JSONObject:
    """Verify bytes, imports, tools, paths, runtime, and lock before execution."""

    value = _closure(closure)
    if not capsule_root.is_dir() or capsule_root.is_symlink():
        raise receipts.ReleaseControlError("recovery capsule root is not a real directory")
    _authorize_io_path(value, path=capsule_root.resolve(strict=True))
    expected_sys_path = [
        receipts._validate_string(item, label="recovery sys.path entry")  # noqa: SLF001
        for item in receipts._array(value.get("sys_path"), label="recovery sys.path")  # noqa: SLF001
    ]
    _validate_active_sys_path(expected=expected_sys_path, active=active_sys_path)
    for sys_path_entry in expected_sys_path:
        _authorize_io_path(value, path=Path(sys_path_entry))
    python_members = _member_table(value, field="python_members", capsule_root=capsule_root)
    shell_helpers = _member_table(value, field="shell_helpers", capsule_root=capsule_root)
    data_resources = _member_table(value, field="data_resources", capsule_root=capsule_root)
    ignored_roots: list[Path] = []
    for raw_item in receipts._array(  # noqa: SLF001
        value.get("external_executables"), label="recovery external executables"
    ):
        item = receipts._object(raw_item, label="recovery external executable")  # noqa: SLF001
        if item.get("name") != "python":
            continue
        executable = Path(cast(str, item["path"]))
        environment_root = executable.parent.parent
        try:
            if capsule_root.resolve(strict=True) in environment_root.resolve(strict=True).parents:
                ignored_roots.append(environment_root)
        except OSError:
            pass
    _verify_member_inventory(
        capsule_root,
        python_members=python_members,
        shell_helpers=shell_helpers,
        data_resources=data_resources,
        ignored_roots=ignored_roots,
    )
    modules = {_module_name(path): path for path in python_members}
    actual_static: set[tuple[str, str, str]] = set()
    actual_dynamic: set[tuple[str, str, str]] = set()
    for member_path, (path, _) in python_members.items():
        source = path.read_bytes()
        actual_static.update(
            _local_static_imports(member_path=member_path, source=source, modules=modules)
        )
        for importer, module in _literal_dynamic_imports(member_path=member_path, source=source):
            target = modules.get(module)
            if target is None:
                raise receipts.ReleaseControlError(
                    "recovery dynamic import targets an unlisted member"
                )
            actual_dynamic.add((importer, module, target))
    expected_static = _edge_table(value, field="static_imports", members=python_members)
    expected_dynamic = _edge_table(value, field="dynamic_imports", members=python_members)
    if actual_static != expected_static:
        raise receipts.ReleaseControlError("recovery static import graph differs from the closure")
    if actual_dynamic != expected_dynamic:
        raise receipts.ReleaseControlError(
            "recovery dynamic import graph differs from the allowlist"
        )
    runtime = receipts._object(  # noqa: SLF001
        value.get("python_runtime"), label="recovery Python runtime"
    )
    actual_runtime = (
        {
            "implementation": "CPython",
            "version": f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
            "abi": f"cp{sys.version_info.major}{sys.version_info.minor}",
        }
        if active_python_runtime is None
        else dict(active_python_runtime)
    )
    if (
        runtime.get("implementation") != actual_runtime.get("implementation")
        or runtime.get("version") != actual_runtime.get("version")
        or runtime.get("abi") != actual_runtime.get("abi")
    ):
        raise receipts.ReleaseControlError("recovery Python runtime mismatch")
    versions = {} if executable_versions is None else dict(executable_versions)
    for raw_item in receipts._array(  # noqa: SLF001
        value.get("external_executables"), label="recovery external executables"
    ):
        item = receipts._object(raw_item, label="recovery external executable")  # noqa: SLF001
        name = cast(str, item["name"])
        path = resolve_external_executable(closure=closure, name=name)
        version = versions.get(str(path))
        if version is None:
            if name == "python":
                version = f"Python {actual_runtime['version']}"
            elif name == "sandbox":
                if resolve_trusted_os_sandbox(closure=closure) != path:
                    raise receipts.ReleaseControlError(
                        "recovery sandbox identity changed during closure verification"
                    )
                # Its independently frozen digest binds this exact version. The
                # first execution occurs only through private_loader_command.
                version = cast(str, item.get("version"))
            else:
                # The digest and closure membership authorize this tool, but its
                # first execution must occur inside the OS sandbox. A caller may
                # supply an independently captured version without executing it.
                continue
        if version != item.get("version"):
            raise receipts.ReleaseControlError("recovery external executable version mismatch")
    _verify_dependency_lock(value, capsule_root)
    _validated_runtime_files(value)
    _verify_extracted_python_runtime(closure=closure, capsule_root=capsule_root)
    return value


def build_host_actuator_binding(
    *,
    closure: bytes,
    capsule_root: Path,
    host_root: Path,
    host_python: Path,
    host_gh: Path,
) -> receipts.JSONObject:
    """Bind the offline authority plane to a byte-identical host actuation plane."""

    value = _closure(closure)
    if host_root.is_symlink() or not host_root.is_dir():
        raise receipts.ReleaseControlError("recovery host actuator root is invalid")
    checked_host_root = _authorize_io_path(
        value,
        path=host_root.resolve(strict=True),
    )
    members: dict[str, tuple[Path, str]] = {}
    for field in ("python_members", "shell_helpers", "data_resources"):
        for name, identity in _member_table(
            value,
            field=field,
            capsule_root=capsule_root,
        ).items():
            if name in members:
                raise receipts.ReleaseControlError(
                    "recovery host actuator closure member is duplicated"
                )
            members[name] = identity
    required_names = (
        receipts._RECOVERY_CAPSULE_SOURCE_ASSETS  # noqa: SLF001
        | receipts._RECOVERY_CAPSULE_SCHEMA_ASSETS  # noqa: SLF001
    )
    if not receipts._RECOVERY_CAPSULE_WORKFLOWS.issubset(required_names):  # noqa: SLF001
        raise receipts.ReleaseControlError(
            "recovery host actuator source lacks the frozen workflow pair"
        )
    if not required_names.issubset(members):
        raise receipts.ReleaseControlError(
            "recovery host actuator source is absent from the execution closure"
        )
    host_source: dict[str, bytes] = {}
    for name in sorted(required_names):
        capsule_path, expected_digest = members[name]
        host_path = checked_host_root.joinpath(*PurePosixPath(name).parts)
        capsule_raw = receipts._read_regular(  # noqa: SLF001
            capsule_path,
            label=f"recovery capsule actuator source {name}",
            max_bytes=receipts.MAX_SOURCE_BODY_BYTES,
        )
        host_raw = receipts._read_regular(  # noqa: SLF001
            host_path,
            label=f"recovery host actuator source {name}",
            max_bytes=receipts.MAX_SOURCE_BODY_BYTES,
        )
        if (
            receipts._sha256(capsule_raw) != expected_digest  # noqa: SLF001
            or host_raw != capsule_raw
        ):
            raise receipts.ReleaseControlError(
                "recovery host actuator source identity mismatch"
            )
        host_source[name] = host_raw

    python_items = [
        receipts._object(item, label="recovery host actuator Python")  # noqa: SLF001
        for item in receipts._array(  # noqa: SLF001
            value.get("external_executables"),
            label="recovery external executables",
        )
        if receipts._object(  # noqa: SLF001
            item, label="recovery external executable"
        ).get("name")
        == "python"
    ]
    if len(python_items) != 1:
        raise receipts.ReleaseControlError(
            "recovery host actuator Python identity is absent or ambiguous"
        )
    python_item = python_items[0]
    if (
        not host_python.is_absolute()
        or host_python.is_symlink()
        or not host_python.is_file()
        or not os.access(host_python, os.X_OK)
    ):
        raise receipts.ReleaseControlError("recovery host actuator Python path is invalid")
    host_python_raw = receipts._read_regular(  # noqa: SLF001
        host_python,
        label="recovery host actuator Python",
        max_bytes=256 * 1024 * 1024,
    )
    python_digest = receipts._sha256(host_python_raw)  # noqa: SLF001
    python_version = receipts._validate_string(  # noqa: SLF001
        python_item.get("version"), label="recovery host actuator Python version"
    )
    if python_digest != python_item.get("sha256"):
        raise receipts.ReleaseControlError("recovery host actuator Python identity mismatch")

    if (
        not host_gh.is_absolute()
        or host_gh.is_symlink()
        or not host_gh.is_file()
        or not os.access(host_gh, os.X_OK)
    ):
        raise receipts.ReleaseControlError("recovery host actuator GitHub CLI path is invalid")
    gh_raw = receipts._read_regular(  # noqa: SLF001
        host_gh,
        label="recovery host actuator GitHub CLI",
        max_bytes=256 * 1024 * 1024,
    )
    gh_digest = receipts._sha256(gh_raw)  # noqa: SLF001
    expected_gh_digest = receipts.PINNED_GH_BINARY_DIGESTS.get(
        (sys.platform, platform.machine())
    )
    if gh_digest != expected_gh_digest:
        raise receipts.ReleaseControlError("recovery host actuator GitHub CLI identity mismatch")

    manifest_raw = receipts._read_regular(  # noqa: SLF001
        capsule_root / "recovery-capsule-manifest.json",
        label="recovery host actuator capsule manifest",
        max_bytes=receipts.MAX_SOURCE_BODY_BYTES,
    )
    manifest = receipts._object(  # noqa: SLF001
        receipts.strict_canonical_json(
            manifest_raw,
            label="recovery host actuator capsule manifest",
        ),
        label="recovery host actuator capsule manifest",
    )
    candidate = receipts._object(  # noqa: SLF001
        manifest.get("candidate"), label="recovery host actuator candidate"
    )
    source_sha = receipts._validate_string(  # noqa: SLF001
        candidate.get("source_sha"), label="recovery host actuator candidate source SHA"
    )
    if receipts.GIT_SHA_RE.fullmatch(source_sha) is None:
        raise receipts.ReleaseControlError(
            "recovery host actuator candidate source SHA is invalid"
        )
    workflow_digests = [
        {"path": name, "sha256": receipts._sha256(host_source[name])}  # noqa: SLF001
        for name in sorted(receipts._RECOVERY_CAPSULE_WORKFLOWS)  # noqa: SLF001
    ]
    binding: receipts.JSONObject = {
        "schema": "kestrel.recovery_host_actuator_binding.v1",
        "candidate_source_sha": source_sha,
        "capsule_manifest_sha256": receipts._sha256(manifest_raw),  # noqa: SLF001
        "execution_closure_sha256": receipts._sha256(closure),  # noqa: SLF001
        "host_source": {
            "asset_count": len(host_source),
            "source_bundle_digest": receipts.source_bundle_digest(host_source),
            "workflow_digests": workflow_digests,
        },
        "host_python": {"sha256": python_digest, "version": python_version},
        "host_gh": {
            "sha256": gh_digest,
            "version": receipts.PINNED_GH_VERSION_LINE.decode("ascii"),
        },
        "authority_plane": {
            "name": "offline_capsule",
            "network_authority": "deny_all",
            "role": "interpret_and_verify",
        },
        "actuation_plane": {
            "name": "dispatch_pinned_host_workflow",
            "network_authority": "workflow_scoped",
            "role": "acquire_and_mutate",
        },
        "provenance": {
            "producer": "scripts/recovery_launcher.py",
            "provider": "local",
            "method": "offline-authority-host-actuator-binding",
        },
        "confidence": 1,
        "validation_status": "validated",
    }
    binding["binding_digest"] = receipts._sha256(  # noqa: SLF001
        receipts.canonical_json_bytes(binding)
    )
    receipts._validate_schema(  # noqa: SLF001
        "kestrel.recovery_host_actuator_binding.v1",
        binding,
        label="recovery host actuator binding",
    )
    return binding


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    verify = commands.add_parser("verify")
    verify.add_argument("closure")
    verify.add_argument("--capsule-root", required=True)
    materialize = commands.add_parser("materialize-candidate")
    materialize.add_argument("closure")
    materialize.add_argument("--capsule-root", required=True)
    materialize.add_argument("--destination", required=True)
    bind_host = commands.add_parser("bind-host-actuator")
    bind_host.add_argument("closure")
    bind_host.add_argument("--capsule-root", required=True)
    bind_host.add_argument("--host-root", required=True)
    bind_host.add_argument("--host-python", required=True)
    bind_host.add_argument("--host-gh", required=True)
    bind_host.add_argument("--output", required=True)
    launch = commands.add_parser("launch")
    launch.add_argument("closure")
    launch.add_argument("--capsule-root", required=True)
    launch.add_argument("--network-endpoint", action="append", default=[])
    launch.add_argument("--executable", required=True)
    launch.add_argument("arguments", nargs=argparse.REMAINDER)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    closure_path = Path(args.closure)
    closure = receipts._read_regular(  # noqa: SLF001
        closure_path, label="recovery execution closure", max_bytes=MAX_CLOSURE_BYTES
    )
    value = _closure(closure)
    try:
        python_executable = resolve_trusted_recovery_python(closure=closure)
    except receipts.ReleaseControlError as exc:
        raise receipts.ReleaseControlError(
            "recovery Python interpreter is absent from the exact closure"
        ) from exc
    capsule_root = Path(args.capsule_root)
    runtime_files = receipts._array(  # noqa: SLF001
        value.get("runtime_files"), label="recovery runtime files"
    )
    base_library_root = _verify_extracted_python_runtime(
        closure=closure, capsule_root=capsule_root
    )
    if runtime_files and base_library_root is None:
        raise receipts.ReleaseControlError("recovery Python runtime lock is absent")
    interpreter_sys_path, active_runtime = (
        inspect_isolated_python(
            python_executable,
            closure=closure,
            additional_library_roots=(base_library_root,),
        )
        if runtime_files and base_library_root is not None
        else inspect_isolated_python(python_executable)
    )
    active_sys_path = effective_recovery_sys_path(
        capsule_root=capsule_root,
        interpreter_sys_path=interpreter_sys_path,
    )
    verify_execution_closure(
        closure=closure,
        capsule_root=capsule_root,
        active_sys_path=active_sys_path,
        active_python_runtime=active_runtime,
    )
    if args.command == "materialize-candidate":
        materialize_candidate_from_capsule(
            closure=closure,
            capsule_root=capsule_root,
            destination=Path(args.destination),
        )
        return 0
    if args.command == "bind-host-actuator":
        binding = build_host_actuator_binding(
            closure=closure,
            capsule_root=capsule_root,
            host_root=Path(args.host_root),
            host_python=Path(args.host_python),
            host_gh=Path(args.host_gh),
        )
        output = Path(args.output)
        _authorize_io_path(value, path=output, require_write=True)
        if output.exists() or output.is_symlink() or not output.parent.is_dir():
            raise receipts.ReleaseControlError(
                "recovery host actuator binding output must be absent"
            )
        descriptor = os.open(
            output,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        with os.fdopen(descriptor, "wb") as target:
            target.write(receipts.canonical_json_bytes(binding))
            target.flush()
            os.fsync(target.fileno())
        return 0
    if args.command == "verify":
        return 0
    declared_endpoints = cast(list[str], args.network_endpoint)
    if declared_endpoints != sorted(set(declared_endpoints)):
        raise receipts.ReleaseControlError(
            "declared recovery network endpoints are not sorted unique"
        )
    for endpoint in declared_endpoints:
        authorize_network_endpoint(closure=closure, endpoint=endpoint)
    raw_arguments = cast(list[str], args.arguments)
    authorize_launch_arguments(closure=closure, arguments=raw_arguments)
    executable_name = cast(str, args.executable)
    executable = resolve_external_executable(closure=closure, name=executable_name)
    if executable_name == "python":
        if raw_arguments[:1] == ["--"]:
            raw_arguments = raw_arguments[1:]
        if not raw_arguments:
            raise receipts.ReleaseControlError("isolated recovery Python target is missing")
        target = Path(raw_arguments[0])
        resolved_target = _authorize_io_path(value, path=target)
        members = _member_table(
            value,
            field="python_members",
            capsule_root=capsule_root,
        )
        if resolved_target not in {path.resolve(strict=True) for path, _digest in members.values()}:
            raise receipts.ReleaseControlError(
                "isolated recovery Python target is absent from the closure"
            )
        arguments = [
            str(executable),
            "-I",
            "-S",
            "-B",
            "-c",
            ISOLATED_PYTHON_BOOTSTRAP,
            json.dumps(value["sys_path"], separators=(",", ":")),
            *raw_arguments,
        ]
    else:
        arguments = [str(executable), *raw_arguments]
    if executable_name == "sandbox":
        raise receipts.ReleaseControlError("recovery sandbox cannot be its own target")
    sandbox = resolve_trusted_os_sandbox(closure=closure)
    if sandbox == executable:
        raise receipts.ReleaseControlError("recovery sandbox and target must be distinct")
    environment = build_isolated_environment(closure=closure)
    sandbox_arguments = build_os_sandbox_arguments(
        closure=closure,
        sandbox=sandbox,
        command=arguments,
        declared_endpoints=declared_endpoints,
    )
    if runtime_files:
        private_command = private_loader_command(
            closure=closure,
            executable=sandbox,
            arguments=sandbox_arguments[1:],
        )
        os.execve(private_command[0], private_command, environment)  # nosec B606
    os.execve(str(sandbox), sandbox_arguments, environment)  # nosec B606
    return 1  # pragma: no cover


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
