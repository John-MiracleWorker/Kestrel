#!/usr/bin/env bash
set -euo pipefail
IFS=$'\n\t'

if [[ "$#" -ne 1 ]]; then
  echo "usage: $0 DESTINATION" >&2
  exit 2
fi

destination=$1
if [[ -L "$destination" ]]; then
  echo "destination must be absent or empty" >&2
  exit 1
fi
if [[ -e "$destination" ]]; then
  if [[ ! -d "$destination" ]] || [[ -n "$(find "$destination" -mindepth 1 -print -quit)" ]]; then
    echo "destination must be absent or empty" >&2
    exit 1
  fi
else
  install -d -m 0700 "$destination"
fi

platform="$(uname -s):$(uname -m)"
case "$platform" in
  Darwin:arm64)
    actionlint_url="https://github.com/rhysd/actionlint/releases/download/v1.7.7/actionlint_1.7.7_darwin_arm64.tar.gz"
    actionlint_digest="2693315b9093aeacb4ebd91a993fea54fc215057bf0da2659056b4bc033873db"
    shellcheck_url="https://github.com/koalaman/shellcheck/releases/download/v0.11.0/shellcheck-v0.11.0.darwin.aarch64.tar.xz"
    shellcheck_digest="56affdd8de5527894dca6dc3d7e0a99a873b0f004d7aabc30ae407d3f48b0a79"
    ;;
  Linux:x86_64)
    actionlint_url="https://github.com/rhysd/actionlint/releases/download/v1.7.7/actionlint_1.7.7_linux_amd64.tar.gz"
    actionlint_digest="023070a287cd8cccd71515fedc843f1985bf96c436b7effaecce67290e7e0757"
    shellcheck_url="https://github.com/koalaman/shellcheck/releases/download/v0.11.0/shellcheck-v0.11.0.linux.x86_64.tar.xz"
    shellcheck_digest="8c3be12b05d5c177a04c29e3c78ce89ac86f1595681cab149b65b97c4e227198"
    ;;
  *)
    echo "unsupported workflow-linter bootstrap platform: $platform" >&2
    exit 1
    ;;
esac

bootstrap_tmp="$(mktemp -d "${TMPDIR:-/tmp}/kestrel-workflow-linters.XXXXXX")"
cleanup() {
  rm -rf -- "$bootstrap_tmp"
}
trap cleanup EXIT HUP INT TERM

actionlint_archive="$bootstrap_tmp/actionlint.tar.gz"
shellcheck_archive="$bootstrap_tmp/shellcheck.tar.xz"
curl --fail --location --proto '=https' --tlsv1.2 --output "$actionlint_archive" "$actionlint_url"
curl --fail --location --proto '=https' --tlsv1.2 --output "$shellcheck_archive" "$shellcheck_url"

printf '%s  %s\n' "$actionlint_digest" "$actionlint_archive" | shasum -a 256 -c -
printf '%s  %s\n' "$shellcheck_digest" "$shellcheck_archive" | shasum -a 256 -c -

tar -tf "$actionlint_archive" >/dev/null
tar -tJf "$shellcheck_archive" >/dev/null

python3 - "$actionlint_archive" actionlint "$shellcheck_archive" shellcheck-v0.11.0/shellcheck <<'PY'
from __future__ import annotations

import sys
import tarfile
from pathlib import PurePosixPath


def validate(archive_path: str, expected_member: str) -> None:
    seen: set[str] = set()
    expected_count = 0
    with tarfile.open(archive_path, mode="r:*") as archive:
        if archive.pax_headers:
            raise SystemExit("workflow-linter archive has global pax headers")
        for member in archive:
            name = member.name.rstrip("/")
            path = PurePosixPath(name)
            if (
                not name
                or path.is_absolute()
                or any(part in {"", ".", ".."} for part in path.parts)
                or "../" in f"{name}/"
                or "\\" in name
                or name in seen
            ):
                raise SystemExit("workflow-linter archive has an unsafe or duplicate member")
            seen.add(name)
            if member.pax_headers or not (member.isdir() or member.isfile()):
                raise SystemExit("workflow-linter archive has a link or special member")
            if name == expected_member:
                if not member.isfile() or member.size <= 0:
                    raise SystemExit("workflow-linter executable member is not regular")
                expected_count += 1
    if expected_count != 1:
        raise SystemExit("workflow-linter executable member is absent or ambiguous")


validate(sys.argv[1], sys.argv[2])
validate(sys.argv[3], sys.argv[4])
PY

tar -xOf "$actionlint_archive" actionlint >"$bootstrap_tmp/actionlint"
tar -xJOf "$shellcheck_archive" shellcheck-v0.11.0/shellcheck >"$bootstrap_tmp/shellcheck"
chmod 0755 "$bootstrap_tmp/actionlint" "$bootstrap_tmp/shellcheck"

# actionlint 1.7.7
actionlint_version="$("$bootstrap_tmp/actionlint" -version)"
actionlint_version="${actionlint_version%%$'\n'*}"
if [[ "$actionlint_version" != "1.7.7" ]]; then
  echo "actionlint 1.7.7 version verification failed" >&2
  exit 1
fi

shellcheck_version="$("$bootstrap_tmp/shellcheck" --version)"
if ! grep -Fqx 'ShellCheck - shell script analysis tool' <<<"$shellcheck_version" ||
  ! grep -Fqx 'version: 0.11.0' <<<"$shellcheck_version"; then
  echo "ShellCheck 0.11.0 version verification failed" >&2
  exit 1
fi

install -m 0755 "$bootstrap_tmp/actionlint" "$destination/actionlint"
install -m 0755 "$bootstrap_tmp/shellcheck" "$destination/shellcheck"
if [[ "$(find "$destination" -mindepth 1 -print | LC_ALL=C sort)" != \
  "$destination/actionlint"$'\n'"$destination/shellcheck" ]]; then
  echo "workflow-linter installation inventory mismatch" >&2
  exit 1
fi
