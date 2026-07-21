from __future__ import annotations

import ctypes
import os
import re
import secrets
import shutil
import stat
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from ctypes import wintypes
from pathlib import Path
from typing import Any

_SDDL_REVISION_1 = 1
_TOKEN_QUERY = 0x0008
_TOKEN_USER_CLASS = 1
_ERROR_INSUFFICIENT_BUFFER = 122
_ERROR_ALREADY_EXISTS = 183
_SE_FILE_OBJECT = 1
_OWNER_SECURITY_INFORMATION = 0x00000001
_DACL_SECURITY_INFORMATION = 0x00000004
_PROTECTED_DACL_SECURITY_INFORMATION = 0x80000000
_WINDOWS_REPARSE_POINT = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x0400)
_WINDOWS_TEMP_ATTEMPTS = 32
_WINDOWS_SYSTEM_SID = "S-1-5-18"
_WINDOWS_ADMINISTRATORS_SID = "S-1-5-32-544"
_WINDOWS_SID_ALIASES = {
    "SY": _WINDOWS_SYSTEM_SID,
    "LS": "S-1-5-19",
    "NS": "S-1-5-20",
    "BA": _WINDOWS_ADMINISTRATORS_SID,
}


class PrivateDirectoryError(OSError):
    """Raised when an owner-private temporary directory cannot be proven."""


class _SecurityAttributes(ctypes.Structure):
    _fields_ = [
        ("nLength", wintypes.DWORD),
        ("lpSecurityDescriptor", wintypes.LPVOID),
        ("bInheritHandle", wintypes.BOOL),
    ]


class _SidAndAttributes(ctypes.Structure):
    _fields_ = [
        ("Sid", wintypes.LPVOID),
        ("Attributes", wintypes.DWORD),
    ]


class _TokenUser(ctypes.Structure):
    _fields_ = [("User", _SidAndAttributes)]


@contextmanager
def private_temporary_directory(
    *,
    prefix: str,
    parent: Path | None = None,
) -> Iterator[Path]:
    """Create a temporary directory private to the current OS account.

    CPython did not make ``mkdir(mode=0o700)`` owner-private on Windows until
    3.13.  Kestrel supports older interpreters, so native Windows creates the
    leaf atomically with a protected inheritable DACL instead of materializing
    sensitive snapshots in a directory with inherited permissions.
    """

    if not _is_windows():
        # TemporaryDirectory's POSIX cleanup deliberately recovers owner
        # permissions before retrying.  Extension snapshots are hardened to
        # 0500/0400 before launch, so a plain shutil.rmtree cannot reliably
        # remove them when the context exits.
        with tempfile.TemporaryDirectory(prefix=prefix, dir=parent) as temp_name:
            root = Path(temp_name)
            validate_owner_private_directory(root)
            yield root
        return

    root = create_owner_private_temporary_directory(prefix=prefix, parent=parent)
    try:
        yield root
    finally:
        _remove_private_tree(root)


def create_owner_private_temporary_directory(
    *,
    prefix: str,
    parent: Path | None = None,
) -> Path:
    """Create a uniquely named owner-private directory for managed cleanup."""

    if _is_windows():
        return _create_windows_private_temp_directory(prefix=prefix, parent=parent)
    root = Path(tempfile.mkdtemp(prefix=prefix, dir=parent))
    try:
        validate_owner_private_directory(root)
    except BaseException:
        try:
            root.rmdir()
        except BaseException:
            pass
        raise
    return root


def create_owner_private_directory(path: Path) -> Path:
    """Atomically create one directory with the strongest native boundary."""

    target = Path(path)
    if _is_windows():
        current_sid = _windows_current_user_sid()
        _windows_create_directory_with_sddl(
            target,
            _windows_private_directory_sddl(current_sid),
        )
    else:
        target.mkdir(mode=0o700)
    try:
        validate_owner_private_directory(target)
    except BaseException:
        try:
            target.rmdir()
        except BaseException:
            pass
        raise
    return target


