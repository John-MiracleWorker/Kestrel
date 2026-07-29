from __future__ import annotations

import hashlib
import json
import re
import shutil
import stat
import subprocess  # nosec B404
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlsplit

from ..repair_integrity import load_review_receipt, load_validation_receipt
from ..security_boundary import redact_secrets, redact_text, sanitized_subprocess_environment
from ..state_store import AgentStateStore, utc_now
from .schema import ensure_engineering_schema

_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,191}\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_GIT_SHA = re.compile(r"[0-9a-f]{40,64}\Z")
_BRANCH = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,254}\Z")
_TERMINAL_CHECK_FAILURES = {
    "ACTION_REQUIRED",
    "CANCELLED",
    "FAILURE",
    "STALE",
    "STARTUP_FAILURE",
    "TIMED_OUT",
}


class CommandRunner(Protocol):
    def __call__(
        self,
        command: list[str],
        *,
        cwd: Path,
        timeout: float,
    ) -> subprocess.CompletedProcess[str]: ...


@dataclass(frozen=True)
class GitHubFeedbackRecord:
    event_id: str
    request_id: str
    external_event_id: str
    kind: str
    status: str
    payload: dict[str, Any]
    evidence_refs: tuple[str, ...]
    created_at: str

    def to_payload(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["evidence_refs"] = list(self.evidence_refs)
        return payload


@dataclass(frozen=True)
class GitHubChangeRequestRecord:
    request_id: str
    run_id: str
    project_id: str | None
    review_id: str
    validation_id: str
    candidate_digest: str
    source_head_sha: str
    reviewed_commit_sha: str | None
    base_branch: str
    head_branch: str
    title: str
    body: str
    request_digest: str
    status: str
    external_number: int | None
    external_url: str | None
    publish_receipt: dict[str, Any]
    recovery_run_id: str | None
    actor: str
    created_at: str
    updated_at: str
    feedback: tuple[GitHubFeedbackRecord, ...] = ()

    def to_payload(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["feedback"] = [item.to_payload() for item in self.feedback]
        payload["publish_tool_request"] = {
            "tool_name": "github.pr.create",
            "arguments": {
                "request_id": self.request_id,
                "expected_request_digest": self.request_digest,
            },
            "requires_exact_call_approval": True,
            "required_enablement": [
                "allow_git_push",
                "allow_remote_mutation",
            ],
        }
        payload["sync_tool_request"] = {
            "tool_name": "github.pr.sync",
            "arguments": {
                "request_id": self.request_id,
                "expected_request_digest": self.request_digest,
            },
            "requires_exact_call_approval": False,
            "required_enablement": ["allow_remote_mutation"],
        }
        return payload


class GitHubWorkflowService:
    """Bind GitHub publication and feedback to trusted repair evidence."""

    def __init__(
        self,
        state: AgentStateStore,
        *,
        runner: CommandRunner | None = None,
    ) -> None:
        self.state = state
        self.runner = runner or _run_command
        ensure_engineering_schema(state)

    def prepare(
        self,
        *,
        request_id: str,
        run_id: str,
        review_id: str,
        title: str,
        base_branch: str,
        head_branch: str | None,
        actor: str,
    ) -> GitHubChangeRequestRecord:
        request_key = _identifier(request_id, "request_id")
        run_key = _identifier(run_id, "run_id")
        run = self.state.get_run(run_key)
        workspace = Path(run.workspace)
        review = load_review_receipt(workspace, _identifier(review_id, "review_id"))
        validation_id = _identifier(review.get("validation_id"), "validation_id")
        validation = load_validation_receipt(workspace, validation_id)
        snapshot = review.get("repair_snapshot")
        validation_snapshot = validation.get("repair_snapshot")
        if (
            not isinstance(snapshot, dict)
            or not isinstance(validation_snapshot, dict)
            or validation.get("success") is not True
            or review.get("commit_gate", {}).get("commit_allowed") is not True
            or snapshot.get("diff_digest") != validation_snapshot.get("diff_digest")
            or snapshot.get("diff_digest") != review.get("diff_digest")
        ):
            raise ValueError("repair review and validation evidence are stale or mismatched")
        candidate_digest = _digest(snapshot.get("diff_digest"), "candidate_digest")
        source_head = _git_sha(snapshot.get("head_sha"), "source_head_sha")
        review_branch = _branch(snapshot.get("branch"), "head_branch")
        resolved_head = _branch(head_branch or review_branch, "head_branch")
        if resolved_head != review_branch:
            raise ValueError("pull request head branch does not match the reviewed branch")
        base = _branch(base_branch, "base_branch")
        if base == resolved_head:
            raise ValueError("pull request base and head branches must differ")
        reviewed_commit = _current_reviewed_commit(
            workspace,
            source_head=source_head,
            snapshot=snapshot,
            expected_branch=resolved_head,
        )
        body = _pull_request_body(
            run=run,
            review=review,
            validation=validation,
            snapshot=snapshot,
        )
        payload = {
            "schema": "kestrel.github_change_request.v1",
            "run_id": run_key,
            "project_id": run.project_id,
            "review_id": review_id,
            "validation_id": validation_id,
            "candidate_digest": candidate_digest,
            "source_head_sha": source_head,
            "reviewed_commit_sha": reviewed_commit,
            "base_branch": base,
            "head_branch": resolved_head,
            "title": _text(title, "title", 256),
            "body": body,
        }
        request_digest = _hash(payload)
        actor_text = _text(actor, "actor", 160)
        now = utc_now()
        with self.state._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute(
                "SELECT * FROM github_change_requests WHERE request_id = ?",
                (request_key,),
            ).fetchone()
            if existing is not None:
                current = self.get(request_key)
                if current.request_digest != request_digest:
                    raise ValueError("github_change_request_identity_conflict")
                return current
            conn.execute(
                """
                INSERT INTO github_change_requests (
                    request_id, run_id, project_id, review_id, validation_id,
                    candidate_digest, source_head_sha, reviewed_commit_sha,
                    base_branch, head_branch, title, body, request_digest,
                    status, external_number, external_url, publish_receipt_json,
                    recovery_run_id, actor, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'prepared',
                    NULL, NULL, '{}', NULL, ?, ?, ?)
                """,
                (
                    request_key,
                    run_key,
                    run.project_id,
                    review_id,
                    validation_id,
                    candidate_digest,
                    source_head,
                    reviewed_commit,
                    base,
                    resolved_head,
                    payload["title"],
                    body,
                    request_digest,
                    actor_text,
                    now,
                    now,
                ),
            )
        return self.get(request_key)

    def publish(
        self,
        request_id: str,
        *,
        expected_request_digest: str,
    ) -> GitHubChangeRequestRecord:
        request = self.get(request_id)
        expected = _digest(expected_request_digest, "expected_request_digest")
        if request.request_digest != expected:
            raise ValueError("GitHub request digest changed after approval")
        if request.status in {"published", "ci_passed", "ci_failed", "changes_requested"}:
            return request
        workspace = Path(self.state.get_run(request.run_id).workspace)
        review = load_review_receipt(workspace, request.review_id)
        snapshot = review.get("repair_snapshot")
        if not isinstance(snapshot, dict):
            raise ValueError("GitHub request review has no trusted candidate snapshot")
        commit_sha = _current_reviewed_commit(
            workspace,
            source_head=request.source_head_sha,
            snapshot=snapshot,
            expected_branch=request.head_branch,
        )
        if commit_sha is None:
            raise ValueError(
                "reviewed candidate must be committed locally before pull request publication"
            )
        if (
            request.reviewed_commit_sha is not None
            and request.reviewed_commit_sha != commit_sha
        ):
            raise ValueError("reviewed commit changed after pull request preparation")
        repository = _github_repository(workspace, runner=self.runner)
        push = self.runner(
            [
                _executable("git"),
                "push",
                "--set-upstream",
                "origin",
                request.head_branch,
            ],
            cwd=workspace,
            timeout=120.0,
        )
        if push.returncode != 0:
            self._record_publish_failure(
                request,
                status="publish_failed",
                stage="git_push",
                result=push,
                reviewed_commit_sha=commit_sha,
            )
            raise RuntimeError(redact_text(push.stderr or "Git push failed."))
        existing = _view_pull_request(
            request,
            workspace=workspace,
            repository=repository,
            runner=self.runner,
        )
        if existing is None:
            created = self.runner(
                [
                    _executable("gh"),
                    "pr",
                    "create",
                    "--repo",
                    repository,
                    "--base",
                    request.base_branch,
                    "--head",
                    request.head_branch,
                    "--title",
                    request.title,
                    "--body",
                    request.body,
                ],
                cwd=workspace,
                timeout=120.0,
            )
            if created.returncode != 0:
                self._record_publish_failure(
                    request,
                    status="publish_partial",
                    stage="gh_pr_create",
                    result=created,
                    reviewed_commit_sha=commit_sha,
                )
                raise RuntimeError(redact_text(created.stderr or "Pull request creation failed."))
            existing = _view_pull_request(
                request,
                workspace=workspace,
                repository=repository,
                runner=self.runner,
            )
        if existing is None:
            raise RuntimeError("Pull request was created but could not be reconciled.")
        url = _github_url(existing.get("url"))
        number = _positive_int(existing.get("number"), "pull request number")
        receipt = {
            "schema": "kestrel.github_publish_receipt.v1",
            "repository": repository,
            "number": number,
            "url": url,
            "reviewed_commit_sha": commit_sha,
            "request_digest": request.request_digest,
            "published_at": utc_now(),
            "reconciled_existing": bool(existing.get("_reconciled_existing")),
        }
        with self.state._connect() as conn:
            conn.execute(
                """
                UPDATE github_change_requests
                SET status = 'published', reviewed_commit_sha = ?,
                    external_number = ?, external_url = ?,
                    publish_receipt_json = ?, updated_at = ?
                WHERE request_id = ? AND request_digest = ?
                """,
                (
                    commit_sha,
                    number,
                    url,
                    _json(receipt),
                    utc_now(),
                    request.request_id,
                    request.request_digest,
                ),
            )
        return self.get(request.request_id)

    def sync(
        self,
        request_id: str,
        *,
        expected_request_digest: str,
    ) -> GitHubChangeRequestRecord:
        request = self.get(request_id)
        if request.request_digest != _digest(
            expected_request_digest,
            "expected_request_digest",
        ):
            raise ValueError("GitHub request digest changed before feedback sync")
        if request.external_number is None:
            raise ValueError("pull request must be published before feedback sync")
        workspace = Path(self.state.get_run(request.run_id).workspace)
        repository = _github_repository(workspace, runner=self.runner)
        viewed = self.runner(
            [
                _executable("gh"),
                "pr",
                "view",
                str(request.external_number),
                "--repo",
                repository,
                "--json",
                "number,url,state,reviewDecision,statusCheckRollup,comments,reviews,headRefOid",
            ],
            cwd=workspace,
            timeout=60.0,
        )
        if viewed.returncode != 0:
            raise RuntimeError(redact_text(viewed.stderr or "GitHub feedback sync failed."))
        payload = _safe_github_payload(viewed.stdout)
        status = _feedback_status(payload)
        external_event_id = _hash(
            {
                "request_id": request.request_id,
                "payload": payload,
            }
        )
        event_id = "github_feedback_" + external_event_id[:24]
        now = utc_now()
        with self.state._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                """
                INSERT OR IGNORE INTO github_feedback_events (
                    event_id, request_id, external_event_id, kind, status,
                    payload_json, evidence_refs_json, created_at
                ) VALUES (?, ?, ?, 'pull_request_snapshot', ?, ?, ?, ?)
                """,
                (
                    event_id,
                    request.request_id,
                    external_event_id,
                    status,
                    _json(payload),
                    _json(
                        [
                            request.external_url,
                            f"github-pr:{request.external_number}",
                        ]
                    ),
                    now,
                ),
            )
            conn.execute(
                """
                UPDATE github_change_requests
                SET status = ?, updated_at = ?
                WHERE request_id = ?
                """,
                (status, now, request.request_id),
            )
        return self.get(request.request_id)

    def bind_recovery_run(
        self,
        request_id: str,
        *,
        recovery_run_id: str,
    ) -> GitHubChangeRequestRecord:
        request = self.get(request_id)
        if request.status not in {"ci_failed", "changes_requested"}:
            raise ValueError("GitHub request has no actionable failed feedback")
        recovery = self.state.get_run(_identifier(recovery_run_id, "recovery_run_id"))
        if recovery.project_id != request.project_id:
            raise ValueError("GitHub recovery run belongs to a different project")
        with self.state._connect() as conn:
            cursor = conn.execute(
                """
                UPDATE github_change_requests
                SET recovery_run_id = ?, updated_at = ?
                WHERE request_id = ? AND recovery_run_id IS NULL
                """,
                (recovery.run_id, utc_now(), request.request_id),
            )
        if cursor.rowcount != 1:
            current = self.get(request.request_id)
            if current.recovery_run_id != recovery.run_id:
                raise ValueError("GitHub recovery run was already selected")
        return self.get(request.request_id)

    def get(self, request_id: str) -> GitHubChangeRequestRecord:
        request_key = _identifier(request_id, "request_id")
        with self.state._connect() as conn:
            row = conn.execute(
                "SELECT * FROM github_change_requests WHERE request_id = ?",
                (request_key,),
            ).fetchone()
            feedback = conn.execute(
                """
                SELECT * FROM github_feedback_events
                WHERE request_id = ?
                ORDER BY created_at ASC, event_id ASC
                """,
                (request_key,),
            ).fetchall()
        if row is None:
            raise KeyError(f"Unknown GitHub change request: {request_key}")
        return _change_request(
            row,
            tuple(_feedback_record(item) for item in feedback),
        )

    def list(self, *, run_id: str | None = None) -> list[GitHubChangeRequestRecord]:
        params: list[object] = []
        sql = "SELECT request_id FROM github_change_requests"
        if run_id is not None:
            sql += " WHERE run_id = ?"
            params.append(_identifier(run_id, "run_id"))
        sql += " ORDER BY created_at ASC, request_id ASC"
        with self.state._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [self.get(str(row["request_id"])) for row in rows]

    def _record_publish_failure(
        self,
        request: GitHubChangeRequestRecord,
        *,
        status: str,
        stage: str,
        result: subprocess.CompletedProcess[str],
        reviewed_commit_sha: str,
    ) -> None:
        receipt = {
            "schema": "kestrel.github_publish_failure.v1",
            "stage": stage,
            "returncode": result.returncode,
            "stdout": redact_text(result.stdout or ""),
            "stderr": redact_text(result.stderr or ""),
            "reviewed_commit_sha": reviewed_commit_sha,
            "request_digest": request.request_digest,
            "recorded_at": utc_now(),
        }
        with self.state._connect() as conn:
            conn.execute(
                """
                UPDATE github_change_requests
                SET status = ?, reviewed_commit_sha = ?,
                    publish_receipt_json = ?, updated_at = ?
                WHERE request_id = ?
                """,
                (
                    status,
                    reviewed_commit_sha,
                    _json(receipt),
                    utc_now(),
                    request.request_id,
                ),
            )


def _pull_request_body(
    *,
    run: Any,
    review: dict[str, Any],
    validation: dict[str, Any],
    snapshot: dict[str, Any],
) -> str:
    changed_files = [
        str(item)
        for item in snapshot.get("changed_files", [])
        if isinstance(item, str)
    ]
    risks = [
        str(item)
        for item in review.get("risks", [])
        if isinstance(item, str) and item.strip()
    ]
    summary = str(review.get("summary") or "").strip()
    lines = [
        "## Objective",
        "",
        str(run.message).strip(),
        "",
        "## Approach",
        "",
        summary or "Apply the reviewed repair candidate without expanding its scope.",
        "",
        "## Changed files",
        "",
        *(f"- `{path}`" for path in changed_files),
        "",
        "## Validation",
        "",
        f"- Signed validation: `{validation.get('validation_id')}`",
        f"- Candidate digest: `{snapshot.get('diff_digest')}`",
        f"- Result: {'passed' if validation.get('success') is True else 'failed'}",
        "",
        "## Known risks",
        "",
        *(f"- {risk}" for risk in risks),
        *(["- No reviewer risk notes were recorded."] if not risks else []),
        "",
        "## Rollback",
        "",
        "- Revert the reviewed commit or use the recorded Kestrel repair rollback artifact.",
        "",
        "## Kestrel provenance",
        "",
        f"- Run: `{run.run_id}`",
        f"- Review: `{review.get('review_id')}`",
        f"- Validation: `{validation.get('validation_id')}`",
    ]
    body = "\n".join(lines).strip()
    return _text(body, "pull request body", 64_000)


def _current_reviewed_commit(
    workspace: Path,
    *,
    source_head: str,
    snapshot: dict[str, Any],
    expected_branch: str,
) -> str | None:
    branch = _git_text(workspace, ["branch", "--show-current"])
    if branch != expected_branch:
        raise ValueError("current branch does not match the reviewed repair branch")
    current_head = _git_text(workspace, ["rev-parse", "HEAD"])
    if current_head == source_head:
        current_digest = snapshot.get("diff_digest")
        if current_digest is None:
            raise ValueError("review candidate digest is missing")
        return None
    _verify_reviewed_commit(
        workspace,
        source_head=source_head,
        commit_sha=current_head,
        snapshot=snapshot,
    )
    status_text = _git_text(workspace, ["status", "--porcelain=v1", "--untracked-files=all"])
    public_changes = [
        line
        for line in status_text.splitlines()
        if not _private_repair_status_path(line)
    ]
    if public_changes:
        raise ValueError("working tree changed after the reviewed commit")
    return current_head


def _verify_reviewed_commit(
    workspace: Path,
    *,
    source_head: str,
    commit_sha: str,
    snapshot: dict[str, Any],
) -> None:
    commit = _git_sha(commit_sha, "commit_sha")
    parent = _git_text(workspace, ["rev-parse", f"{commit}^"])
    if parent != source_head:
        raise ValueError("reviewed commit parent does not match the review source HEAD")
    expected_paths = sorted(str(item) for item in snapshot.get("changed_files", []))
    actual_paths = sorted(
        item
        for item in _git_text(
            workspace,
            ["diff", "--name-only", "--no-renames", source_head, commit, "--"],
        ).splitlines()
        if item
    )
    if expected_paths != actual_paths:
        raise ValueError("reviewed commit changed-file set does not match review evidence")
    manifest = snapshot.get("changed_manifest")
    if not isinstance(manifest, list):
        raise ValueError("review snapshot has no changed-file manifest")
    for raw in manifest:
        if not isinstance(raw, dict):
            raise ValueError("review snapshot changed-file manifest is invalid")
        _verify_manifest_entry(workspace, commit=commit, entry=raw)


def _verify_manifest_entry(workspace: Path, *, commit: str, entry: dict[str, Any]) -> None:
    path = str(entry.get("path") or "")
    entry_type = str(entry.get("type") or "")
    tree = _git_text(workspace, ["ls-tree", commit, "--", path])
    if entry_type == "deleted":
        if tree:
            raise ValueError(f"reviewed deletion is present in commit: {path}")
        return
    if not tree:
        raise ValueError(f"reviewed path is missing from commit: {path}")
    parts = tree.split(maxsplit=3)
    if len(parts) < 3:
        raise ValueError(f"reviewed path has invalid Git tree metadata: {path}")
    mode = parts[0]
    blob = _git_bytes(workspace, ["show", f"{commit}:{path}"])
    expected_size = entry.get("size")
    expected_digest = str(entry.get("sha256") or "")
    if len(blob) != expected_size or hashlib.sha256(blob).hexdigest() != expected_digest:
        raise ValueError(f"reviewed path content changed in commit: {path}")
    if entry_type == "symlink":
        if mode != "120000":
            raise ValueError(f"reviewed symlink changed type in commit: {path}")
        return
    if entry_type != "regular":
        raise ValueError(f"reviewed path has unsupported type: {path}")
    expected_mode = int(entry.get("mode") or 0)
    actual_mode = 0o755 if mode == "100755" else 0o644 if mode == "100644" else 0
    if stat.S_IMODE(expected_mode) != actual_mode:
        raise ValueError(f"reviewed path mode changed in commit: {path}")


def _view_pull_request(
    request: GitHubChangeRequestRecord,
    *,
    workspace: Path,
    repository: str,
    runner: CommandRunner,
) -> dict[str, Any] | None:
    viewed = runner(
        [
            _executable("gh"),
            "pr",
            "view",
            request.head_branch,
            "--repo",
            repository,
            "--json",
            "number,url,state,headRefOid",
        ],
        cwd=workspace,
        timeout=60.0,
    )
    if viewed.returncode != 0:
        return None
    payload = _safe_github_payload(viewed.stdout)
    head_oid = str(payload.get("headRefOid") or "")
    if request.reviewed_commit_sha is not None and head_oid != request.reviewed_commit_sha:
        raise ValueError("existing pull request head does not match the reviewed commit")
    payload["_reconciled_existing"] = True
    return payload


def _github_repository(workspace: Path, *, runner: CommandRunner) -> str:
    completed = runner(
        [_executable("git"), "remote", "get-url", "origin"],
        cwd=workspace,
        timeout=30.0,
    )
    if completed.returncode != 0:
        raise ValueError("Git origin remote is unavailable")
    raw = str(completed.stdout or "").strip()
    if raw.startswith("git@github.com:"):
        path = raw.removeprefix("git@github.com:")
    else:
        parsed = urlsplit(raw)
        if parsed.scheme != "https" or parsed.hostname != "github.com":
            raise ValueError("GitHub PR publication requires a github.com origin")
        if parsed.username is not None or parsed.password is not None:
            raise ValueError("GitHub origin must not contain credentials")
        path = parsed.path.lstrip("/")
    repository = path.removesuffix(".git").strip("/")
    parts = repository.split("/")
    if len(parts) != 2 or any(
        re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,99}", item) is None
        for item in parts
    ):
        raise ValueError("GitHub origin owner/repository is invalid")
    return repository


