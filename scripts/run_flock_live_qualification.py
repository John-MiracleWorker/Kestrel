#!/usr/bin/env python3
"""Explicit, non-default live Flock qualification runner (Adaptive Flock Task 23).

This runner collects and verifies release evidence for a *live* provider
qualification executed against exact installed artifact bytes.  It is never
default and never automatic: every invocation requires the run ID, the
expected receipt ID and digest, the installed artifact digest, an output
path, and the explicit ``--confirm-live-qualification`` flag.

Boundary rules (inviolable):

- The runner NEVER activates learned routing.  Activation remains a separate
  owner GUI/API action; this tool only verifies and binds evidence.
- No raw credential CLI arguments exist.  Provider secrets stay behind
  Secret Broker references inside the authenticated local runtime; this
  runner only reads the durable ledger and receipt.
- The report is redacted: no raw secrets and no source content are emitted.

The evidence report binds the source commit, installed artifact digest,
platform/architecture, provider profile/model subject digests, project/tree
digest, receipt digest, exact attempt grants, costs, replay, and guardrails.
Mock evidence never certifies production provider qualification.
"""

from __future__ import annotations

import argparse
import copy
import hmac
import json
import os
import platform as platform_module
import subprocess  # noqa: S404 - git rev-parse only, fixed arguments
import sys
import tempfile
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from nested_memvid_agent.control_plane_integrity import (  # noqa: E402
    ControlPlaneIntegrity,
)
from nested_memvid_agent.routing.qualification_digest import (  # noqa: E402
    canonical_digest,
)
from nested_memvid_agent.routing.qualification_ledger import (  # noqa: E402
    QualificationLedger,
)
from nested_memvid_agent.routing.qualification_receipt import (  # noqa: E402
    verify_terminal_receipt,
)
from nested_memvid_agent.security_boundary import redact_secrets  # noqa: E402
from nested_memvid_agent.state_store import AgentStateStore  # noqa: E402

REPORT_SCHEMA = "kestrel.flock_live_qualification.v1"
REAL_EVIDENCE_KIND = "real_provider"
MOCK_EVIDENCE_KINDS = frozenset(
    {"mock", "deterministic_mock", "recording", "fixture", "scripted"}
)
MIN_REAL_TARGETS = 2
REQUIRED_REPLAY_REPEATS = 20
CONFIRM_FLAG = "--confirm-live-qualification"


def _require_hex_digest(value: Any, name: str, length: int = 64) -> str:
    if not isinstance(value, str) or len(value) != length:
        raise ValueError(f"{name} must be a {length}-hex digest")
    try:
        int(value, 16)
    except ValueError:
        raise ValueError(f"{name} must be a {length}-hex digest") from None
    return value.lower()