def harden_empty_owner_private_directory(path: Path) -> None:
    """Harden an existing empty directory without trusting existing children.

    Non-empty trees are deliberately rejected.  Applying a private ACL only to
    their root could leave previously inherited weak ACLs below that root.
    """

    target = Path(path)
    try:
        before = target.lstat()
    except OSError as exc:
        raise PrivateDirectoryError("private_directory_unavailable") from exc
    if _is_link_or_reparse(before) or not stat.S_ISDIR(before.st_mode):
        raise PrivateDirectoryError("private_directory_not_real")
    if _directory_has_entries(target):
        raise PrivateDirectoryError("private_directory_not_empty")

    if _is_windows():
        current_sid = _windows_current_user_sid()
        if not _windows_sddl_sid_matches(
            _windows_sddl_owner(_windows_directory_sddl(target)),
            current_sid,
        ):
            raise PrivateDirectoryError("private_directory_wrong_windows_owner")
        _windows_apply_private_directory_sddl(
            target,
            _windows_private_directory_sddl(current_sid),
        )
    else:
        target.chmod(0o700)

    after = target.lstat()
    if not _same_identity(before, after) or _is_link_or_reparse(after):
        raise PrivateDirectoryError("private_directory_identity_changed")
    if _directory_has_entries(target):
        raise PrivateDirectoryError("private_directory_harden_race")
    validate_owner_private_directory(target)


def validate_owner_private_directory(path: Path) -> None:
    """Validate the exact directory using the native permission model."""

    try:
        before = path.lstat()
    except OSError as exc:
        raise PrivateDirectoryError("private_directory_unavailable") from exc
    if _is_link_or_reparse(before) or not stat.S_ISDIR(before.st_mode):
        raise PrivateDirectoryError("private_directory_not_real")

    if _is_windows():
        current_sid = _windows_current_user_sid()
        _validate_windows_private_sddl(
            _windows_directory_sddl(path),
            current_sid=current_sid,
        )
    else:
        path.chmod(0o700)
        current = path.lstat()
        if not _same_identity(before, current) or _is_link_or_reparse(current):
            raise PrivateDirectoryError("private_directory_identity_changed")
        if stat.S_IMODE(current.st_mode) & 0o077:
            raise PrivateDirectoryError("private_directory_not_owner_only")
        getuid = getattr(os, "getuid", None)
        if callable(getuid) and current.st_uid != getuid():
            raise PrivateDirectoryError("private_directory_not_owned")

    after = path.lstat()
    if not _same_identity(before, after) or _is_link_or_reparse(after):
        raise PrivateDirectoryError("private_directory_identity_changed")


def _is_windows() -> bool:
    return os.name == "nt"


def _create_windows_private_temp_directory(
    *,
    prefix: str,
    parent: Path | None = None,
) -> Path:
    selected_parent = Path(tempfile.gettempdir()) if parent is None else Path(parent)
    selected_metadata = selected_parent.lstat()
    if _is_link_or_reparse(selected_metadata) or not stat.S_ISDIR(
        selected_metadata.st_mode
    ):
        raise PrivateDirectoryError("private_temp_parent_not_real")
    parent_root = selected_parent.resolve(strict=True)
    parent_metadata = parent_root.lstat()
    if _is_link_or_reparse(parent_metadata) or not stat.S_ISDIR(
        parent_metadata.st_mode
    ) or not _same_identity(selected_metadata, parent_metadata):
        raise PrivateDirectoryError("private_temp_parent_not_real")

    for _ in range(_WINDOWS_TEMP_ATTEMPTS):
        candidate = parent_root / f"{prefix}{secrets.token_hex(16)}"
        try:
            create_owner_private_directory(candidate)
        except FileExistsError:
            continue
        return candidate
    raise PrivateDirectoryError("private_temp_name_exhausted")


def _windows_private_directory_sddl(current_sid: str) -> str:
    trustees: list[str] = []
    normalized: set[str] = set()
    for trustee in (current_sid, "SY", "BA"):
        identity = _normalize_sddl_sid(trustee)
        if identity in normalized:
            continue
        normalized.add(identity)
        trustees.append(trustee)
    aces = "".join(f"(A;OICI;FA;;;{trustee})" for trustee in trustees)
    return f"O:{current_sid}D:P{aces}"