def _feedback_status(payload: dict[str, Any]) -> str:
    state = str(payload.get("state") or "").upper()
    if state == "MERGED":
        return "merged"
    if state == "CLOSED":
        return "closed"
    if str(payload.get("reviewDecision") or "").upper() == "CHANGES_REQUESTED":
        return "changes_requested"
    checks = payload.get("statusCheckRollup")
    if isinstance(checks, list):
        conclusions = {
            str(item.get("conclusion") or item.get("state") or "").upper()
            for item in checks
            if isinstance(item, dict)
        }
        if conclusions & _TERMINAL_CHECK_FAILURES:
            return "ci_failed"
        if checks and all(
            str(item.get("conclusion") or item.get("state") or "").upper()
            in {"SUCCESS", "NEUTRAL", "SKIPPED"}
            for item in checks
            if isinstance(item, dict)
        ):
            return "ci_passed"
    return "published"


def _private_repair_status_path(line: str) -> bool:
    path = line[3:] if len(line) > 3 else line
    if " -> " in path:
        path = path.split(" -> ", maxsplit=1)[1]
    normalized = path.strip().strip('"').replace("\\", "/")
    return normalized == ".nest" or normalized.startswith(
        (
            ".nest/",
            ".kestrel/improvements/",
        )
    )


def _safe_github_payload(value: str) -> dict[str, Any]:
    if len(value.encode("utf-8", errors="replace")) > 2_000_000:
        raise ValueError("GitHub response exceeds the 2 MiB bound")
    try:
        payload = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ValueError("GitHub response is invalid JSON") from exc
    if not isinstance(payload, dict):
        raise ValueError("GitHub response must be an object")
    safe = redact_secrets(payload)
    if not isinstance(safe, dict):
        raise ValueError("GitHub response redaction failed")
    return safe