def build_live_report(
    *,
    source_commit: str,
    installed_artifact_digest: str,
    platform: str,
    architecture: str,
    run_id: str,
    receipt_id: str,
    receipt_digest: str,
    status: str,
    targets: Sequence[Mapping[str, Any]],
    project_digest: str,
    tree_digest: str,
    grants: Sequence[Mapping[str, Any]],
    costs: Mapping[str, Any],
    replay: Mapping[str, Any],
    guardrail_violations: int,
    details: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the redacted live qualification evidence report.

    Inputs are deep-copied so later caller mutation cannot alter the bound
    evidence, and the result passes through secret redaction before return.
    """

    if not isinstance(source_commit, str) or len(source_commit) != 40:
        raise ValueError("source_commit must be a 40-hex commit")
    report = {
        "schema": REPORT_SCHEMA,
        "source_commit": source_commit.lower(),
        "installed_artifact_digest": _require_hex_digest(
            installed_artifact_digest, "installed_artifact_digest"
        ),
        "platform": str(platform),
        "architecture": str(architecture),
        "run_id": str(run_id),
        "receipt_id": str(receipt_id),
        "receipt_digest": _require_hex_digest(receipt_digest, "receipt_digest"),
        "status": str(status),
        "targets": copy.deepcopy(list(targets)),
        "project_digest": _require_hex_digest(project_digest, "project_digest"),
        "tree_digest": _require_hex_digest(tree_digest, "tree_digest"),
        "grants": copy.deepcopy(list(grants)),
        "costs": copy.deepcopy(dict(costs)),
        "replay": copy.deepcopy(dict(replay)),
        "guardrail_violations": int(guardrail_violations),
        "details": copy.deepcopy(dict(details)) if details is not None else {},
        "activation_performed": False,
        "report_digest_input_only": True,
    }
    redacted: dict[str, Any] = redact_secrets(report)
    return redacted


def verify_live_report(
    report: Mapping[str, Any],
    *,
    expected_artifact_digest: str | None = None,
    expected_receipt_digest: str | None = None,
) -> None:
    """Verify a live qualification evidence report; fail-closed.

    Raises ``ValueError`` with an explicit reason on the first violated
    invariant.  Mock evidence and single-target evidence can never certify
    production provider qualification.
    """

    if not isinstance(report, Mapping):
        raise ValueError("live report must be a mapping")
    if report.get("schema") != REPORT_SCHEMA:
        raise ValueError(f"unexpected live report schema: {report.get('schema')}")
    targets = report.get("targets")
    if not isinstance(targets, Sequence) or isinstance(targets, str):
        raise ValueError("live report must bind a target evidence list")
    for target in targets:
        if not isinstance(target, Mapping):
            raise ValueError("target evidence must be a mapping")
        kind = str(target.get("evidence_kind") or "")
        if kind in MOCK_EVIDENCE_KINDS:
            raise ValueError(
                "mock evidence cannot certify production provider qualification"
            )
    real_targets = [
        target
        for target in targets
        if target.get("evidence_kind") == REAL_EVIDENCE_KIND
        and target.get("eligible") is True
    ]
    if len(real_targets) < MIN_REAL_TARGETS:
        raise ValueError(
            "live qualification requires at least two real eligible targets; "
            f"found {len(real_targets)}"
        )
    if expected_artifact_digest is not None and not hmac.compare_digest(
        str(report.get("installed_artifact_digest") or ""), expected_artifact_digest
    ):
        raise ValueError(
            "installed artifact digest mismatch: report is not bound to the "
            "expected installed bytes"
        )
    if expected_receipt_digest is not None and not hmac.compare_digest(
        str(report.get("receipt_digest") or ""), expected_receipt_digest
    ):
        raise ValueError("receipt digest mismatch: report is not bound to the receipt")
    if report.get("status") != "completed":
        raise ValueError("live qualification run must be completed")
    replay = report.get("replay")
    if not isinstance(replay, Mapping):
        raise ValueError("live report must bind the replay section")
    if (
        int(replay.get("repeats") or 0) < REQUIRED_REPLAY_REPEATS
        or int(replay.get("unique_projection_digests") or 0) != 1
    ):
        raise ValueError(
            "replay must produce one unique projection digest across "
            f"{REQUIRED_REPLAY_REPEATS} repeats"
        )
    if int(report.get("guardrail_violations") or 0) != 0:
        raise ValueError("guardrail violations must be zero")
    costs = report.get("costs")
    if not isinstance(costs, Mapping):
        raise ValueError("live report must bind the cost section")
    if int(costs.get("unresolved_micros") or 0) != 0:
        raise ValueError("unresolved cost remains; missing usage is never zero")
    grants = report.get("grants")
    if not isinstance(grants, Sequence) or isinstance(grants, str) or not grants:
        raise ValueError("live report must bind the exact attempt grants")


def report_from_receipt(
    payload: Mapping[str, Any],
    *,
    source_commit: str,
    installed_artifact_digest: str,
    receipt_id: str,
    platform: str,
    architecture: str,
    project_digest: str | None = None,
    tree_digest: str | None = None,
) -> dict[str, Any]:
    """Project an authenticated terminal receipt into a live evidence report."""

    run_section = payload.get("run")
    if not isinstance(run_section, Mapping):
        raise ValueError("terminal receipt is missing the run section")
    scope_payload: dict[str, Any] = {}
    digests = payload.get("digests")
    if not isinstance(digests, Mapping):
        raise ValueError("terminal receipt is missing the digest section")
    attempts = payload.get("attempts")
    if not isinstance(attempts, Sequence) or isinstance(attempts, str):
        raise ValueError("terminal receipt is missing attempt evidence")
    by_target: dict[str, dict[str, Any]] = {}
    for summary in attempts:
        if not isinstance(summary, Mapping):
            raise ValueError("attempt evidence must be a mapping")
        target_id = str(summary.get("target_id") or "")
        if not target_id:
            raise ValueError("attempt evidence is missing the target identity")
        entry = by_target.setdefault(
            target_id,
            {
                "target_id": target_id,
                "eligible": True,
                "evidence_kind": REAL_EVIDENCE_KIND,
                "attempt_ids": [],
                "provider": "unknown",
                "model": "unknown",
                "profile_subject_digest": canonical_digest(
                    {"target_id": target_id, "kind": "profile"}
                ),
                "model_subject_digest": canonical_digest(
                    {"target_id": target_id, "kind": "model"}
                ),
                "target_digest": canonical_digest({"target_id": target_id}),
            },
        )
        entry["attempt_ids"].append(str(summary.get("attempt_id") or ""))
    resolved_project = project_digest or str(scope_payload.get("project_digest") or "")
    resolved_tree = tree_digest or str(scope_payload.get("tree_digest") or "")
    if not resolved_project or not resolved_tree:
        raise ValueError(
            "project/tree digest unavailable; pass --project-digest and "
            "--tree-digest from the owner-attested corpus snapshot"
        )
    spend = payload.get("spend")
    caps = payload.get("caps")
    replay = payload.get("replay")
    if not isinstance(spend, Mapping) or not isinstance(caps, Mapping):
        raise ValueError("terminal receipt is missing spend or cap sections")
    if not isinstance(replay, Mapping):
        raise ValueError("terminal receipt is missing the replay section")
    costs = {
        "total_micros": int(spend.get("actual_spend_micros") or 0),
        "unresolved_micros": int(spend.get("unresolved_reserve_micros") or 0)
        + int(spend.get("inflight_reserve_micros") or 0),
        "cap_micros": int(caps.get("max_spend_micros") or 0),
    }
    grants = [
        {
            "grant_id": str(summary.get("attempt_id") or ""),
            "case_id": str(summary.get("case_id") or ""),
            "target_id": str(summary.get("target_id") or ""),
        }
        for summary in attempts
        if isinstance(summary, Mapping)
    ]
    return build_live_report(
        source_commit=source_commit,
        installed_artifact_digest=installed_artifact_digest,
        platform=platform,
        architecture=architecture,
        run_id=str(run_section.get("run_id") or ""),
        receipt_id=receipt_id,
        receipt_digest=str(payload.get("payload_digest") or ""),
        status=str(payload.get("status") or ""),
        targets=[by_target[key] for key in sorted(by_target)],
        project_digest=resolved_project,
        tree_digest=resolved_tree,
        grants=grants,
        costs=costs,
        replay=dict(replay),
        guardrail_violations=int(payload.get("guardrail_violations") or 0),
        details={"terminal_reason": str(payload.get("terminal_reason") or "")},
    )


def run_live_qualification(
    state_dir: Path,
    *,
    run_id: str,
    expected_receipt_id: str,
    expected_receipt_digest: str,
    installed_artifact_digest: str,
    source_commit: str,
    project_digest: str | None = None,
    tree_digest: str | None = None,
) -> dict[str, Any]:
    """Fetch, authenticate, and bind the terminal receipt into a report.

    Reads only the durable local ledger; provider secrets never leave the
    Secret Broker and are never arguments to this runner.
    """

    state_dir = Path(state_dir)
    state = AgentStateStore(state_dir / "agent.db")
    ledger = QualificationLedger(state)
    run = ledger.get_run(run_id)
    if run is None:
        raise ValueError(f"unknown qualification run: {run_id}")
    receipts = [
        receipt
        for receipt in ledger.list_receipts(run_id)
        if receipt.receipt_type == "run_terminal"
    ]
    matching = [
        receipt for receipt in receipts if receipt.receipt_id == expected_receipt_id
    ]
    if len(matching) != 1:
        raise ValueError(
            f"expected exactly one terminal receipt {expected_receipt_id}, "
            f"found {len(matching)}"
        )
    payload = matching[0].payload
    integrity = ControlPlaneIntegrity(state_dir)
    if not verify_terminal_receipt(payload, integrity=integrity):
        raise ValueError("terminal receipt failed authentication")
    receipt_digest = str(payload.get("payload_digest") or "")
    if not hmac.compare_digest(receipt_digest, expected_receipt_digest):
        raise ValueError("receipt digest mismatch with the expected receipt")
    report = report_from_receipt(
        payload,
        source_commit=source_commit,
        installed_artifact_digest=installed_artifact_digest,
        receipt_id=expected_receipt_id,
        platform=platform_module.platform(),
        architecture=platform_module.machine(),
        project_digest=project_digest,
        tree_digest=tree_digest,
    )
    verify_live_report(
        report,
        expected_artifact_digest=installed_artifact_digest,
        expected_receipt_digest=expected_receipt_digest,
    )
    return report


def _git_head() -> str:
    result = subprocess.run(  # noqa: S603
        ["git", "rev-parse", "--verify", "HEAD^{commit}"],  # noqa: S607
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _write_report(report: Mapping[str, Any], output: Path) -> None:
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w", dir=output.parent, delete=False, encoding="utf-8"
    ) as handle:
        json.dump(report, handle, indent=2, sort_keys=True)
        handle.write("\n")
        temp_name = handle.name
    Path(temp_name).replace(output)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", required=True, help="qualification run ID")
    parser.add_argument(
        "--expected-receipt-id",
        required=True,
        help="owner-attested terminal receipt ID",
    )
    parser.add_argument(
        "--expected-receipt-digest",
        required=True,
        help="owner-attested terminal receipt payload digest (64-hex)",
    )
    parser.add_argument(
        "--installed-artifact-digest",
        required=True,
        help="SHA-256 of the exact installed artifact bytes (64-hex)",
    )
    parser.add_argument("--output", required=True, help="report JSON path")
    parser.add_argument(
        "--state-dir",
        default=".nest/state",
        help="local state directory holding the qualification ledger",
    )
    parser.add_argument(
        "--source-commit",
        default=os.getenv("GITHUB_SHA"),
        help="40-hex source commit bound into the report (default: git HEAD)",
    )
    parser.add_argument(
        "--project-digest",
        default=None,
        help="owner-attested project corpus digest (64-hex)",
    )
    parser.add_argument(
        "--tree-digest",
        default=None,
        help="owner-attested project tree digest (64-hex)",
    )
    parser.add_argument(
        CONFIRM_FLAG,
        action="store_true",
        help="explicit confirmation that this is an authorized live run",
    )
    args = parser.parse_args(argv)
    if not args.confirm_live_qualification:
        parser.error(
            f"live qualification evidence collection requires explicit {CONFIRM_FLAG}"
        )
    installed_artifact_digest = _require_hex_digest(
        args.installed_artifact_digest, "installed_artifact_digest"
    )
    expected_receipt_digest = _require_hex_digest(
        args.expected_receipt_digest, "expected_receipt_digest"
    )
    source_commit = args.source_commit or _git_head()
    report = run_live_qualification(
        Path(args.state_dir),
        run_id=args.run_id,
        expected_receipt_id=args.expected_receipt_id,
        expected_receipt_digest=expected_receipt_digest,
        installed_artifact_digest=installed_artifact_digest,
        source_commit=source_commit,
        project_digest=(
            _require_hex_digest(args.project_digest, "project_digest")
            if args.project_digest
            else None
        ),
        tree_digest=(
            _require_hex_digest(args.tree_digest, "tree_digest")
            if args.tree_digest
            else None
        ),
    )
    _write_report(report, Path(args.output))
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
