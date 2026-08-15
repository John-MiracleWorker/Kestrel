#!/usr/bin/env bash
set -euo pipefail
IFS=$'\n\t'

if [[ "$#" -ne 1 ]]; then
  echo "usage: $0 DESTINATION" >&2
  exit 2
fi

destination=$1
platform="$(uname -s):$(uname -m)"
case "$platform" in
  Darwin:arm64)
    gh_url="https://github.com/cli/cli/releases/download/v2.97.0/gh_2.97.0_macOS_arm64.zip"
    gh_digest="a58b8fd77b417a38f47a0b54d1370c59b0fcdb324ccc9ca002b0998f7c4c999e"
    archive_kind="zip"
    archive_root="gh_2.97.0_macOS_arm64"
    ;;
  Linux:x86_64)
    gh_url="https://github.com/cli/cli/releases/download/v2.97.0/gh_2.97.0_linux_amd64.tar.gz"
    gh_digest="a2c9b8497e1f85b1ad0dfcb78b5a622e098801b8e461e459e88e1ee12f018112"
    archive_kind="tar"
    archive_root="gh_2.97.0_linux_amd64"
    ;;
  *)
    echo "unsupported workflow-tools bootstrap platform: $platform" >&2
    exit 1
    ;;
esac

if [[ -L "$destination" ]]; then
  echo "workflow-tools destination must be absent or empty" >&2
  exit 1
fi
if [[ -e "$destination" ]]; then
  if [[ ! -d "$destination" ]] || [[ -n "$(find "$destination" -mindepth 1 -print -quit)" ]]; then
    echo "workflow-tools destination must be absent or empty" >&2
    exit 1
  fi
else
  install -d -m 0700 "$destination"
fi

if destination_mode="$(stat -f '%Lp' "$destination" 2>/dev/null)"; then
  :
else
  destination_mode="$(stat -c '%a' "$destination")"
fi
if [[ "$destination_mode" != "700" ]]; then
  echo "workflow-tools destination must have mode 0700" >&2
  exit 1
fi

bootstrap_tmp="$(mktemp -d "${TMPDIR:-/tmp}/kestrel-workflow-tools.XXXXXX")"
cleanup() {
  rm -rf -- "$bootstrap_tmp"
}
trap cleanup EXIT HUP INT TERM
install -d -m 0700 "$bootstrap_tmp/home"

archive="$bootstrap_tmp/gh.$archive_kind"
curl --fail --location --proto '=https' --tlsv1.2 --proxy '' --noproxy '*' \
  --output "$archive" "$gh_url"

archive_size="$(wc -c <"$archive" | tr -d '[:space:]')"
if [[ ! "$archive_size" =~ ^[0-9]+$ ]] || ((archive_size == 0 || archive_size > 67108864)); then
  echo "workflow-tools archive exceeds the allowed size" >&2
  exit 1
fi

printf '%s  %s\n' "$gh_digest" "$archive" | shasum -a 256 -c -

python3 - "$archive" "$archive_kind" "$archive_root" "$bootstrap_tmp/gh" <<'PY'
from __future__ import annotations

import stat
import sys
import tarfile
import zipfile
from pathlib import PurePosixPath
from typing import BinaryIO

MAX_BINARY_BYTES = 134_217_728

archive_path, archive_kind, archive_root, output_path = sys.argv[1:]
required = {
    f"{archive_root}/LICENSE",
    f"{archive_root}/bin/gh",
}
seen: set[str] = set()
def fail(message: str) -> None:
    raise SystemExit(f"workflow-tools archive {message}")


def normalized_name(raw_name: str) -> str:
    if not raw_name or "\\" in raw_name or "\x00" in raw_name or "//" in raw_name:
        fail("contains an unsafe or duplicate member")
    name = raw_name[:-1] if raw_name.endswith("/") else raw_name
    path = PurePosixPath(name)
    if (
        not name
        or path.is_absolute()
        or any(part in {"", ".", ".."} for part in path.parts)
        or name in seen
    ):
        fail("contains an unsafe or duplicate member")
    seen.add(name)
    return name


