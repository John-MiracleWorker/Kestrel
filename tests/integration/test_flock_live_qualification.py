"""Gated live Flock qualification evidence collection (Adaptive Flock Task 23).

This integration gate runs ONLY against owner-configured real provider
targets, a real project corpus, and real containment on the exact installed
artifact bytes.  It never runs by default: without
``RUN_FLOCK_LIVE_QUALIFICATION=1`` every case skips cleanly.  With the flag
set, the operator must additionally provide the exact evidence coordinates
via environment (state dir, run ID, expected receipt ID/digest, installed
artifact digest, project/tree digests, and an output path); a missing
coordinate fails closed with an explicit message rather than fabricating
evidence.  The runner never activates learned routing and never accepts raw
credentials — secrets remain behind Secret Broker references in the local
runtime.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from scripts.run_flock_live_qualification import main

pytestmark = pytest.mark.skipif(
    os.getenv("RUN_FLOCK_LIVE_QUALIFICATION") != "1",
    reason=(
        "set RUN_FLOCK_LIVE_QUALIFICATION=1 with owner-configured real "
        "providers, project corpus, and containment to run live evidence "
        "collection"
    ),
)

_REQUIRED_ENV = (
    "KESTREL_FLOCK_LIVE_STATE_DIR",
    "KESTREL_FLOCK_LIVE_RUN_ID",
    "KESTREL_FLOCK_LIVE_RECEIPT_ID",
    "KESTREL_FLOCK_LIVE_RECEIPT_DIGEST",
    "KESTREL_FLOCK_LIVE_ARTIFACT_DIGEST",
    "KESTREL_FLOCK_LIVE_PROJECT_DIGEST",
    "KESTREL_FLOCK_LIVE_TREE_DIGEST",
    "KESTREL_FLOCK_LIVE_OUTPUT",
)


def _required_env() -> dict[str, str]:
    missing = [name for name in _REQUIRED_ENV if not os.getenv(name)]
    if missing:
        raise RuntimeError(
            "RUN_FLOCK_LIVE_QUALIFICATION=1 requires owner-configured evidence "
            f"coordinates; missing: {', '.join(missing)}"
        )
    return {name: os.environ[name] for name in _REQUIRED_ENV}


def test_live_qualification_produces_redacted_bound_report() -> None:
    env = _required_env()
    argv = [
        "--run-id",
        env["KESTREL_FLOCK_LIVE_RUN_ID"],
        "--expected-receipt-id",
        env["KESTREL_FLOCK_LIVE_RECEIPT_ID"],
        "--expected-receipt-digest",
        env["KESTREL_FLOCK_LIVE_RECEIPT_DIGEST"],
        "--installed-artifact-digest",
        env["KESTREL_FLOCK_LIVE_ARTIFACT_DIGEST"],
        "--project-digest",
        env["KESTREL_FLOCK_LIVE_PROJECT_DIGEST"],
        "--tree-digest",
        env["KESTREL_FLOCK_LIVE_TREE_DIGEST"],
        "--state-dir",
        env["KESTREL_FLOCK_LIVE_STATE_DIR"],
        "--output",
        env["KESTREL_FLOCK_LIVE_OUTPUT"],
        "--confirm-live-qualification",
    ]
    source_commit = os.getenv("KESTREL_FLOCK_LIVE_SOURCE_COMMIT")
    if source_commit:
        argv.extend(["--source-commit", source_commit])
    assert main(argv) == 0
    report = json.loads(
        Path(env["KESTREL_FLOCK_LIVE_OUTPUT"]).read_text(encoding="utf-8")
    )
    assert report["schema"] == "kestrel.flock_live_qualification.v1"
    assert report["installed_artifact_digest"] == (
        env["KESTREL_FLOCK_LIVE_ARTIFACT_DIGEST"].lower()
    )
    assert report["receipt_digest"] == env["KESTREL_FLOCK_LIVE_RECEIPT_DIGEST"].lower()
    assert report["activation_performed"] is False
    real_targets = [
        target
        for target in report["targets"]
        if target["evidence_kind"] == "real_provider" and target["eligible"] is True
    ]
    assert len(real_targets) >= 2
    assert report["replay"]["unique_projection_digests"] == 1
    assert report["guardrail_violations"] == 0
    assert report["costs"]["unresolved_micros"] == 0
