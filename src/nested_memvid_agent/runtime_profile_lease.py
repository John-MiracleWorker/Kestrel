from __future__ import annotations

import ctypes
import errno
import json
import os
import secrets
import shlex
import subprocess  # nosec B404
import sys
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from importlib import metadata as importlib_metadata
from pathlib import Path
from threading import Lock
from typing import IO, Any, Literal, Protocol
from urllib.parse import urlparse

from .file_lock import lock_exclusive, unlock
from .private_artifacts import (
    ensure_owner_only_directory,
    open_private_file_descriptor,
    read_private_text,
    write_private_text,
)

RUNTIME_PROFILE_LEASE_SCHEMA = "kestrel.runtime_profile_lease.v1"
LeaseManagement = Literal["desktop", "cli"]
LeaseDisposition = Literal[
    "available",
    "attach_desktop",
    "offer_desktop_takeover",
    "version_conflict",
    "stale_unverified",
    "foreign_or_unrelated",
]
_LEASE_LOCK_NAME = "runtime-profile.lock"
_LEASE_METADATA_NAME = "runtime-profile.json"
_LEASE_CONTROL_DIRECTORY_NAME = ".kestrel-runtime-profiles"
_LEASE_KEYS = frozenset(
    {
        "schema",
        "profile_id",
        "management",
        "owner_digest",
        "pid",
        "process_birth_marker",
        "executable_digest",
        "launch_nonce_digest",
        "base_url",
        "version",
        "created_at",
    }
)


@dataclass(frozen=True)
class LeaseProcessSnapshot:
    pid: int
    owner_digest: str
    process_birth_marker: str
    executable_digest: str


class LeaseProcessInspector(Protocol):
    def __call__(self, pid: int) -> LeaseProcessSnapshot | None: ...


@dataclass(frozen=True)
class RuntimeLeaseIdentity:
    profile_id: str
    management: LeaseManagement
    owner_digest: str
    pid: int
    process_birth_marker: str
    executable_digest: str
    launch_nonce_digest: str
    base_url: str
    version: str
    created_at: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "profile_id", _required_text(self.profile_id, "profile_id"))
        if self.management not in {"desktop", "cli"}:
            raise ValueError("management must be desktop or cli")
        for field_name in (
            "owner_digest",
            "executable_digest",
            "launch_nonce_digest",
        ):
            object.__setattr__(
                self,
                field_name,
                _sha256_digest(getattr(self, field_name), field_name),
            )
        if isinstance(self.pid, bool) or not isinstance(self.pid, int) or self.pid <= 0:
            raise ValueError("pid must be a positive integer")
        object.__setattr__(
            self,
            "process_birth_marker",
            _required_text(self.process_birth_marker, "process_birth_marker"),
        )
        object.__setattr__(self, "base_url", _loopback_base_url(self.base_url))
        object.__setattr__(self, "version", _required_text(self.version, "version"))
        created_at = _required_text(self.created_at, "created_at")
        try:
            parsed = datetime.fromisoformat(created_at)
        except ValueError as exc:
            raise ValueError("created_at must be an ISO-8601 timestamp") from exc
        if parsed.tzinfo is None:
            raise ValueError("created_at must include a timezone")
        object.__setattr__(self, "created_at", created_at)

    def to_payload(self) -> dict[str, object]:
        return {
            "schema": RUNTIME_PROFILE_LEASE_SCHEMA,
            "profile_id": self.profile_id,
            "management": self.management,
            "owner_digest": self.owner_digest,
            "pid": self.pid,
            "process_birth_marker": self.process_birth_marker,
            "executable_digest": self.executable_digest,
            "launch_nonce_digest": self.launch_nonce_digest,
            "base_url": self.base_url,
            "version": self.version,
            "created_at": self.created_at,
        }


@dataclass(frozen=True)
class RuntimeLeaseState:
    status: LeaseDisposition
    current: RuntimeLeaseIdentity | None = None
    can_terminate: bool = False
    detail: str | None = None


class RuntimeLeaseConflict(RuntimeError):
    def __init__(self, state: RuntimeLeaseState) -> None:
        self.state = state
        self.current = state.current
        self.disposition = state.status
        super().__init__(state.status)