def _validate_windows_private_sddl(sddl: str, *, current_sid: str) -> None:
    if not _windows_sddl_sid_matches(_windows_sddl_owner(sddl), current_sid):
        raise PrivateDirectoryError("private_directory_wrong_windows_owner")

    dacl_match = re.search(r"D:(.*?)(?=S:|$)", sddl)
    if dacl_match is None:
        raise PrivateDirectoryError("private_directory_missing_windows_dacl")
    dacl = dacl_match.group(1)
    dacl_flags = dacl.split("(", 1)[0]
    if "P" not in dacl_flags:
        raise PrivateDirectoryError("private_directory_windows_dacl_inherited")

    expected = [
        _normalize_sddl_sid(current_sid),
        _WINDOWS_SYSTEM_SID,
        _WINDOWS_ADMINISTRATORS_SID,
    ]
    expected = list(dict.fromkeys(expected))
    actual: list[str] = []
    for encoded_ace in re.findall(r"\(([^()]*)\)", dacl):
        fields = encoded_ace.split(";")
        if len(fields) != 6:
            raise PrivateDirectoryError("private_directory_windows_ace_invalid")
        ace_type, ace_flags, rights, object_guid, inherit_guid, trustee = fields
        if (
            ace_type != "A"
            or "OI" not in ace_flags
            or "CI" not in ace_flags
            or rights != "FA"
            or object_guid
            or inherit_guid
        ):
            raise PrivateDirectoryError("private_directory_windows_ace_unsafe")
        actual.append(trustee)
    unmatched = expected.copy()
    for trustee in actual:
        matches = [
            candidate
            for candidate in unmatched
            if _windows_sddl_sid_matches(trustee, candidate)
        ]
        if len(matches) != 1:
            raise PrivateDirectoryError("private_directory_windows_trustee_unsafe")
        unmatched.remove(matches[0])
    if unmatched:
        raise PrivateDirectoryError("private_directory_windows_trustee_unsafe")


def _normalize_sddl_sid(value: str) -> str:
    normalized = value.strip().upper()
    return _WINDOWS_SID_ALIASES.get(normalized, normalized)


def _windows_sddl_sid_matches(left: str, right: str) -> bool:
    """Compare SDDL trustees without mistaking an alias for another SID.

    ``ConvertSecurityDescriptorToStringSecurityDescriptorW`` is allowed to
    emit SDDL constants instead of the numeric SID supplied at creation.  In
    particular, the built-in Administrator account can be rendered as
    ``LA``.  Fixed well-known aliases can be normalized locally;
    machine-relative aliases must be expanded by Windows so accounts from
    another machine or domain are never accepted based on their RID.
    """

    normalized_left = _normalize_sddl_sid(left)
    normalized_right = _normalize_sddl_sid(right)
    if normalized_left == normalized_right:
        return True
    if not _is_windows():
        return False
    expanded_left = _windows_expand_sddl_sid_alias(normalized_left)
    expanded_right = _windows_expand_sddl_sid_alias(normalized_right)
    return (
        expanded_left is not None
        and expanded_right is not None
        and expanded_left == expanded_right
    )


def _windows_expand_sddl_sid_alias(value: str) -> str | None:
    """Return one native numeric SID for an SDDL trustee, or fail closed."""

    normalized = _normalize_sddl_sid(value)
    if normalized.startswith("S-"):
        return normalized
    if re.fullmatch(r"[A-Z]{2,3}", normalized) is None:
        return None

    advapi32, kernel32 = _windows_libraries()
    _configure_windows_security_api(advapi32, kernel32)
    descriptor = wintypes.LPVOID()
    descriptor_size = wintypes.DWORD()
    if not advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW(
        f"O:{normalized}D:P",
        _SDDL_REVISION_1,
        ctypes.byref(descriptor),
        ctypes.byref(descriptor_size),
    ):
        return None
    try:
        owner = wintypes.LPVOID()
        owner_defaulted = wintypes.BOOL()
        if not advapi32.GetSecurityDescriptorOwner(
            descriptor,
            ctypes.byref(owner),
            ctypes.byref(owner_defaulted),
        ) or not owner:
            return None
        encoded = wintypes.LPWSTR()
        if not advapi32.ConvertSidToStringSidW(owner, ctypes.byref(encoded)):
            return None
        try:
            if not encoded.value:
                return None
            return _normalize_sddl_sid(str(encoded.value))
        finally:
            kernel32.LocalFree(ctypes.cast(encoded, wintypes.HLOCAL))
    finally:
        kernel32.LocalFree(descriptor)


