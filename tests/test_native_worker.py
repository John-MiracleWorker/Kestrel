"""PR 8 — Native agent workers and branch fan-out tests.

Add target_kind=native_agent, define native worker lifecycle adapter,
support structured start/status/steer/cancel/artifact collection, integrate
Codex as a first native worker, preserve worktree isolation and exact
approval boundaries, add candidate branch review and merge proposal
artifacts, and do not auto-merge or publish remotely.

RED tests:
- Native worker cancellation is verified
- Credentials remain in the correct trust domain
- Artifacts bind to the expected worktree/branch/diff
- Multiple workers cannot overwrite each other's branches
- Merge proposal requires independent validation/review
"""
from __future__ import annotations

import pytest

from nested_memvid_agent.routing.native_worker import (
    NativeWorkerAdapter,
    NativeWorkerConfig,
    NativeWorkerStatus,
    WorkerCredentials,
    WorkerState,
    start_native_worker,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_config(
    *,
    worker_id: str = "worker-1",
    worktree_path: str | None = None,
    branch: str = "feature/test-1",
    trust_domain: str = "local",
) -> NativeWorkerConfig:
    return NativeWorkerConfig(
        worker_id=worker_id,
        worktree_path=worktree_path or "/tmp/worktree-1",
        branch=branch,
        objective="rename variable foo to bar in src/app.py",
        trust_domain=trust_domain,
        command="codex --quiet --print",
    )


def _make_credentials(trust_domain: str = "local") -> WorkerCredentials:
    return WorkerCredentials(
        trust_domain=trust_domain,
        env_vars={"OPENAI_API_KEY": "sk-test-key"} if trust_domain == "cloud" else {},
    )


# ---------------------------------------------------------------------------
# Native worker cancellation is verified
# ---------------------------------------------------------------------------

class TestNativeWorkerCancellation:
    def test_cancel_transitions_to_cancelled_state(self):
        adapter = NativeWorkerAdapter()
        config = _make_config()
        worker = adapter.start(config)
        assert worker.state == WorkerState.RUNNING

        adapter.cancel(worker)
        assert worker.state == WorkerState.CANCELLED

    def test_cancel_is_idempotent(self):
        adapter = NativeWorkerAdapter()
        config = _make_config()
        worker = adapter.start(config)
        adapter.cancel(worker)
        # Cancelling again should not raise
        adapter.cancel(worker)
        assert worker.state == WorkerState.CANCELLED

    def test_cancelled_worker_does_not_produce_artifacts(self):
        adapter = NativeWorkerAdapter()
        config = _make_config()
        worker = adapter.start(config)
        adapter.cancel(worker)
        artifacts = adapter.collect_artifacts(worker)
        # A cancelled worker should not produce completion artifacts
        assert all(not a.kind.startswith("completion_") for a in artifacts)

    def test_cancel_before_start_is_noop(self):
        adapter = NativeWorkerAdapter()
        config = _make_config()
        worker = NativeWorkerStatus(config=config)
        # Worker hasn't been started yet
        adapter.cancel(worker)
        assert worker.state in (WorkerState.PENDING, WorkerState.CANCELLED)


# ---------------------------------------------------------------------------
# Credentials remain in the correct trust domain
# ---------------------------------------------------------------------------

class TestCredentialTrustDomain:
    def test_local_trust_domain_does_not_expose_cloud_credentials(self):
        """A local-only worker must not receive cloud API keys."""
        config = _make_config(trust_domain="local")
        creds = _make_credentials(trust_domain="local")
        adapter = NativeWorkerAdapter()
        worker = adapter.start(config, credentials=creds)
        # The worker's environment should not contain cloud keys
        assert "OPENAI_API_KEY" not in worker.sanitized_env

    def test_cloud_trust_domain_can_receive_cloud_credentials(self):
        config = _make_config(trust_domain="cloud")
        creds = _make_credentials(trust_domain="cloud")
        adapter = NativeWorkerAdapter()
        worker = adapter.start(config, credentials=creds)
        # Cloud worker should have the key available
        assert worker.sanitized_env.get("OPENAI_API_KEY") == "sk-test-key"

    def test_credential_mismatch_raises_error(self):
        """A local worker cannot receive cloud-domain credentials."""
        config = _make_config(trust_domain="local")
        creds = _make_credentials(trust_domain="cloud")
        adapter = NativeWorkerAdapter()
        with pytest.raises(ValueError, match="trust_domain"):
            adapter.start(config, credentials=creds)


# ---------------------------------------------------------------------------
# Artifacts bind to the expected worktree/branch/diff
# ---------------------------------------------------------------------------

class TestArtifactBinding:
    def test_artifacts_record_worktree_and_branch(self):
        adapter = NativeWorkerAdapter()
        config = _make_config(
            worktree_path="/tmp/wt-test",
            branch="feature/test-artifacts",
        )
        worker = adapter.start(config)
        adapter.complete(worker, diff="--- a/file\n+++ b/file\n@@ -1 +1 @@\n-foo\n+bar")
        artifacts = adapter.collect_artifacts(worker)
        assert len(artifacts) > 0
        for artifact in artifacts:
            assert artifact.worktree_path == "/tmp/wt-test"
            assert artifact.branch == "feature/test-artifacts"

    def test_diff_artifact_captures_changes(self):
        adapter = NativeWorkerAdapter()
        config = _make_config()
        worker = adapter.start(config)
        test_diff = "--- a/src/app.py\n+++ b/src/app.py\n@@ -1 +1 @@\n-foo\n+bar\n"
        adapter.complete(worker, diff=test_diff)
        artifacts = adapter.collect_artifacts(worker)
        diff_artifacts = [a for a in artifacts if a.kind == "diff"]
        assert len(diff_artifacts) == 1
        assert diff_artifacts[0].content == test_diff

    def test_no_artifacts_from_incomplete_worker(self):
        adapter = NativeWorkerAdapter()
        config = _make_config()
        worker = adapter.start(config)
        # Worker is still running, not completed
        artifacts = adapter.collect_artifacts(worker)
        assert len(artifacts) == 0


# ---------------------------------------------------------------------------
# Multiple workers cannot overwrite each other's branches
# ---------------------------------------------------------------------------

class TestBranchIsolation:
    def test_two_workers_on_same_branch_raises_error(self):
        adapter = NativeWorkerAdapter()
        config1 = _make_config(worker_id="w1", branch="feature/shared")
        config2 = _make_config(worker_id="w2", branch="feature/shared")
        adapter.start(config1)
        with pytest.raises(ValueError, match="branch.*already.*active|branch.*locked"):
            adapter.start(config2)

    def test_two_workers_on_different_branches_succeed(self):
        adapter = NativeWorkerAdapter()
        config1 = _make_config(worker_id="w1", branch="feature/a")
        config2 = _make_config(worker_id="w2", branch="feature/b")
        w1 = adapter.start(config1)
        w2 = adapter.start(config2)
        assert w1.branch == "feature/a"
        assert w2.branch == "feature/b"
        assert w1.worker_id != w2.worker_id

    def test_completed_worker_releases_branch(self):
        adapter = NativeWorkerAdapter()
        config1 = _make_config(worker_id="w1", branch="feature/reusable")
        w1 = adapter.start(config1)
        adapter.complete(w1, diff="")
        # After completion, the branch should be released
        config2 = _make_config(worker_id="w2", branch="feature/reusable")
        w2 = adapter.start(config2)
        assert w2.branch == "feature/reusable"


# ---------------------------------------------------------------------------
# Merge proposal requires independent validation/review
# ---------------------------------------------------------------------------

class TestMergeProposalGuard:
    def test_merge_proposal_requires_validation(self):
        """A merge proposal artifact must include a validation_passed field
        that defaults to False (unvalidated)."""
        adapter = NativeWorkerAdapter()
        config = _make_config()
        worker = adapter.start(config)
        adapter.complete(worker, diff="some diff", validation_passed=False)
        artifacts = adapter.collect_artifacts(worker)
        merge_proposals = [a for a in artifacts if a.kind == "merge_proposal"]
        assert len(merge_proposals) == 1
        assert merge_proposals[0].metadata.get("validation_passed") is False

    def test_validated_merge_proposal_carries_evidence(self):
        adapter = NativeWorkerAdapter()
        config = _make_config()
        worker = adapter.start(config)
        adapter.complete(
            worker,
            diff="some diff",
            validation_passed=True,
            validation_codes=("tests_passed", "lint_clean"),
        )
        artifacts = adapter.collect_artifacts(worker)
        merge_proposals = [a for a in artifacts if a.kind == "merge_proposal"]
        assert len(merge_proposals) == 1
        assert merge_proposals[0].metadata.get("validation_passed") is True
        assert "tests_passed" in merge_proposals[0].metadata.get("validation_codes", [])

    def test_merge_proposal_does_not_auto_merge(self):
        """The merge proposal must not trigger an actual merge. It is a
        reviewable artifact only."""
        adapter = NativeWorkerAdapter()
        config = _make_config()
        worker = adapter.start(config)
        adapter.complete(worker, diff="some diff", validation_passed=True)
        artifacts = adapter.collect_artifacts(worker)
        merge_proposals = [a for a in artifacts if a.kind == "merge_proposal"]
        assert len(merge_proposals) == 1
        # The proposal must explicitly say merged=False
        assert merge_proposals[0].metadata.get("merged") is False

    def test_no_remote_publication(self):
        """The merge proposal must not trigger any remote push."""
        adapter = NativeWorkerAdapter()
        config = _make_config()
        worker = adapter.start(config)
        adapter.complete(worker, diff="some diff", validation_passed=True)
        artifacts = adapter.collect_artifacts(worker)
        merge_proposals = [a for a in artifacts if a.kind == "merge_proposal"]
        assert len(merge_proposals) == 1
        assert merge_proposals[0].metadata.get("pushed_remote") is False


# ---------------------------------------------------------------------------
# start_native_worker convenience function
# ---------------------------------------------------------------------------

class TestStartNativeWorker:
    def test_start_native_worker_returns_status(self):
        config = _make_config()
        worker = start_native_worker(config)
        assert isinstance(worker, NativeWorkerStatus)
        assert worker.state == WorkerState.RUNNING
        assert worker.worker_id == config.worker_id