def _change_request(
    row: Any,
    feedback: tuple[GitHubFeedbackRecord, ...],
) -> GitHubChangeRequestRecord:
    receipt = _load(row["publish_receipt_json"], {})
    return GitHubChangeRequestRecord(
        request_id=str(row["request_id"]),
        run_id=str(row["run_id"]),
        project_id=_optional(row["project_id"]),
        review_id=str(row["review_id"]),
        validation_id=str(row["validation_id"]),
        candidate_digest=str(row["candidate_digest"]),
        source_head_sha=str(row["source_head_sha"]),
        reviewed_commit_sha=_optional(row["reviewed_commit_sha"]),
        base_branch=str(row["base_branch"]),
        head_branch=str(row["head_branch"]),
        title=str(row["title"]),
        body=str(row["body"]),
        request_digest=str(row["request_digest"]),
        status=str(row["status"]),
        external_number=(
            None if row["external_number"] is None else int(row["external_number"])
        ),
        external_url=_optional(row["external_url"]),
        publish_receipt=dict(receipt) if isinstance(receipt, dict) else {},
        recovery_run_id=_optional(row["recovery_run_id"]),
        actor=str(row["actor"]),
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
        feedback=feedback,
    )


def _feedback_record(row: Any) -> GitHubFeedbackRecord:
    payload = _load(row["payload_json"], {})
    evidence = _load(row["evidence_refs_json"], [])
    return GitHubFeedbackRecord(
        event_id=str(row["event_id"]),
        request_id=str(row["request_id"]),
        external_event_id=str(row["external_event_id"]),
        kind=str(row["kind"]),
        status=str(row["status"]),
        payload=dict(payload) if isinstance(payload, dict) else {},
        evidence_refs=tuple(str(item) for item in evidence),
        created_at=str(row["created_at"]),
    )