def _windows_sddl_owner(sddl: str) -> str:
    owner_match = re.search(r"O:(.*?)(?=G:|D:|S:|$)", sddl)
    if owner_match is None:
        raise PrivateDirectoryError("private_directory_wrong_windows_owner")
    return _normalize_sddl_sid(owner_match.group(1))


def _windows_current_user_sid() -> str:
    advapi32, kernel32 = _windows_libraries()
    _configure_windows_security_api(advapi32, kernel32)
    token = wintypes.HANDLE()
    if not advapi32.OpenProcessToken(
        kernel32.GetCurrentProcess(),
        _TOKEN_QUERY,
        ctypes.byref(token),
    ):
        _raise_windows_error("OpenProcessToken")
    try:
        required = wintypes.DWORD()
        advapi32.GetTokenInformation(
            token,
            _TOKEN_USER_CLASS,
            None,
            0,
            ctypes.byref(required),
        )
        error = _windows_last_error()
        if error != _ERROR_INSUFFICIENT_BUFFER or required.value == 0:
            raise PrivateDirectoryError(
                f"GetTokenInformation(size) failed with Windows error {error}"
            )
        buffer = ctypes.create_string_buffer(required.value)
        if not advapi32.GetTokenInformation(
            token,
            _TOKEN_USER_CLASS,
            buffer,
            required,
            ctypes.byref(required),
        ):
            _raise_windows_error("GetTokenInformation")
        token_user = ctypes.cast(buffer, ctypes.POINTER(_TokenUser)).contents
        sid_text = wintypes.LPWSTR()
        if not advapi32.ConvertSidToStringSidW(
            token_user.User.Sid,
            ctypes.byref(sid_text),
        ):
            _raise_windows_error("ConvertSidToStringSidW")
        try:
            if not sid_text.value:
                raise PrivateDirectoryError("current_windows_sid_unavailable")
            return str(sid_text.value)
        finally:
            kernel32.LocalFree(ctypes.cast(sid_text, wintypes.HLOCAL))
    finally:
        kernel32.CloseHandle(token)


def _windows_create_directory_with_sddl(path: Path, sddl: str) -> None:
    advapi32, kernel32 = _windows_libraries()
    _configure_windows_security_api(advapi32, kernel32)
    descriptor = wintypes.LPVOID()
    descriptor_size = wintypes.DWORD()
    if not advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW(
        sddl,
        _SDDL_REVISION_1,
        ctypes.byref(descriptor),
        ctypes.byref(descriptor_size),
    ):
        _raise_windows_error("ConvertStringSecurityDescriptorToSecurityDescriptorW")
    try:
        attributes = _SecurityAttributes(
            nLength=ctypes.sizeof(_SecurityAttributes),
            lpSecurityDescriptor=descriptor,
            bInheritHandle=False,
        )
        if not kernel32.CreateDirectoryW(str(path), ctypes.byref(attributes)):
            error = _windows_last_error()
            if error == _ERROR_ALREADY_EXISTS:
                raise FileExistsError(error, "temporary directory already exists", path)
            raise PrivateDirectoryError(
                error,
                f"CreateDirectoryW failed: {_windows_error_text(error)}",
                path,
            )
    finally:
        kernel32.LocalFree(descriptor)