class RuntimeProfileLease:
    """One OS-locked writer lease with redacted JSON evidence beside it."""

    def __init__(
        self,
        profile_root: Path,
        identity: RuntimeLeaseIdentity,
        handle: IO[str],
    ) -> None:
        self.profile_root = Path(profile_root)
        self.identity = identity
        self.lock_path = runtime_profile_lock_path(profile_root)
        self.metadata_path = runtime_profile_metadata_path(profile_root)
        self._guard = Lock()
        self._handle: IO[str] | None = handle

    @classmethod
    def acquire(
        cls,
        profile_root: Path,
        identity: RuntimeLeaseIdentity,
        *,
        inspector: LeaseProcessInspector | None = None,
    ) -> RuntimeProfileLease:
        root = Path(profile_root).expanduser().resolve(strict=False)
        ensure_owner_only_directory(root)
        descriptor = open_private_file_descriptor(runtime_profile_lock_path(root))
        try:
            handle = os.fdopen(descriptor, "r+", encoding="utf-8")
        except BaseException:
            os.close(descriptor)
            raise
        try:
            lock_exclusive(handle, blocking=False)
        except OSError as exc:
            handle.close()
            if not _is_lock_contention(exc):
                raise
            state = cls._inspect_busy(
                root,
                profile_id=identity.profile_id,
                version=identity.version,
                inspector=inspector,
            )
            raise RuntimeLeaseConflict(state) from exc
        except BaseException:
            handle.close()
            raise

        try:
            write_private_text(
                runtime_profile_metadata_path(root),
                json.dumps(identity.to_payload(), sort_keys=True, separators=(",", ":")),
            )
        except BaseException:
            try:
                unlock(handle)
            finally:
                handle.close()
            raise
        return cls(root, identity, handle)

    @classmethod
    def inspect(
        cls,
        profile_root: Path,
        *,
        profile_id: str = "default",
        version: str | None = None,
        inspector: LeaseProcessInspector | None = None,
    ) -> RuntimeLeaseState:
        root = Path(profile_root).expanduser().resolve(strict=False)
        lock_path = runtime_profile_lock_path(root)
        metadata_path = runtime_profile_metadata_path(root)
        if not lock_path.exists():
            current = _read_metadata(metadata_path)
            if current is None and not metadata_path.exists():
                return RuntimeLeaseState(status="available")
            return RuntimeLeaseState(
                status="stale_unverified",
                current=current,
                detail="lease_metadata_without_os_lock",
            )

        descriptor = open_private_file_descriptor(lock_path)
        try:
            handle = os.fdopen(descriptor, "r+", encoding="utf-8")
        except BaseException:
            os.close(descriptor)
            raise
        try:
            try:
                lock_exclusive(handle, blocking=False)
            except OSError as exc:
                if not _is_lock_contention(exc):
                    raise
                return cls._inspect_busy(
                    root,
                    profile_id=profile_id,
                    version=version,
                    inspector=inspector,
                )
            current = _read_metadata(metadata_path)
            if current is None and not metadata_path.exists():
                return RuntimeLeaseState(status="available")
            return RuntimeLeaseState(
                status="stale_unverified",
                current=current,
                detail="lease_metadata_is_not_backed_by_os_lock",
            )
        finally:
            try:
                unlock(handle)
            except OSError:
                pass
            handle.close()

    @classmethod
    def _inspect_busy(
        cls,
        profile_root: Path,
        *,
        profile_id: str,
        version: str | None,
        inspector: LeaseProcessInspector | None,
    ) -> RuntimeLeaseState:
        del cls
        current = _read_metadata(runtime_profile_metadata_path(profile_root))
        if current is None:
            return RuntimeLeaseState(
                status="foreign_or_unrelated",
                detail="busy_os_lock_without_verifiable_metadata",
            )
        process_inspector = inspector or inspect_lease_process
        try:
            process = process_inspector(current.pid)
        except (OSError, RuntimeError, ValueError):
            process = None
        if process is None or not _process_matches(current, process):
            return RuntimeLeaseState(
                status="foreign_or_unrelated",
                current=current,
                detail="busy_os_lock_process_identity_unverified",
            )
        if current.profile_id != _required_text(profile_id, "profile_id"):
            return RuntimeLeaseState(
                status="foreign_or_unrelated",
                current=current,
                detail="profile_id_mismatch",
            )
        expected_version = version or _package_version()
        if current.version != expected_version:
            return RuntimeLeaseState(
                status="version_conflict",
                current=current,
                detail="runtime_version_mismatch",
            )
        disposition: LeaseDisposition = (
            "attach_desktop"
            if current.management == "desktop"
            else "offer_desktop_takeover"
        )
        return RuntimeLeaseState(status=disposition, current=current)

    def release(self) -> None:
        with self._guard:
            handle = self._handle
            if handle is None:
                return
            self._handle = None
            try:
                current = _read_metadata(self.metadata_path)
                if current == self.identity:
                    try:
                        self.metadata_path.unlink()
                    except FileNotFoundError:
                        pass
                unlock(handle)
            finally:
                handle.close()


