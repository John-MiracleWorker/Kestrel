from __future__ import annotations

import json
from typing import Any

from ..engineering.github_workflow import GitHubWorkflowService
from ..runtime_models import ToolCall, ToolExecution, ToolSpec
from ..security_boundary import redact_text
from ..state_store import AgentStateStore
from .base import AgentTool, ToolContext


class GitHubCreatePullRequestTool(AgentTool):
    """Publish only a durable, reviewed GitHub change request."""

    spec = ToolSpec(
        name="github.pr.create",
        description=(
            "Push the exact reviewed local branch and create or reconcile its GitHub "
            "pull request. Both remote mutation and Git push must be enabled."
        ),
        parameters={
            "type": "object",
            "properties": {
                "request_id": {"type": "string"},
                "expected_request_digest": {
                    "type": "string",
                    "pattern": "^[0-9a-f]{64}$",
                },
            },
            "required": ["request_id", "expected_request_digest"],
        },
        risk="critical",
        requires_approval=True,
        capabilities=("github", "git-push", "remote-mutation", "review-gated"),
    )
    needs_call_id = True
    wait_for_completion_on_timeout = True

    def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolExecution:
        call = _call(self.spec.name, arguments)
        if not context.config.allow_git_push or not context.config.allow_remote_mutation:
            return self._result(
                call,
                success=False,
                content=(
                    "GitHub PR publication requires both allow_git_push and "
                    "allow_remote_mutation."
                ),
                data={
                    "allow_git_push": context.config.allow_git_push,
                    "allow_remote_mutation": context.config.allow_remote_mutation,
                },
                error="github_remote_mutation_disabled",
            )
        try:
            service = GitHubWorkflowService(AgentStateStore(context.config.state_path))
            request = service.get(str(arguments.get("request_id") or ""))
            _validate_context(request.run_id, request.project_id, context)
            published = service.publish(
                request.request_id,
                expected_request_digest=str(
                    arguments.get("expected_request_digest") or ""
                ),
            )
            payload = published.to_payload()
            return self._result(
                call,
                success=True,
                content=json.dumps(payload, indent=2),
                data=payload,
            )
        except Exception as exc:  # noqa: BLE001 - tool boundary returns bounded failure
            return self._result(
                call,
                success=False,
                content=redact_text(str(exc)),
                error="github_pr_publish_failed",
            )


class GitHubSyncPullRequestTool(AgentTool):
    """Read CI and review state into the durable originating run."""

    spec = ToolSpec(
        name="github.pr.sync",
        description=(
            "Read bounded GitHub pull-request checks, reviews, and comments into "
            "the durable Kestrel change request."
        ),
        parameters={
            "type": "object",
            "properties": {
                "request_id": {"type": "string"},
                "expected_request_digest": {
                    "type": "string",
                    "pattern": "^[0-9a-f]{64}$",
                },
            },
            "required": ["request_id", "expected_request_digest"],
        },
        risk="medium",
        requires_approval=False,
        capabilities=("github", "external-read", "ci-feedback", "review-feedback"),
    )
    wait_for_completion_on_timeout = True

    def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolExecution:
        call = _call(self.spec.name, arguments)
        if not context.config.allow_remote_mutation:
            return self._result(
                call,
                success=False,
                content="GitHub integration is disabled by allow_remote_mutation.",
                error="github_integration_disabled",
            )
        try:
            service = GitHubWorkflowService(AgentStateStore(context.config.state_path))
            request = service.get(str(arguments.get("request_id") or ""))
            _validate_context(request.run_id, request.project_id, context)
            synced = service.sync(
                request.request_id,
                expected_request_digest=str(
                    arguments.get("expected_request_digest") or ""
                ),
            )
            payload = synced.to_payload()
            return self._result(
                call,
                success=True,
                content=json.dumps(payload, indent=2),
                data=payload,
            )
        except Exception as exc:  # noqa: BLE001 - tool boundary returns bounded failure
            return self._result(
                call,
                success=False,
                content=redact_text(str(exc)),
                error="github_pr_sync_failed",
            )


def _call(name: str, arguments: dict[str, Any]) -> ToolCall:
    return ToolCall(
        id=str(arguments.get("_tool_call_id") or ""),
        name=name,
        arguments={
            key: value
            for key, value in arguments.items()
            if not str(key).startswith("_")
        },
    )


def _validate_context(
    run_id: str,
    project_id: str | None,
    context: ToolContext,
) -> None:
    if context.run_id != run_id:
        raise ValueError("GitHub request does not belong to the active run")
    if project_id is not None and context.project_id != project_id:
        raise ValueError("GitHub request does not belong to the active project")