def _run_command(
    command: list[str],
    *,
    cwd: Path,
    timeout: float,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603
        command,
        cwd=cwd,
        env=sanitized_subprocess_environment(),
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def _git_text(workspace: Path, arguments: list[str]) -> str:
    completed = _run_command(
        [_executable("git"), *arguments],
        cwd=workspace,
        timeout=30.0,
    )
    if completed.returncode != 0:
        raise ValueError(redact_text(completed.stderr or "Git command failed."))
    return str(completed.stdout or "").strip()


def _git_bytes(workspace: Path, arguments: list[str]) -> bytes:
    completed = subprocess.run(  # noqa: S603
        [_executable("git"), *arguments],
        cwd=workspace,
        env=sanitized_subprocess_environment(),
        capture_output=True,
        timeout=30.0,
        check=False,
    )
    if completed.returncode != 0:
        raise ValueError(
            redact_text(completed.stderr.decode("utf-8", errors="replace"))
        )
    return bytes(completed.stdout)


def _executable(name: str) -> str:
    resolved = shutil.which(name)
    if resolved is None:
        raise RuntimeError(f"Required executable is unavailable: {name}")
    return str(Path(resolved).resolve())


def _github_url(value: Any) -> str:
    text = str(value or "").strip()
    parsed = urlsplit(text)
    if (
        parsed.scheme != "https"
        or parsed.hostname != "github.com"
        or parsed.username is not None
        or parsed.password is not None
        or parsed.port is not None
        or parsed.query
        or parsed.fragment
        or re.fullmatch(
            r"/[A-Za-z0-9][A-Za-z0-9_.-]{0,99}/"
            r"[A-Za-z0-9][A-Za-z0-9_.-]{0,99}/pull/[1-9][0-9]*",
            parsed.path,
        )
        is None
    ):
        raise ValueError("GitHub returned an invalid pull request URL")
    return text


def _positive_int(value: Any, field: str) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{field} is invalid")
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} is invalid") from exc
    if number < 1:
        raise ValueError(f"{field} is invalid")
    return number


