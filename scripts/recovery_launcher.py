#!/usr/bin/env python3
"""Verify and launch only the executable closure frozen in a recovery capsule."""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
import platform
import subprocess
import sys
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
ISOLATED_PYTHON_BOOTSTRAP = (
    "import json,runpy,sys;"
    "sys.path[:]=json.loads(sys.argv.pop(1));"
    "target=sys.argv.pop(1);"
    "runpy.run_path(target,run_name='__main__')"
)
SANDBOX_POLICY_ENV = "KESTREL_RECOVERY_SANDBOX_POLICY"
SANDBOX_PROTOCOL = "kestrel-recovery-sandbox-v1"
TRUSTED_OS_SANDBOX_IDENTITIES: Mapping[tuple[str, str], frozenset[tuple[str, str]]] = {}


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


def _regular_member(root: Path, relative: PurePosixPath, *, label: str) -> Path:
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
        or path.stat().st_size > MAX_MEMBER_BYTES
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
        if path.suffix == ".sh":
            actual_shell.add(relative)
    if actual_python != set(python_members):
        raise receipts.ReleaseControlError(
            "recovery Python member inventory has missing or extra files"
        )
    if actual_shell != set(shell_helpers):
        raise receipts.ReleaseControlError(
            "recovery shell helper inventory has missing or extra files"
        )
    resource_parents = {PurePosixPath(path).parent.as_posix() for path in data_resources}
    actual_resources: set[str] = set()
    for parent in resource_parents:
        directory = capsule_root.joinpath(*PurePosixPath(parent).parts)
        if not directory.is_dir() or directory.is_symlink():
            raise receipts.ReleaseControlError("recovery data resource directory is missing")
        for path in directory.rglob("*"):
            if path.is_file() and not path.is_symlink():
                actual_resources.add(path.relative_to(capsule_root).as_posix())
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


def inspect_isolated_python(executable: Path) -> tuple[list[str], dict[str, str]]:
    """Read sys.path and runtime identity from the exact isolated target interpreter."""

    probe = (
        "import json,platform,sys;"
        "print(json.dumps({'implementation':platform.python_implementation(),"
        "'version':platform.python_version(),"
        "'abi':f'cp{sys.version_info.major}{sys.version_info.minor}',"
        "'sys_path':sys.path},sort_keys=True,separators=(',',':')))"
    )
    completed = subprocess.run(  # noqa: S603  # nosec B603
        [str(executable), "-I", "-S", "-c", probe],
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


def _validate_active_sys_path(*, expected: Sequence[str], active: Sequence[str]) -> None:
    if not active or len(active) != len(set(active)):
        raise receipts.ReleaseControlError("active recovery sys.path is empty or duplicated")
    positions: list[int] = []
    for entry in active:
        if not Path(entry).is_absolute() or entry not in expected:
            raise receipts.ReleaseControlError(
                "active sys.path contains ambient paths outside the recovery closure"
            )
        positions.append(expected.index(entry))
    if positions != sorted(positions):
        raise receipts.ReleaseControlError(
            "active recovery sys.path order differs from the closure"
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
            result = subprocess.run(
                [str(path), "--version"],
                check=False,
                capture_output=True,
                text=True,
                env=build_isolated_environment(closure=closure),
                timeout=10,
            )
            if result.returncode != 0:
                raise receipts.ReleaseControlError(
                    "recovery external executable version check failed"
                )
            version = (result.stdout or result.stderr).splitlines()[0]
        if version != item.get("version"):
            raise receipts.ReleaseControlError("recovery external executable version mismatch")
    _verify_dependency_lock(value, capsule_root)
    return value


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    verify = commands.add_parser("verify")
    verify.add_argument("closure")
    verify.add_argument("--capsule-root", required=True)
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
        python_executable = resolve_external_executable(closure=closure, name="python")
    except receipts.ReleaseControlError as exc:
        raise receipts.ReleaseControlError(
            "recovery Python interpreter is absent from the exact closure"
        ) from exc
    active_sys_path, active_runtime = inspect_isolated_python(python_executable)
    verify_execution_closure(
        closure=closure,
        capsule_root=Path(args.capsule_root),
        active_sys_path=active_sys_path,
        active_python_runtime=active_runtime,
    )
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
            capsule_root=Path(args.capsule_root),
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
    policy: receipts.JSONObject = {
        "schema": "kestrel.recovery_sandbox_policy.v1",
        "protocol": SANDBOX_PROTOCOL,
        "capsule_root": str(Path(args.capsule_root).resolve(strict=True)),
        "target": {
            "name": executable_name,
            "path": str(executable),
            "arguments": cast(list[receipts.JSONValue], arguments[1:]),
        },
        "io_roots": value["io_roots"],
        "network_policy": {
            "default_deny": True,
            "allowed_endpoints": cast(list[receipts.JSONValue], declared_endpoints),
        },
    }
    environment = build_isolated_environment(closure=closure)
    environment[SANDBOX_POLICY_ENV] = receipts.canonical_json_bytes(policy).decode("ascii")
    sandbox_arguments = [
        str(sandbox),
        "--protocol",
        SANDBOX_PROTOCOL,
        "--policy-env",
        SANDBOX_POLICY_ENV,
        "--",
        *arguments,
    ]
    os.execve(str(sandbox), sandbox_arguments, environment)
    return 1  # pragma: no cover


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
