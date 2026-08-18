#!/usr/bin/env python3
"""Stage the exact offline dependency closure for a recovery capsule."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import posixpath
import re
import shutil
import subprocess  # nosec B404
import sys
import tarfile
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast
from urllib.parse import urlsplit
from urllib.request import urlopen

BWRAP_PACKAGE_URL = (
    "https://archive.ubuntu.com/ubuntu/pool/main/b/bubblewrap/"
    "bubblewrap_0.9.0-1ubuntu0.1_amd64.deb"
)
BWRAP_PACKAGE_SHA256 = (
    "1b506492bd9c7fd0cdb4f02ac822f1d3e336b0aead5113c1239baf8db5db562a"
)
BWRAP_BINARY_SHA256 = (
    "52231e1caf55bcbc667b269f49c63599a6f7db4767ae6a039580d0ff853db712"
)
BWRAP_VERSION = "bubblewrap 0.9.0"
RECOVERY_PYTHON_VERSION = "3.11.14"
RECOVERY_PYTHON_ABI = "cp311"
RECOVERY_PYTHON_PACKAGE_URL = (
    "https://github.com/actions/python-versions/releases/download/"
    "3.11.14-18393181605/python-3.11.14-linux-24.04-x64.tar.gz"
)
RECOVERY_PYTHON_PACKAGE_SHA256 = (
    "295c25eeb4fdad1ec9526a27fbd9b476d7c79b00547d74d809b306381d0796d5"
)
RECOVERY_PYTHON_BINARY_SHA256 = (
    "dcd2d22a91c5adb37fa3f54a3a16d2ed7616b84931eb606c0fdfbca38395dab8"
)
RECOVERY_WHEEL_PLATFORM = "manylinux2014_x86_64"
RECOVERY_RUNTIME_PLATFORM = "ubuntu-24.04-x86_64"
PYPI_SIMPLE_INDEX_URL = "https://pypi.org/simple"
MAX_PACKAGE_BYTES = 1024 * 1024
MAX_REQUIREMENTS_BYTES = 1024 * 1024
MAX_WHEEL_BYTES = 256 * 1024 * 1024
MAX_RUNTIME_FILE_BYTES = 256 * 1024 * 1024
MAX_RUNTIME_TOTAL_BYTES = 512 * 1024 * 1024
MAX_RUNTIME_FILES = 512
MAX_PYTHON_PACKAGE_BYTES = 256 * 1024 * 1024
MAX_PYTHON_RUNTIME_FILE_BYTES = 512 * 1024 * 1024
MAX_PYTHON_RUNTIME_TOTAL_BYTES = 1024 * 1024 * 1024
MAX_PYTHON_RUNTIME_FILES = 32768

Fetch = Callable[[str], bytes]
ExtractBwrap = Callable[[Path, Path], Path]
DownloadWheels = Callable[[Path, Path], None]
BuildPythonRuntime = Callable[
    [Path, Path, str], tuple[dict[str, object], Path, Path]
]
CollectRuntimeFiles = Callable[
    [Path, Path, Path, Path, Path], list[tuple[str, Path]]
]


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode()


def _fetch_bounded(url: str) -> bytes:
    parsed = urlsplit(url)
    if (
        parsed.scheme != "https"
        or parsed.netloc != "archive.ubuntu.com"
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
    ):
        raise ValueError("recovery dependency URL is not the pinned Ubuntu HTTPS origin")
    # The exact HTTPS origin and redirect policy are validated around this call.
    with urlopen(url, timeout=30) as response:  # nosec B310
        if response.geturl() != url:
            raise ValueError("recovery dependency download redirected away from the pinned URL")
        package = response.read(MAX_PACKAGE_BYTES + 1)
    if not package or len(package) > MAX_PACKAGE_BYTES:
        raise ValueError("recovery dependency package size is invalid")
    return package


def _fetch_python_bounded(url: str) -> bytes:
    if url != RECOVERY_PYTHON_PACKAGE_URL:
        raise ValueError("recovery Python URL is not the frozen actions/python-versions asset")
    # The exact asset URL is fixed and every returned byte is hash pinned.
    with urlopen(url, timeout=60) as response:  # nosec B310
        final = urlsplit(response.geturl())
        if (
            final.scheme != "https"
            or final.hostname
            not in {"github.com", "release-assets.githubusercontent.com"}
            or final.username is not None
            or final.password is not None
            or final.fragment
        ):
            raise ValueError("recovery Python download left the pinned GitHub asset origin")
        package = response.read(MAX_PYTHON_PACKAGE_BYTES + 1)
    if not package or len(package) > MAX_PYTHON_PACKAGE_BYTES:
        raise ValueError("recovery Python package size is invalid")
    return package


def _python_runtime_member_name(name: str) -> str:
    normalized = name[2:] if name.startswith("./") else name
    path = Path(normalized)
    if (
        not normalized
        or normalized.startswith("/")
        or "\\" in normalized
        or "\x00" in normalized
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise ValueError("recovery Python source archive member path is unsafe")
    return path.as_posix()


def _python_runtime_tree_identity(root: Path) -> tuple[int, int, str]:
    if root.is_symlink() or not root.is_dir():
        raise ValueError("recovery Python runtime tree root is unsafe")
    records: list[dict[str, object]] = []
    total = 0
    for path in sorted(root.rglob("*"), key=lambda item: item.relative_to(root).as_posix()):
        if path.is_symlink():
            raise ValueError("recovery Python runtime tree contains a link")
        if path.is_dir():
            continue
        if not path.is_file():
            raise ValueError("recovery Python runtime tree contains a special file")
        relative = path.relative_to(root).as_posix()
        size = path.stat().st_size
        if size < 0 or size > MAX_PYTHON_RUNTIME_FILE_BYTES:
            raise ValueError("recovery Python runtime file size is invalid")
        total += size
        if total > MAX_PYTHON_RUNTIME_TOTAL_BYTES or len(records) >= MAX_PYTHON_RUNTIME_FILES:
            raise ValueError("recovery Python runtime tree is too large")
        mode = 0o755 if path.stat().st_mode & 0o111 else 0o644
        records.append(
            {
                "mode": f"{mode:04o}",
                "path": relative,
                "sha256": "sha256:" + _sha256_path(path),
                "size_bytes": size,
            }
        )
    if not records:
        raise ValueError("recovery Python runtime tree is empty")
    return len(records), total, "sha256:" + _sha256_bytes(_canonical_bytes(records))


def _build_python_runtime_archive(
    source_archive: Path,
    work_root: Path,
    expected_python_sha256: str = RECOVERY_PYTHON_BINARY_SHA256,
) -> tuple[dict[str, object], Path, Path]:
    """Derive a link-free, deterministic bin+lib Python runtime from a pinned asset."""

    if source_archive.is_symlink() or not source_archive.is_file():
        raise ValueError("recovery Python source archive is unsafe")
    if work_root.exists() or work_root.is_symlink():
        raise ValueError("recovery Python runtime work root must be absent")
    work_root.mkdir(mode=0o700)
    runtime_root = work_root / "python-base"
    runtime_root.mkdir(mode=0o700)
    members: dict[str, tarfile.TarInfo] = {}
    payloads: dict[str, Path] = {}
    payload_root = work_root / "source-payloads"
    payload_root.mkdir(mode=0o700)
    with tarfile.open(source_archive, mode="r|gz") as source:
        for member in source:
            name = _python_runtime_member_name(member.name)
            if name in members:
                raise ValueError("recovery Python source archive has duplicate members")
            members[name] = member
            if not (name == "bin/python3.11" or name.startswith("lib/")):
                continue
            if member.isreg():
                extracted = source.extractfile(member)
                if extracted is None:
                    raise ValueError("recovery Python source member has no body")
                payload = payload_root / hashlib.sha256(name.encode()).hexdigest()
                flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
                descriptor = os.open(payload, flags, 0o600)
                written = 0
                with os.fdopen(descriptor, "wb") as output:
                    while chunk := extracted.read(1024 * 1024):
                        written += len(chunk)
                        if written > member.size or written > MAX_PYTHON_RUNTIME_FILE_BYTES:
                            raise ValueError("recovery Python source member size is invalid")
                        output.write(chunk)
                extracted.close()
                if written != member.size:
                    raise ValueError("recovery Python source member size is invalid")
                payloads[name] = payload

    selected = sorted(
        name
        for name, member in members.items()
        if (name == "bin/python3.11" or name.startswith("lib/"))
        and not member.isdir()
    )
    if not selected or len(selected) > MAX_PYTHON_RUNTIME_FILES:
        raise ValueError("recovery Python source runtime inventory is invalid")

    def body(name: str, trail: frozenset[str] = frozenset()) -> tuple[Path, int]:
        if name in trail:
            raise ValueError("recovery Python source archive link cycle")
        member = members.get(name)
        if member is None:
            raise ValueError("recovery Python source archive link target is absent")
        if member.isreg():
            payload = payloads.get(name)
            if payload is None:
                raise ValueError("recovery Python source member payload is absent")
            return payload, member.mode
        if member.issym() or member.islnk():
            target = member.linkname
            if member.issym():
                target = posixpath.join(posixpath.dirname(name), target)
            normalized = posixpath.normpath(target)
            if (
                normalized.startswith("/")
                or normalized == ".."
                or normalized.startswith("../")
            ):
                raise ValueError("recovery Python source archive link escapes the runtime")
            return body(_python_runtime_member_name(normalized), trail | {name})
        raise ValueError("recovery Python source runtime contains a special member")

    total = 0
    for name in selected:
        payload, source_mode = body(name)
        size = payload.stat().st_size
        total += size
        if total > MAX_PYTHON_RUNTIME_TOTAL_BYTES:
            raise ValueError("recovery Python source runtime is too large")
        target = runtime_root.joinpath(*Path(name).parts)
        target.parent.mkdir(parents=True, mode=0o755, exist_ok=True)
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(target, flags, 0o600)
        with os.fdopen(descriptor, "wb") as output, payload.open("rb") as source:
            shutil.copyfileobj(source, output, length=1024 * 1024)
        target.chmod(0o755 if source_mode & 0o111 else 0o644)

    python = runtime_root / "bin" / "python3.11"
    if _sha256_path(python) != expected_python_sha256:
        raise ValueError("recovery Python runtime executable digest mismatch")
    file_count, total_size, tree_digest = _python_runtime_tree_identity(runtime_root)
    archive_path = work_root / "python-runtime.tar.gz"
    with archive_path.open("xb") as raw_output:
        with gzip.GzipFile(
            filename="", mode="wb", fileobj=raw_output, compresslevel=9, mtime=0
        ) as compressed:
            with tarfile.open(
                fileobj=compressed, mode="w|", format=tarfile.GNU_FORMAT
            ) as archive:
                directories = {
                    parent
                    for path in runtime_root.rglob("*")
                    if path.is_file()
                    for parent in path.relative_to(runtime_root).parents
                    if parent != Path(".")
                }
                for directory in sorted(directories, key=lambda item: item.as_posix()):
                    info = tarfile.TarInfo(directory.as_posix())
                    info.type = tarfile.DIRTYPE
                    info.mode = 0o755
                    info.uid = info.gid = info.mtime = 0
                    archive.addfile(info)
                for path in sorted(
                    (item for item in runtime_root.rglob("*") if item.is_file()),
                    key=lambda item: item.relative_to(runtime_root).as_posix(),
                ):
                    info = tarfile.TarInfo(path.relative_to(runtime_root).as_posix())
                    info.mode = 0o755 if path.stat().st_mode & 0o111 else 0o644
                    info.uid = info.gid = info.mtime = 0
                    info.size = path.stat().st_size
                    with path.open("rb") as source:
                        archive.addfile(info, source)
    archive_size = archive_path.stat().st_size
    if archive_size <= 0 or archive_size > MAX_PYTHON_PACKAGE_BYTES:
        raise ValueError("recovery Python runtime archive size is invalid")
    manifest = {
        "schema": "kestrel.recovery_python_runtime.v1",
        "platform": RECOVERY_RUNTIME_PLATFORM,
        "python_version": RECOVERY_PYTHON_VERSION,
        "python_abi": RECOVERY_PYTHON_ABI,
        "python_executable_path": "bin/python3.11",
        "python_executable_sha256": "sha256:" + expected_python_sha256,
        "source_archive_url": RECOVERY_PYTHON_PACKAGE_URL,
        "source_archive_sha256": "sha256:" + _sha256_path(source_archive),
        "runtime_archive_path": "recovery/python-runtime.tar.gz",
        "runtime_archive_sha256": "sha256:" + _sha256_path(archive_path),
        "runtime_archive_size_bytes": archive_size,
        "runtime_tree_sha256": tree_digest,
        "runtime_file_count": file_count,
        "runtime_total_size_bytes": total_size,
    }
    return manifest, archive_path, runtime_root


def _extract_bwrap(package_path: Path, destination: Path) -> Path:
    dpkg_deb = shutil.which("dpkg-deb")
    if dpkg_deb is None:
        raise ValueError("dpkg-deb is required to stage the pinned bubblewrap package")
    completed = subprocess.run(  # noqa: S603  # nosec B603
        [dpkg_deb, "--extract", str(package_path), str(destination)],
        check=False,
        capture_output=True,
        env={"LANG": "C.UTF-8", "LC_ALL": "C.UTF-8", "PATH": os.environ.get("PATH", "")},
        text=True,
    )
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip()
        raise ValueError(f"pinned bubblewrap package extraction failed: {detail}")
    return destination / "usr" / "bin" / "bwrap"


def _download_wheels(requirements: Path, wheelhouse: Path) -> None:
    environment = {
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PATH": os.environ.get("PATH", ""),
        "PIP_DISABLE_PIP_VERSION_CHECK": "1",
        "PYTHONNOUSERSITE": "1",
    }
    completed = subprocess.run(  # noqa: S603  # nosec B603
        [
            sys.executable,
            "-I",
            "-m",
            "pip",
            "--isolated",
            "download",
            "--disable-pip-version-check",
            f"--index-url={PYPI_SIMPLE_INDEX_URL}",
            "--require-hashes",
            "--only-binary=:all:",
            f"--platform={RECOVERY_WHEEL_PLATFORM}",
            "--python-version=3.11",
            "--implementation=cp",
            "--abi=cp311",
            "--dest",
            str(wheelhouse),
            "--requirement",
            str(requirements),
        ],
        check=False,
        capture_output=True,
        env=environment,
        text=True,
    )
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip()
        raise ValueError(f"hash-locked recovery wheel download failed: {detail}")


def _runtime_environment(*, library_path: Path | None = None) -> dict[str, str]:
    environment = {
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PATH": os.environ.get("PATH", ""),
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONNOUSERSITE": "1",
    }
    if library_path is not None:
        environment["LD_LIBRARY_PATH"] = str(library_path)
    return environment


def _is_elf(path: Path) -> bool:
    try:
        with path.open("rb") as handle:
            return handle.read(4) == b"\x7fELF"
    except OSError:
        return False


def _regular_elf_files(roots: list[Path]) -> list[Path]:
    files: set[Path] = set()
    for root in roots:
        if root.is_symlink() or not root.is_dir():
            continue
        for directory, child_directories, child_files in os.walk(root, followlinks=False):
            child_directories[:] = sorted(child_directories)
            directory_path = Path(directory)
            for name in sorted(child_files):
                candidate = directory_path / name
                if candidate.is_symlink() or not candidate.is_file() or not _is_elf(candidate):
                    continue
                files.add(candidate.resolve(strict=True))
                if len(files) > 4096:
                    raise ValueError("recovery runtime ELF inventory is too large")
    return sorted(files, key=str)


def _ldd_dependencies(
    executable: Path, *, ldd: Path, library_path: Path | None = None
) -> list[tuple[str, Path]]:
    completed = subprocess.run(  # noqa: S603  # nosec B603
        [str(ldd), str(executable)],
        check=False,
        capture_output=True,
        env=_runtime_environment(library_path=library_path),
        text=True,
        timeout=30,
    )
    combined = "\n".join(part for part in (completed.stdout, completed.stderr) if part)
    if "not found" in combined:
        raise ValueError("recovery runtime dependency resolution is incomplete")
    if completed.returncode != 0:
        lowered = combined.strip().lower()
        if lowered in {"not a dynamic executable", "statically linked"}:
            return []
        raise ValueError("recovery runtime dependency inspection failed")
    dependencies: list[tuple[str, Path]] = []
    for raw_line in completed.stdout.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("linux-vdso.so"):
            continue
        if "=>" in line:
            target = line.split("=>", 1)[1].strip().split(None, 1)[0]
        else:
            target = line.split(None, 1)[0]
        if not target.startswith("/"):
            raise ValueError("recovery runtime dependency output is not an absolute path")
        sandbox_path = Path(target)
        try:
            resolved = sandbox_path.resolve(strict=True)
        except OSError as exc:
            raise ValueError("recovery runtime dependency is missing") from exc
        if not resolved.is_file() or not _is_elf(resolved):
            raise ValueError("recovery runtime dependency is not a regular ELF file")
        dependencies.append((target, resolved))
    return dependencies


def _collect_runtime_files(
    requirements: Path,
    wheelhouse: Path,
    work_root: Path,
    bwrap: Path,
    python_root: Path,
) -> list[tuple[str, Path]]:
    if sys.platform != "linux" or os.uname().machine != "x86_64":
        raise ValueError("recovery runtime staging requires Ubuntu 24.04 x86_64")
    release = Path("/etc/os-release").read_text(encoding="utf-8")
    if 'ID=ubuntu' not in release or 'VERSION_ID="24.04"' not in release:
        raise ValueError("recovery runtime staging host is not Ubuntu 24.04")
    if (
        f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
        != RECOVERY_PYTHON_VERSION
        or _sha256_path(Path(sys.executable).resolve(strict=True))
        != RECOVERY_PYTHON_BINARY_SHA256
    ):
        raise ValueError("recovery runtime staging Python identity mismatch")
    ldd_name = shutil.which("ldd")
    if ldd_name is None:
        raise ValueError("ldd is required to stage the recovery runtime")
    ldd = Path(ldd_name).resolve(strict=True)
    if bwrap.is_symlink() or not bwrap.is_file():
        raise ValueError("recovery runtime staging bubblewrap input is unsafe")
    if python_root.is_symlink() or not python_root.is_dir():
        raise ValueError("recovery runtime staging Python root is unsafe")
    base_python = python_root / "bin" / "python3.11"
    base_library = python_root / "lib"
    if (
        base_python.is_symlink()
        or not base_python.is_file()
        or _sha256_path(base_python) != RECOVERY_PYTHON_BINARY_SHA256
        or base_library.is_symlink()
        or not base_library.is_dir()
    ):
        raise ValueError("recovery runtime staging Python tree identity mismatch")
    venv = work_root / "runtime-probe"
    environment = _runtime_environment(library_path=base_library)
    commands = (
        [str(base_python), "-I", "-S", "-B", "-m", "venv", "--copies", str(venv)],
        [
            str(venv / "bin" / "python"),
            "-I",
            "-B",
            "-m",
            "pip",
            "--isolated",
            "install",
            "--no-compile",
            "--no-index",
            "--find-links",
            str(wheelhouse),
            "--require-hashes",
            "--only-binary=:all:",
            "-r",
            str(requirements),
        ],
        [
            str(venv / "bin" / "python"),
            "-I",
            "-B",
            "-m",
            "pip",
            "--isolated",
            "check",
        ],
    )
    for command in commands:
        completed = subprocess.run(  # noqa: S603  # nosec B603
            command,
            check=False,
            capture_output=True,
            env=environment,
            text=True,
            timeout=300,
        )
        if completed.returncode != 0:
            raise ValueError("recovery runtime probe environment creation failed")
    probe = (
        "import json,sys;"
        "print(json.dumps({'base_prefix':sys.base_prefix,'sys_path':sys.path},"
        "sort_keys=True,separators=(',',':')))"
    )
    completed = subprocess.run(  # noqa: S603  # nosec B603
        [str(venv / "bin" / "python"), "-I", "-B", "-c", probe],
        check=False,
        capture_output=True,
        env=environment,
        text=True,
        timeout=30,
    )
    if completed.returncode != 0:
        raise ValueError("recovery runtime Python path probe failed")
    try:
        observed = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise ValueError("recovery runtime Python path probe was invalid") from exc
    if (
        type(observed) is not dict
        or set(observed) != {"base_prefix", "sys_path"}
        or type(observed.get("base_prefix")) is not str
        or type(observed.get("sys_path")) is not list
    ):
        raise ValueError("recovery runtime Python path probe fields mismatch")
    base_prefix = Path(cast(str, observed["base_prefix"])).resolve(strict=True)
    sys_path = [
        candidate.resolve(strict=True)
        for item in cast(list[object], observed["sys_path"])
        if type(item) is str
        and item
        and (candidate := Path(item)).is_dir()
        and not candidate.is_symlink()
    ]
    if not sys_path:
        raise ValueError("recovery runtime Python path probe is empty")
    venv_root = venv.resolve(strict=True)
    if base_prefix != python_root.resolve(strict=True):
        raise ValueError("recovery runtime probe escaped the staged Python root")
    scan_roots = [base_library, *sys_path]
    elf_files = _regular_elf_files(scan_roots)
    elf_files.extend(
        [
            base_python.resolve(strict=True),
            (venv / "bin" / "python").resolve(strict=True),
            bwrap.resolve(strict=True),
        ]
    )
    external: dict[str, Path] = {}
    queue = sorted(set(elf_files), key=str)
    inspected: set[Path] = set()
    while queue:
        elf = queue.pop(0)
        if elf in inspected:
            continue
        inspected.add(elf)
        for sandbox_path, source in _ldd_dependencies(
            elf, ldd=ldd, library_path=base_library
        ):
            if source == venv_root or venv_root in source.parents:
                continue
            if source == python_root or python_root in source.parents:
                continue
            prior = external.setdefault(sandbox_path, source)
            if prior != source:
                raise ValueError("recovery runtime dependency path is ambiguous")
            if source not in inspected:
                queue.append(source)
        if len(external) > MAX_RUNTIME_FILES or len(inspected) > 8192:
            raise ValueError("recovery runtime dependency inventory is too large")
    names = {Path(path).name for path in external}
    if not any(name.startswith("ld-linux-") for name in names):
        raise ValueError("recovery runtime dynamic loader is absent")
    if len(external) > MAX_RUNTIME_FILES:
        raise ValueError("recovery runtime dependency inventory is too large")
    return sorted(external.items())


def _runtime_manifest(
    runtime_files: list[tuple[str, Path]],
) -> tuple[dict[str, object], list[tuple[str, Path]]]:
    entries: list[dict[str, object]] = []
    staged: list[tuple[str, Path]] = []
    total = 0
    previous = ""
    basenames: set[str] = set()
    for sandbox_path, source in sorted(runtime_files):
        path = Path(sandbox_path)
        if (
            not path.is_absolute()
            or path.as_posix() != sandbox_path
            or any(part in {".", ".."} for part in path.parts)
            or sandbox_path <= previous
            or sandbox_path.startswith(("/dev/", "/proc/", "/sys/", "/tmp/"))  # nosec B108
            or source.is_symlink()
            or not source.is_file()
        ):
            raise ValueError("recovery runtime dependency inventory is unsafe")
        size = source.stat().st_size
        if size <= 0 or size > MAX_RUNTIME_FILE_BYTES:
            raise ValueError("recovery runtime dependency size is invalid")
        total += size
        if total > MAX_RUNTIME_TOTAL_BYTES or len(entries) >= MAX_RUNTIME_FILES:
            raise ValueError("recovery runtime dependency inventory is too large")
        basename = path.name
        if (
            re.fullmatch(r"[A-Za-z0-9._+-]+", basename) is None
            or basename in basenames
        ):
            raise ValueError("recovery runtime dependency basename collision is ambiguous")
        basenames.add(basename)
        asset_path = f"recovery/runtime/{basename}"
        digest = "sha256:" + _sha256_path(source)
        entries.append(
            {
                "asset_path": asset_path,
                "sandbox_path": sandbox_path,
                "sha256": digest,
                "size_bytes": size,
            }
        )
        staged.append((asset_path, source))
        previous = sandbox_path
    if not entries:
        raise ValueError("recovery runtime dependency inventory is empty")
    return (
        {
            "schema": "kestrel.recovery_runtime.v1",
            "platform": RECOVERY_RUNTIME_PLATFORM,
            "python_version": RECOVERY_PYTHON_VERSION,
            "python_executable_sha256": "sha256:" + RECOVERY_PYTHON_BINARY_SHA256,
            "files": entries,
        },
        staged,
    )


def _write_exclusive(path: Path, value: bytes, *, mode: int) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags, mode)  # lgtm[py/overly-permissive-file] — staged recovery dependency: 0o644 is the required world-readable archive contract (secrets stay 0o600)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = -1
            handle.write(value)
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        if descriptor >= 0:
            os.close(descriptor)
        path.unlink(missing_ok=True)
        raise
    path.chmod(mode)


def _requirements_bytes(source_root: Path) -> bytes:
    path = source_root / "config" / "recovery-requirements.txt"
    if path.is_symlink() or not path.is_file():
        raise ValueError("recovery requirements lock is missing or unsafe")
    value = path.read_bytes()
    if not value or len(value) > MAX_REQUIREMENTS_BYTES:
        raise ValueError("recovery requirements lock size is invalid")
    logical = [
        line.strip()
        for line in value.decode("utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    if not logical or not any("--hash=sha256:" in line for line in logical):
        raise ValueError("recovery requirements lock is not hash locked")
    return value


def _verified_bwrap(path: Path, expected_digest: str) -> bytes:
    if path.is_symlink() or not path.is_file():
        raise ValueError("staged bubblewrap binary is missing or unsafe")
    value = path.read_bytes()
    if _sha256_bytes(value) != expected_digest:
        raise ValueError("staged bubblewrap binary digest mismatch")
    completed = subprocess.run(  # noqa: S603  # nosec B603
        [str(path), "--version"],
        check=False,
        capture_output=True,
        env={"LANG": "C.UTF-8", "LC_ALL": "C.UTF-8"},
        text=True,
    )
    if completed.returncode != 0 or completed.stdout.strip() != BWRAP_VERSION:
        raise ValueError("staged bubblewrap binary version mismatch")
    return value


def _wheel_manifest(wheelhouse: Path) -> tuple[dict[str, object], list[Path]]:
    entries = sorted(wheelhouse.iterdir(), key=lambda path: path.name)
    if not entries:
        raise ValueError("recovery wheelhouse is empty")
    if any(
        path.is_symlink()
        or not path.is_file()
        or not path.name.endswith(".whl")
        or path.stat().st_size <= 0
        or path.stat().st_size > MAX_WHEEL_BYTES
        for path in entries
    ):
        raise ValueError("recovery wheelhouse contains an unsafe or non-wheel entry")
    return (
        {
            "schema": "kestrel.recovery_wheelhouse.v1",
            "wheels": [
                {
                    "filename": path.name,
                    "sha256": "sha256:" + _sha256_path(path),
                    "size_bytes": path.stat().st_size,
                }
                for path in entries
            ],
        },
        entries,
    )


def stage_recovery_dependencies(
    *,
    source_root: Path,
    output_root: Path,
    source_sha: str,
    fetch: Fetch = _fetch_bounded,
    fetch_python: Fetch = _fetch_python_bounded,
    extract_bwrap: ExtractBwrap = _extract_bwrap,
    download_wheels: DownloadWheels = _download_wheels,
    build_python_runtime: BuildPythonRuntime = _build_python_runtime_archive,
    collect_runtime_files: CollectRuntimeFiles = _collect_runtime_files,
    expected_bwrap_package_sha256: str = BWRAP_PACKAGE_SHA256,
    expected_bwrap_binary_sha256: str = BWRAP_BINARY_SHA256,
    expected_python_package_sha256: str = RECOVERY_PYTHON_PACKAGE_SHA256,
    expected_python_binary_sha256: str = RECOVERY_PYTHON_BINARY_SHA256,
) -> dict[str, Any]:
    if re.fullmatch(r"[0-9a-f]{40}", source_sha) is None:
        raise ValueError("recovery dependency source SHA is invalid")
    source_root = source_root.expanduser()
    output_root = output_root.expanduser()
    if source_root.is_symlink():
        raise ValueError("recovery dependency source root is unsafe")
    source_root = source_root.resolve(strict=True)
    if not source_root.is_dir():
        raise ValueError("recovery dependency source root is unsafe")
    if output_root.is_symlink() or output_root.exists():
        raise ValueError("recovery dependency output root must be absent")
    output_root = output_root.resolve(strict=False)
    parent = output_root.parent.resolve(strict=True)
    requirements = _requirements_bytes(source_root)
    package = fetch(BWRAP_PACKAGE_URL)
    if _sha256_bytes(package) != expected_bwrap_package_sha256:
        raise ValueError("pinned bubblewrap package digest mismatch")
    python_package = fetch_python(RECOVERY_PYTHON_PACKAGE_URL)
    if _sha256_bytes(python_package) != expected_python_package_sha256:
        raise ValueError("pinned recovery Python package digest mismatch")

    with tempfile.TemporaryDirectory(prefix=".kestrel-recovery-stage-", dir=parent) as temporary:
        temporary_root = Path(temporary)
        package_path = temporary_root / "bubblewrap.deb"
        package_path.write_bytes(package)
        extracted = temporary_root / "extracted"
        extracted.mkdir(mode=0o700)
        bwrap_path = extract_bwrap(package_path, extracted)
        bwrap = _verified_bwrap(bwrap_path, expected_bwrap_binary_sha256)
        python_package_path = temporary_root / "python.tar.gz"
        python_package_path.write_bytes(python_package)
        python_manifest, python_archive, python_root = build_python_runtime(
            python_package_path,
            temporary_root / "python-runtime-build",
            expected_python_binary_sha256,
        )
        wheelhouse = temporary_root / "wheelhouse"
        wheelhouse.mkdir(mode=0o700)
        requirements_path = temporary_root / "requirements.txt"
        requirements_path.write_bytes(requirements)
        download_wheels(requirements_path, wheelhouse)
        manifest, wheels = _wheel_manifest(wheelhouse)
        runtime_manifest, runtime_files = _runtime_manifest(
            collect_runtime_files(
                requirements_path,
                wheelhouse,
                temporary_root,
                bwrap_path,
                python_root,
            )
        )

        staged = temporary_root / "published"
        recovery = staged / "recovery"
        (recovery / "bin").mkdir(parents=True, mode=0o755)
        (recovery / "runtime").mkdir(mode=0o755)
        (recovery / "wheelhouse").mkdir(mode=0o755)
        _write_exclusive(recovery / "bin" / "bwrap", bwrap, mode=0o755)
        _write_exclusive(recovery / "requirements.txt", requirements, mode=0o644)
        python_manifest_raw = _canonical_bytes(python_manifest)
        _write_exclusive(
            recovery / "python-runtime-manifest.json", python_manifest_raw, mode=0o644
        )
        _write_exclusive(
            recovery / "python-runtime.tar.gz", python_archive.read_bytes(), mode=0o644
        )
        manifest_raw = _canonical_bytes(manifest)
        _write_exclusive(
            recovery / "wheelhouse-manifest.json", manifest_raw, mode=0o644
        )
        runtime_manifest_raw = _canonical_bytes(runtime_manifest)
        _write_exclusive(
            recovery / "runtime-manifest.json", runtime_manifest_raw, mode=0o644
        )
        for wheel in wheels:
            _write_exclusive(
                recovery / "wheelhouse" / wheel.name,
                wheel.read_bytes(),
                mode=0o644,
            )
        runtime_identities = {
            cast(str, item["asset_path"]): (
                cast(str, item["sha256"]),
                cast(int, item["size_bytes"]),
            )
            for item in cast(list[dict[str, object]], runtime_manifest["files"])
        }
        for asset_path, source in runtime_files:
            relative = Path(asset_path).relative_to("recovery")
            raw = source.read_bytes()
            expected_digest, expected_size = runtime_identities[asset_path]
            if len(raw) != expected_size or "sha256:" + _sha256_bytes(raw) != expected_digest:
                raise ValueError("recovery runtime dependency changed during staging")
            _write_exclusive(recovery / relative, raw, mode=0o644)
        receipt: dict[str, Any] = {
            "schema": "kestrel.recovery_dependency_staging.v1",
            "inputs": {
                "bubblewrap_package_url": BWRAP_PACKAGE_URL,
                "bubblewrap_package_sha256": "sha256:"
                + expected_bwrap_package_sha256,
                "requirements_sha256": "sha256:" + _sha256_bytes(requirements),
                "python_package_url": RECOVERY_PYTHON_PACKAGE_URL,
                "python_package_sha256": "sha256:" + expected_python_package_sha256,
                "python_version": RECOVERY_PYTHON_VERSION,
                "python_abi": RECOVERY_PYTHON_ABI,
                "wheel_platform": RECOVERY_WHEEL_PLATFORM,
                "source_sha": source_sha,
            },
            "outputs": {
                "bubblewrap_sha256": "sha256:" + _sha256_bytes(bwrap),
                "bubblewrap_version": BWRAP_VERSION,
                "wheelhouse_manifest_sha256": "sha256:"
                + _sha256_bytes(manifest_raw),
                "wheel_count": len(wheels),
                "runtime_manifest_sha256": "sha256:"
                + _sha256_bytes(runtime_manifest_raw),
                "runtime_file_count": len(runtime_files),
                "python_runtime_manifest_sha256": "sha256:"
                + _sha256_bytes(python_manifest_raw),
                "python_runtime_archive_sha256": cast(
                    str, python_manifest["runtime_archive_sha256"]
                ),
            },
            "provenance": {
                "method": "checksum-pinned-recovery-dependency-staging",
                "producer": "scripts/stage_recovery_dependencies.py",
                "provider": "github.com+archive.ubuntu.com+pypi.org",
            },
            "confidence": 1,
            "validation_status": "validated",
        }
        receipt["receipt_digest"] = "sha256:" + _sha256_bytes(
            _canonical_bytes(receipt)
        )
        _write_exclusive(
            recovery / "dependency-staging-receipt.json",
            _canonical_bytes(receipt),
            mode=0o644,
        )
        staged.replace(output_root)
    return receipt


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path, default=Path.cwd())
    parser.add_argument("--source-sha", required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    args = parser.parse_args()
    try:
        report = stage_recovery_dependencies(
            source_root=args.source_root,
            output_root=args.output_root,
            source_sha=args.source_sha,
        )
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        parser.error(str(exc))
    print(json.dumps(report, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