def runtime_profile_lock_path(profile_root: Path) -> Path:
    return Path(profile_root) / _LEASE_LOCK_NAME


def runtime_profile_metadata_path(profile_root: Path) -> Path:
    return Path(profile_root) / _LEASE_METADATA_NAME


def resolve_runtime_profile_root(
    state_path: Path,
    memory_dir: Path,
    *,
    profile_id: str = "default",
) -> Path:
    canonical_state = Path(state_path).expanduser().resolve(strict=False)
    canonical_memory = Path(memory_dir).expanduser().resolve(strict=False)
    identity = json.dumps(
        {
            "schema": "kestrel.runtime_profile_control.v1",
            "profile_id": _required_text(profile_id, "profile_id"),
            "state_path": str(canonical_state),
            "memory_dir": str(canonical_memory),
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    identity_digest = sha256(identity.encode("utf-8")).hexdigest()
    return (
        canonical_state.parent
        / _LEASE_CONTROL_DIRECTORY_NAME
        / identity_digest
    )


def current_runtime_lease_identity(
    *,
    profile_id: str,
    management: LeaseManagement,
    base_url: str,
    launch_nonce: str | None = None,
    version: str | None = None,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> RuntimeLeaseIdentity:
    snapshot = inspect_lease_process(os.getpid())
    if snapshot is None:
        raise RuntimeError("current_process_identity_unavailable")
    nonce = launch_nonce or secrets.token_urlsafe(32)
    return RuntimeLeaseIdentity(
        profile_id=profile_id,
        management=management,
        owner_digest=snapshot.owner_digest,
        pid=snapshot.pid,
        process_birth_marker=snapshot.process_birth_marker,
        executable_digest=snapshot.executable_digest,
        launch_nonce_digest=sha256(nonce.encode("utf-8")).hexdigest(),
        base_url=base_url,
        version=version or _package_version(),
        created_at=clock().astimezone(UTC).isoformat(),
    )


def inspect_lease_process(pid: int) -> LeaseProcessSnapshot | None:
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        return None
    if os.name == "nt":
        details = _windows_process_details(pid)
    else:
        details = _posix_process_details(pid)
    if details is None:
        return None
    owner_identifier, birth_marker, executable = details
    try:
        executable_digest = _digest_file(executable)
    except OSError:
        return None
    return LeaseProcessSnapshot(
        pid=pid,
        owner_digest=sha256(owner_identifier.encode("utf-8")).hexdigest(),
        process_birth_marker=birth_marker,
        executable_digest=executable_digest,
    )


def _read_metadata(path: Path) -> RuntimeLeaseIdentity | None:
    try:
        text = read_private_text(path, missing_ok=True)
    except (OSError, PermissionError, ValueError):
        return None
    if text is None:
        return None
    try:
        raw = json.loads(text)
    except json.JSONDecodeError:
        return None
    if not isinstance(raw, dict) or frozenset(raw) != _LEASE_KEYS:
        return None
    if raw.get("schema") != RUNTIME_PROFILE_LEASE_SCHEMA:
        return None
    try:
        return RuntimeLeaseIdentity(
            profile_id=raw["profile_id"],
            management=raw["management"],
            owner_digest=raw["owner_digest"],
            pid=raw["pid"],
            process_birth_marker=raw["process_birth_marker"],
            executable_digest=raw["executable_digest"],
            launch_nonce_digest=raw["launch_nonce_digest"],
            base_url=raw["base_url"],
            version=raw["version"],
            created_at=raw["created_at"],
        )
    except (KeyError, TypeError, ValueError):
        return None


def _process_matches(
    identity: RuntimeLeaseIdentity,
    process: LeaseProcessSnapshot,
) -> bool:
    return (
        process.pid == identity.pid
        and secrets.compare_digest(process.owner_digest, identity.owner_digest)
        and secrets.compare_digest(
            process.process_birth_marker,
            identity.process_birth_marker,
        )
        and secrets.compare_digest(
            process.executable_digest,
            identity.executable_digest,
        )
    )


def _posix_process_details(pid: int) -> tuple[str, str, Path] | None:
    if sys.platform == "darwin":
        return _darwin_process_details(pid)
    proc_root = Path("/proc") / str(pid)
    try:
        status = (proc_root / "status").read_text(encoding="utf-8")
        owner_line = next(
            line for line in status.splitlines() if line.startswith("Uid:")
        )
        owner = f"uid:{int(owner_line.split()[1])}"
        stat_text = (proc_root / "stat").read_text(encoding="utf-8")
        tail = stat_text[stat_text.rfind(")") + 2 :].split()
        birth_marker = f"proc-start-ticks:{tail[19]}"
        executable = Path(os.readlink(proc_root / "exe")).resolve()
        return owner, birth_marker, executable
    except (FileNotFoundError, OSError, StopIteration, ValueError, IndexError):
        pass

    result = subprocess.run(
        ["ps", "-ww", "-p", str(pid), "-o", "uid=", "-o", "lstart=", "-o", "command="],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0 or not result.stdout.strip():
        return None
    parts = result.stdout.strip().split(None, 6)
    if len(parts) != 7 or not parts[0].isdigit():
        return None
    command = parts[6]
    try:
        executable_text = shlex.split(command)[0]
    except (ValueError, IndexError):
        return None
    executable = Path(executable_text)
    if not executable.is_absolute():
        return None
    birth_marker = "ps-lstart:" + " ".join(parts[1:6])
    return f"uid:{int(parts[0])}", birth_marker, executable.resolve()


def _darwin_process_details(pid: int) -> tuple[str, str, Path] | None:
    class ProcBsdInfo(ctypes.Structure):
        _fields_ = [
            ("pbi_flags", ctypes.c_uint32),
            ("pbi_status", ctypes.c_uint32),
            ("pbi_xstatus", ctypes.c_uint32),
            ("pbi_pid", ctypes.c_uint32),
            ("pbi_ppid", ctypes.c_uint32),
            ("pbi_uid", ctypes.c_uint32),
            ("pbi_gid", ctypes.c_uint32),
            ("pbi_ruid", ctypes.c_uint32),
            ("pbi_rgid", ctypes.c_uint32),
            ("pbi_svuid", ctypes.c_uint32),
            ("pbi_svgid", ctypes.c_uint32),
            ("rfu_1", ctypes.c_uint32),
            ("pbi_comm", ctypes.c_char * 16),
            ("pbi_name", ctypes.c_char * 32),
            ("pbi_nfiles", ctypes.c_uint32),
            ("pbi_pgid", ctypes.c_uint32),
            ("pbi_pjobc", ctypes.c_uint32),
            ("e_tdev", ctypes.c_uint32),
            ("e_tpgid", ctypes.c_uint32),
            ("pbi_nice", ctypes.c_int32),
            ("pbi_start_tvsec", ctypes.c_uint64),
            ("pbi_start_tvusec", ctypes.c_uint64),
        ]

    try:
        libproc = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
    except OSError:
        return None
    proc_pidinfo = libproc.proc_pidinfo
    proc_pidinfo.argtypes = [
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_uint64,
        ctypes.c_void_p,
        ctypes.c_int,
    ]
    proc_pidinfo.restype = ctypes.c_int
    info = ProcBsdInfo()
    received = proc_pidinfo(
        pid,
        3,
        0,
        ctypes.byref(info),
        ctypes.sizeof(info),
    )
    if received != ctypes.sizeof(info):
        return None
    if (
        info.pbi_pid != pid
        or info.pbi_start_tvsec <= 0
        or not 0 <= info.pbi_start_tvusec < 1_000_000
    ):
        return None
    proc_pidpath = libproc.proc_pidpath
    proc_pidpath.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32]
    proc_pidpath.restype = ctypes.c_int
    path_buffer = ctypes.create_string_buffer(4096)
    path_length = proc_pidpath(pid, path_buffer, len(path_buffer))
    if path_length <= 0:
        return None
    try:
        executable = Path(path_buffer.value.decode("utf-8")).resolve()
    except UnicodeDecodeError:
        return None
    return (
        f"uid:{int(info.pbi_uid)}",
        (
            "darwin-proc-start:"
            f"{int(info.pbi_start_tvsec)}:{int(info.pbi_start_tvusec):06d}"
        ),
        executable,
    )


def _windows_process_details(pid: int) -> tuple[str, str, Path] | None:
    from ctypes import wintypes

    ctypes_module: Any = __import__("ctypes")
    kernel32 = ctypes_module.WinDLL("kernel32", use_last_error=True)
    advapi32 = ctypes_module.WinDLL("advapi32", use_last_error=True)
    open_process = kernel32.OpenProcess
    open_process.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    open_process.restype = wintypes.HANDLE
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = [wintypes.HANDLE]
    close_handle.restype = wintypes.BOOL
    process = open_process(0x1000, False, pid)
    if not process:
        return None
    token = wintypes.HANDLE()
    try:
        get_process_times = kernel32.GetProcessTimes
        get_process_times.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(wintypes.FILETIME),
            ctypes.POINTER(wintypes.FILETIME),
            ctypes.POINTER(wintypes.FILETIME),
            ctypes.POINTER(wintypes.FILETIME),
        ]
        get_process_times.restype = wintypes.BOOL
        creation = wintypes.FILETIME()
        exit_time = wintypes.FILETIME()
        kernel = wintypes.FILETIME()
        user = wintypes.FILETIME()
        if not get_process_times(
            process,
            ctypes.byref(creation),
            ctypes.byref(exit_time),
            ctypes.byref(kernel),
            ctypes.byref(user),
        ):
            return None
        birth_value = (creation.dwHighDateTime << 32) | creation.dwLowDateTime
        size = wintypes.DWORD(32768)
        buffer = ctypes.create_unicode_buffer(size.value)
        query_image = kernel32.QueryFullProcessImageNameW
        query_image.argtypes = [
            wintypes.HANDLE,
            wintypes.DWORD,
            wintypes.LPWSTR,
            ctypes.POINTER(wintypes.DWORD),
        ]
        query_image.restype = wintypes.BOOL
        if not query_image(
            process,
            0,
            buffer,
            ctypes.byref(size),
        ):
            return None
        open_token = advapi32.OpenProcessToken
        open_token.argtypes = [
            wintypes.HANDLE,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.HANDLE),
        ]
        open_token.restype = wintypes.BOOL
        if not open_token(
            process,
            0x0008,
            ctypes.byref(token),
        ):
            return None
        get_token_information = advapi32.GetTokenInformation
        get_token_information.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
        ]
        get_token_information.restype = wintypes.BOOL
        required = wintypes.DWORD()
        get_token_information(
            token,
            1,
            None,
            0,
            ctypes.byref(required),
        )
        if required.value <= 0:
            return None
        token_user = ctypes.create_string_buffer(required.value)
        if not get_token_information(
            token,
            1,
            token_user,
            required,
            ctypes.byref(required),
        ):
            return None
        sid_pointer = ctypes.cast(token_user, ctypes.POINTER(ctypes.c_void_p))[0]
        sid_text = wintypes.LPWSTR()
        convert_sid = advapi32.ConvertSidToStringSidW
        convert_sid.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(wintypes.LPWSTR),
        ]
        convert_sid.restype = wintypes.BOOL
        if not convert_sid(
            sid_pointer,
            ctypes.byref(sid_text),
        ):
            return None
        try:
            owner = f"sid:{sid_text.value}"
        finally:
            local_free = kernel32.LocalFree
            local_free.argtypes = [ctypes.c_void_p]
            local_free.restype = ctypes.c_void_p
            local_free(sid_text)
        return owner, f"filetime:{birth_value}", Path(buffer.value).resolve()
    finally:
        if token:
            close_handle(token)
        close_handle(process)


