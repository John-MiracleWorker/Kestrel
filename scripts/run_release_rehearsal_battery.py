#!/usr/bin/env python3
"""Run N consecutive unique-namespace release rehearsals against one exact
candidate and seal an aggregate receipt digest (REL-004).

The battery is deliberately ritual-free: every rehearsal must pass on its
first attempt. A failure is reported with the rehearsal index and the battery
exits nonzero without retrying, so a flaky rehearsal can never masquerade as
a repeated-rehearsal receipt.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

if not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.run_release_rehearsal import (  # noqa: E402
    _validate_namespace,
    _validate_source,
    run_release_rehearsal,
)

BATTERY_SCHEMA = "kestrel.release_rehearsal_battery.v1"
DEFAULT_REPEATS = 20

_COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")


def _canonical_json(value: Mapping[str, Any]) -> bytes:
    """RFC 8785-style canonical serialization parity with
    scripts.aggregate_runtime_reliability_receipts._canonical_json."""
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("ascii")


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def rehearsal_namespaces(commit: str, repeats: int) -> list[str]:
    """Deterministic, unique rehearsal namespaces for one exact commit."""
    if not _COMMIT_RE.fullmatch(commit):
        raise ValueError(f"invalid exact source commit: {commit!r}")
    if not isinstance(repeats, int) or repeats < 1:
        raise ValueError(f"repeats must be a positive integer, got {repeats!r}")
    namespaces = [
        f"kestrel-rehearsal-{commit[:12]}-{index:03d}" for index in range(1, repeats + 1)
    ]
    for namespace in namespaces:
        _validate_namespace(namespace)
    if len(set(namespaces)) != len(namespaces):
        raise ValueError("rehearsal namespaces are not unique")
    return namespaces


def aggregate_body(
    *,
    commit: str,
    distribution: str,
    version: str,
    namespaces: list[str],
    rehearsal_reports: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "schema": BATTERY_SCHEMA,
        "source": {
            "commit": commit,
            "distribution": distribution,
            "version": version,
        },
        "repeats": len(namespaces),
        "namespaces": list(namespaces),
        "rehearsal_reports": list(rehearsal_reports),
        "zero_flaky_failures": True,
    }


def recompute_aggregate_digest(receipt: Mapping[str, Any]) -> str:
    """SHA-256 of the canonical serialization of the receipt body without the
    aggregate_digest field, so any verifier can recompute the sealed digest."""
    body = {key: value for key, value in receipt.items() if key != "aggregate_digest"}
    return _sha256_bytes(_canonical_json(body))


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def run_release_rehearsal_battery(
    *,
    source_root: Path,
    sandbox_root: Path,
    commit: str,
    repeats: int = DEFAULT_REPEATS,
    output_dir: Path,
) -> dict[str, Any]:
    """Run `repeats` consecutive rehearsals of one exact candidate, each in a
    unique namespace, and return the sealed aggregate receipt."""
    source_root, distribution, version = _validate_source(source_root, commit)
    sandbox_root = sandbox_root.expanduser().resolve(strict=False)
    output_dir = output_dir.expanduser().resolve(strict=False)
    if sandbox_root.exists() or sandbox_root.is_symlink():
        raise ValueError(f"battery sandbox root must not already exist: {sandbox_root}")
    if output_dir.exists() or output_dir.is_symlink():
        raise ValueError(f"battery output directory must not already exist: {output_dir}")
    sandbox_root.parent.resolve(strict=True)
    sandbox_root.mkdir(mode=0o700)
    output_dir.mkdir(mode=0o700)

    namespaces = rehearsal_namespaces(commit, repeats)
    rehearsal_reports: list[dict[str, Any]] = []
    for index, namespace in enumerate(namespaces, start=1):
        rehearsal_sandbox = sandbox_root / namespace
        try:
            report = run_release_rehearsal(
                source_root=source_root,
                sandbox_root=rehearsal_sandbox,
                namespace=namespace,
                commit=commit,
            )
        except (OSError, ValueError) as exc:
            raise ValueError(
                f"rehearsal {index} of {repeats} failed on first attempt in "
                f"namespace {namespace!r}: {exc}"
            ) from exc
        if report.get("passed") is not True:
            raise ValueError(
                f"rehearsal {index} of {repeats} reported a non-passing result "
                f"in namespace {namespace!r}"
            )
        report_file = output_dir / f"rehearsal-{index:03d}.json"
        _write_json(report_file, report)
        rehearsal_reports.append(
            {
                "index": index,
                "namespace": namespace,
                "report_file": report_file.name,
                "report_sha256": _sha256_bytes(report_file.read_bytes()),
            }
        )

    body = aggregate_body(
        commit=commit,
        distribution=distribution,
        version=version,
        namespaces=namespaces,
        rehearsal_reports=rehearsal_reports,
    )
    receipt: dict[str, Any] = {**body, "aggregate_digest": recompute_aggregate_digest(body)}
    if recompute_aggregate_digest(receipt) != receipt["aggregate_digest"]:
        raise ValueError("aggregate receipt digest failed self-verification")
    _write_json(output_dir / "aggregate-receipt.json", receipt)
    return receipt


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--commit", required=True)
    parser.add_argument("--sandbox-root", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--repeats", type=int, default=DEFAULT_REPEATS)
    args = parser.parse_args()
    try:
        receipt = run_release_rehearsal_battery(
            source_root=args.source_root,
            sandbox_root=args.sandbox_root,
            commit=args.commit,
            repeats=args.repeats,
            output_dir=args.output_dir,
        )
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    print(json.dumps(receipt, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