def _identifier(value: Any, field: str) -> str:
    text = str(value or "").strip()
    if _IDENTIFIER.fullmatch(text) is None:
        raise ValueError(f"{field} has an invalid identifier")
    return text


def _digest(value: Any, field: str) -> str:
    text = str(value or "").strip()
    if _SHA256.fullmatch(text) is None:
        raise ValueError(f"{field} must be a SHA-256 digest")
    return text


def _git_sha(value: Any, field: str) -> str:
    text = str(value or "").strip()
    if _GIT_SHA.fullmatch(text) is None:
        raise ValueError(f"{field} is not a Git object id")
    return text


def _branch(value: Any, field: str) -> str:
    text = str(value or "").strip()
    if (
        _BRANCH.fullmatch(text) is None
        or ".." in text
        or text.endswith(("/", "."))
        or "@{" in text
    ):
        raise ValueError(f"{field} is invalid")
    return text


def _text(value: Any, field: str, maximum: int) -> str:
    text = str(value or "").strip()
    if not text or len(text) > maximum:
        raise ValueError(f"{field} must contain between 1 and {maximum} characters")
    if redact_text(text) != text:
        raise ValueError(f"{field} contains sensitive material")
    return text


def _hash(value: Any) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _json(value: Any) -> str:
    try:
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
    except (RecursionError, TypeError, ValueError) as exc:
        raise ValueError("value must be finite JSON") from exc


def _load(value: Any, fallback: Any) -> Any:
    try:
        return json.loads(str(value))
    except (TypeError, ValueError):
        return fallback


def _optional(value: Any) -> str | None:
    return None if value is None else str(value)