def _digest_file(path: Path) -> str:
    digest = sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _package_version() -> str:
    try:
        return importlib_metadata.version("nested-memvid-agent")
    except importlib_metadata.PackageNotFoundError:
        return "0.5.0"


def _required_text(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")
    return value.strip()


def _sha256_digest(value: object, field_name: str) -> str:
    text = _required_text(value, field_name).lower()
    if len(text) != 64 or any(character not in "0123456789abcdef" for character in text):
        raise ValueError(f"{field_name} must be a SHA-256 digest")
    return text


def _loopback_base_url(value: object) -> str:
    text = _required_text(value, "base_url")
    parsed = urlparse(text)
    if (
        parsed.scheme != "http"
        or parsed.hostname not in {"127.0.0.1", "::1", "localhost"}
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("base_url must be an HTTP loopback URL")
    if parsed.port is None:
        raise ValueError("base_url must include a port")
    return text.rstrip("/") + "/"


def _is_lock_contention(error: OSError) -> bool:
    return (
        isinstance(error, BlockingIOError)
        or error.errno
        in {
            errno.EACCES,
            errno.EAGAIN,
            errno.EDEADLK,
            errno.EWOULDBLOCK,
        }
        or getattr(error, "winerror", None) in {32, 33}
        or (os.name == "nt" and error.errno in {32, 33})
    )