def _windows_apply_private_directory_sddl(path: Path, sddl: str) -> None:
    advapi32, kernel32 = _windows_libraries()
    _configure_windows_security_api(advapi32, kernel32)
    descriptor = wintypes.LPVOID()
    descriptor_size = wintypes.DWORD()
    if not advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW(
        sddl,
        _SDDL_REVISION_1,
        ctypes.byref(descriptor),
        ctypes.byref(descriptor_size),
    ):
        _raise_windows_error("ConvertStringSecurityDescriptorToSecurityDescriptorW")
    try:
        dacl_present = wintypes.BOOL()
        dacl = wintypes.LPVOID()
        dacl_defaulted = wintypes.BOOL()
        if not advapi32.GetSecurityDescriptorDacl(
            descriptor,
            ctypes.byref(dacl_present),
            ctypes.byref(dacl),
            ctypes.byref(dacl_defaulted),
        ):
            _raise_windows_error("GetSecurityDescriptorDacl")
        if not dacl_present.value or not dacl:
            raise PrivateDirectoryError("private_directory_missing_windows_dacl")
        security_information = (
            _DACL_SECURITY_INFORMATION | _PROTECTED_DACL_SECURITY_INFORMATION
        )
        result = advapi32.SetNamedSecurityInfoW(
            str(path),
            _SE_FILE_OBJECT,
            security_information,
            None,
            None,
            dacl,
            None,
        )
        if result:
            raise PrivateDirectoryError(
                result,
                f"SetNamedSecurityInfoW failed: {_windows_error_text(result)}",
                path,
            )
    finally:
        kernel32.LocalFree(descriptor)


def _windows_directory_sddl(path: Path) -> str:
    advapi32, kernel32 = _windows_libraries()
    _configure_windows_security_api(advapi32, kernel32)
    owner = wintypes.LPVOID()
    dacl = wintypes.LPVOID()
    descriptor = wintypes.LPVOID()
    security_information = (
        _OWNER_SECURITY_INFORMATION | _DACL_SECURITY_INFORMATION
    )
    result = advapi32.GetNamedSecurityInfoW(
        str(path),
        _SE_FILE_OBJECT,
        security_information,
        ctypes.byref(owner),
        None,
        ctypes.byref(dacl),
        None,
        ctypes.byref(descriptor),
    )
    if result:
        raise PrivateDirectoryError(
            result,
            f"GetNamedSecurityInfoW failed: {_windows_error_text(result)}",
            path,
        )
    try:
        encoded = wintypes.LPWSTR()
        encoded_length = wintypes.DWORD()
        if not advapi32.ConvertSecurityDescriptorToStringSecurityDescriptorW(
            descriptor,
            _SDDL_REVISION_1,
            security_information,
            ctypes.byref(encoded),
            ctypes.byref(encoded_length),
        ):
            _raise_windows_error(
                "ConvertSecurityDescriptorToStringSecurityDescriptorW"
            )
        try:
            if not encoded.value:
                raise PrivateDirectoryError("private_directory_windows_sddl_empty")
            return str(encoded.value)
        finally:
            kernel32.LocalFree(ctypes.cast(encoded, wintypes.HLOCAL))
    finally:
        kernel32.LocalFree(descriptor)


def _windows_libraries() -> tuple[Any, Any]:
    loader = getattr(ctypes, "WinDLL", None)
    if not callable(loader):
        raise PrivateDirectoryError("windows_security_api_unavailable")
    return loader("advapi32", use_last_error=True), loader(
        "kernel32", use_last_error=True
    )