def allowed(name: str, *, is_directory: bool) -> bool:
    if name in {archive_root, f"{archive_root}/bin"}:
        return is_directory
    if name in {f"{archive_root}/LICENSE", f"{archive_root}/bin/gh"}:
        return not is_directory
    man_roots = {
        f"{archive_root}/share",
        f"{archive_root}/share/man",
        f"{archive_root}/share/man/man1",
    }
    if name in man_roots:
        return is_directory
    man_prefix = f"{archive_root}/share/man/man1/"
    if name.startswith(man_prefix):
        leaf = name.removeprefix(man_prefix)
        return (
            not is_directory
            and leaf.startswith("gh")
            and leaf.endswith(".1")
            and "/" not in leaf
            and all(character.isalnum() or character in {"-", ".", "_"} for character in leaf)
        )
    return False


def copy_bounded(source: BinaryIO) -> None:
    remaining = MAX_BINARY_BYTES + 1
    with open(output_path, "xb") as destination:
        while remaining:
            chunk = source.read(min(1024 * 1024, remaining))
            if not chunk:
                break
            destination.write(chunk)
            remaining -= len(chunk)
    if remaining == MAX_BINARY_BYTES + 1 or remaining == 0:
        fail("contains an empty or oversized gh executable")


if archive_kind == "tar":
    with tarfile.open(archive_path, mode="r:gz") as archive:
        if archive.pax_headers:
            fail("contains global pax headers")
        gh_member: tarfile.TarInfo | None = None
        for member in archive:
            name = normalized_name(member.name)
            if member.pax_headers or not (member.isdir() or member.isfile()):
                fail("contains a link or special member")
            if not allowed(name, is_directory=member.isdir()):
                fail("contains an unexpected member")
            if member.isfile() and member.size <= 0:
                fail("contains an empty regular member")
            if member.isfile() and member.size > MAX_BINARY_BYTES:
                fail("contains an oversized member")
            if name == f"{archive_root}/bin/gh":
                if not member.isfile() or not member.mode & 0o100:
                    fail("gh executable is not an executable regular file")
                gh_member = member
        if not required.issubset(seen) or gh_member is None:
            fail("is missing required members")
        extracted = archive.extractfile(gh_member)
        if extracted is None:
            fail("gh executable could not be read")
        with extracted:
            copy_bounded(extracted)
elif archive_kind == "zip":
    with zipfile.ZipFile(archive_path, mode="r") as archive:
        gh_info: zipfile.ZipInfo | None = None
        for info in archive.infolist():
            name = normalized_name(info.filename)
            mode = info.external_attr >> 16
            file_type = stat.S_IFMT(mode)
            is_directory = info.is_dir()
            if info.flag_bits & 0x1:
                fail("contains an encrypted member")
            if is_directory:
                if file_type != stat.S_IFDIR or info.file_size != 0:
                    fail("contains a link or special member")
            elif file_type != stat.S_IFREG:
                fail("contains a link or special member")
            if not allowed(name, is_directory=is_directory):
                fail("contains an unexpected member")
            if not is_directory and info.file_size <= 0:
                fail("contains an empty regular member")
            if info.file_size > MAX_BINARY_BYTES:
                fail("contains an oversized member")
            if name == f"{archive_root}/bin/gh":
                if is_directory or not mode & 0o100:
                    fail("gh executable is not an executable regular file")
                gh_info = info
        if not required.issubset(seen) or gh_info is None:
            fail("is missing required members")
        with archive.open(gh_info, mode="r") as extracted:
            copy_bounded(extracted)
else:
    fail("kind is unsupported")
PY

chmod 0755 "$bootstrap_tmp/gh"
gh_version="$(PATH='' HOME="$bootstrap_tmp/home" "$bootstrap_tmp/gh" --version)"
gh_version="${gh_version%%$'\n'*}"
if [[ "$gh_version" != "gh version 2.97.0 (2026-02-26)" ]]; then
  echo "GitHub CLI version verification failed" >&2
  exit 1
fi

install -m 0755 "$bootstrap_tmp/gh" "$destination/gh"
if [[ ! -f "$destination/gh" ]] || [[ -L "$destination/gh" ]] || \
  [[ "$(find "$destination" -mindepth 1 -maxdepth 1 -print | wc -l | tr -d '[:space:]')" != "1" ]]; then
  echo "workflow-tools installation inventory mismatch" >&2
  exit 1
fi