def _configure_windows_security_api(advapi32: Any, kernel32: Any) -> None:
    kernel32.GetCurrentProcess.argtypes = []
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = [wintypes.HLOCAL]
    kernel32.LocalFree.restype = wintypes.HLOCAL
    kernel32.CreateDirectoryW.argtypes = [
        wintypes.LPCWSTR,
        ctypes.POINTER(_SecurityAttributes),
    ]
    kernel32.CreateDirectoryW.restype = wintypes.BOOL

    advapi32.OpenProcessToken.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.HANDLE),
    ]
    advapi32.OpenProcessToken.restype = wintypes.BOOL
    advapi32.GetTokenInformation.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        wintypes.LPVOID,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    ]
    advapi32.GetTokenInformation.restype = wintypes.BOOL
    advapi32.ConvertSidToStringSidW.argtypes = [
        wintypes.LPVOID,
        ctypes.POINTER(wintypes.LPWSTR),
    ]
    advapi32.ConvertSidToStringSidW.restype = wintypes.BOOL
    advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.LPVOID),
        ctypes.POINTER(wintypes.DWORD),
    ]
    advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW.restype = (
        wintypes.BOOL
    )
    advapi32.GetNamedSecurityInfoW.argtypes = [
        wintypes.LPCWSTR,
        ctypes.c_int,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.LPVOID),
        ctypes.POINTER(wintypes.LPVOID),
        ctypes.POINTER(wintypes.LPVOID),
        ctypes.POINTER(wintypes.LPVOID),
        ctypes.POINTER(wintypes.LPVOID),
    ]
    advapi32.GetNamedSecurityInfoW.restype = wintypes.DWORD
    advapi32.ConvertSecurityDescriptorToStringSecurityDescriptorW.argtypes = [
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.LPWSTR),
        ctypes.POINTER(wintypes.DWORD),
    ]
    advapi32.ConvertSecurityDescriptorToStringSecurityDescriptorW.restype = (
        wintypes.BOOL
    )
    advapi32.GetSecurityDescriptorDacl.argtypes = [
        wintypes.LPVOID,
        ctypes.POINTER(wintypes.BOOL),
        ctypes.POINTER(wintypes.LPVOID),
        ctypes.POINTER(wintypes.BOOL),
    ]
    advapi32.GetSecurityDescriptorDacl.restype = wintypes.BOOL
    advapi32.GetSecurityDescriptorOwner.argtypes = [
        wintypes.LPVOID,
        ctypes.POINTER(wintypes.LPVOID),
        ctypes.POINTER(wintypes.BOOL),
    ]
    advapi32.GetSecurityDescriptorOwner.restype = wintypes.BOOL
    advapi32.SetNamedSecurityInfoW.argtypes = [
        wintypes.LPWSTR,
        ctypes.c_int,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.LPVOID,
        wintypes.LPVOID,
        wintypes.LPVOID,
    ]
    advapi32.SetNamedSecurityInfoW.restype = wintypes.DWORD


def _remove_private_tree(path: Path) -> None:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return
    if _is_link_or_reparse(metadata) or not stat.S_ISDIR(metadata.st_mode):
        raise PrivateDirectoryError("private_temp_cleanup_target_unsafe")
    shutil.rmtree(path)
    if path.exists():
        raise PrivateDirectoryError("private_temp_cleanup_unverified")


def _is_link_or_reparse(metadata: os.stat_result) -> bool:
    attributes = getattr(metadata, "st_file_attributes", 0)
    return stat.S_ISLNK(metadata.st_mode) or bool(
        attributes & _WINDOWS_REPARSE_POINT
    )


def _directory_has_entries(path: Path) -> bool:
    try:
        with os.scandir(path) as entries:
            return next(entries, None) is not None
    except OSError as exc:
        raise PrivateDirectoryError("private_directory_scan_failed") from exc


def _same_identity(expected: os.stat_result, actual: os.stat_result) -> bool:
    return (
        stat.S_IFMT(expected.st_mode) == stat.S_IFMT(actual.st_mode)
        and expected.st_dev == actual.st_dev
        and expected.st_ino == actual.st_ino
    )


def _windows_last_error() -> int:
    implementation = getattr(ctypes, "get_last_error", None)
    return int(implementation()) if callable(implementation) else 0


def _windows_error_text(error: int) -> str:
    implementation = getattr(ctypes, "FormatError", None)
    if callable(implementation):
        return str(implementation(error))
    return f"Windows error {error}"


def _raise_windows_error(operation: str) -> None:
    error = _windows_last_error()
    raise PrivateDirectoryError(
        error,
        f"{operation} failed: {_windows_error_text(error)}",
    )
