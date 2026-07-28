from __future__ import annotations

import json
from typing import Any

from ..engineering.browser_validation import (
    BrowserAssertion,
    BrowserInteraction,
    BrowserValidationRequest,
    BrowserValidationService,
)
from ..runtime_models import ToolExecution, ToolSpec
from ..security_boundary import redact_text
from ..state_store import AgentStateStore
from ..validation_runner import ValidationContainerRunner
from .base import AgentTool, ToolContext
from .command_tools import _tool_call_from_runtime_arguments


class BrowserValidateTool(AgentTool):
    """Capture rendered proof inside Kestrel's digest-pinned OCI boundary."""

    wait_for_completion_on_timeout = True
    spec = ToolSpec(
        name="browser.validate",
        description=(
            "Run contained Playwright smoke, DOM, interaction, console/network, "
            "screenshot, and accessibility checks against the exact repair candidate."
        ),
        parameters={
            "type": "object",
            "properties": {
                "task_id": {"type": "string"},
                "candidate_id": {"type": "string"},
                "expected_candidate_digest": {
                    "type": "string",
                    "pattern": "^[0-9a-f]{64}$",
                },
                "image": {"type": "string"},
                "start_command": {
                    "type": "array",
                    "items": {"type": "string"},
                    "minItems": 1,
                    "maxItems": 64,
                },
                "target_url": {"type": "string"},
                "assertions": {"type": "array", "items": {"type": "object"}},
                "interactions": {"type": "array", "items": {"type": "object"}},
                "allowed_domains": {
                    "type": "array",
                    "items": {"type": "string"},
                },
                "network_fixtures": {"type": "object"},
                "timeout_seconds": {
                    "type": "number",
                    "minimum": 5,
                    "maximum": 600,
                },
            },
            "required": [
                "task_id",
                "expected_candidate_digest",
                "start_command",
                "target_url",
            ],
        },
        risk="high",
        requires_approval=True,
        capabilities=(
            "browser-validation",
            "visual-evidence",
            "accessibility",
            "oci-isolation",
        ),
        produces_validation=True,
    )
    needs_call_id = True

    def __init__(self, *, runner: ValidationContainerRunner | None = None) -> None:
        self.runner = runner

    def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolExecution:
        call = _tool_call_from_runtime_arguments(self.spec.name, arguments)
        if context.run_id is None:
            return self._result(
                call,
                success=False,
                content="Browser validation requires a durable run.",
                error="browser_run_required",
            )
        try:
            request = _request(arguments, context=context)
            service = BrowserValidationService(
                AgentStateStore(context.config.state_path),
                runner=self.runner,
            )
            record = service.validate(
                request,
                expected_candidate_digest=str(
                    arguments.get("expected_candidate_digest") or ""
                ),
            )
            payload = record.to_payload()
            return self._result(
                call,
                success=record.status == "passed",
                content=json.dumps(payload, indent=2),
                data=payload,
                error=(
                    None
                    if record.status == "passed"
                    else "browser_validation_failed"
                ),
            )
        except Exception as exc:  # noqa: BLE001 - tool boundary returns bounded failure
            return self._result(
                call,
                success=False,
                content=redact_text(str(exc)),
                error="browser_validation_error",
            )


def _request(
    arguments: dict[str, Any],
    *,
    context: ToolContext,
) -> BrowserValidationRequest:
    start_command = arguments.get("start_command")
    assertions = arguments.get("assertions", [])
    interactions = arguments.get("interactions", [])
    allowed_domains = arguments.get("allowed_domains", [])
    fixtures = arguments.get("network_fixtures", {})
    if not isinstance(start_command, list):
        raise ValueError("start_command must be a list")
    if not isinstance(assertions, list) or not all(
        isinstance(item, dict) for item in assertions
    ):
        raise ValueError("assertions must be a list of objects")
    if not isinstance(interactions, list) or not all(
        isinstance(item, dict) for item in interactions
    ):
        raise ValueError("interactions must be a list of objects")
    if not isinstance(allowed_domains, list):
        raise ValueError("allowed_domains must be a list")
    if not isinstance(fixtures, dict):
        raise ValueError("network_fixtures must be an object")
    return BrowserValidationRequest(
        run_id=str(context.run_id),
        task_id=str(arguments.get("task_id") or ""),
        candidate_id=(
            None
            if arguments.get("candidate_id") is None
            else str(arguments.get("candidate_id"))
        ),
        workspace=context.workspace,
        image=str(
            arguments.get("image")
            or context.config.validation_container_image
            or ""
        ),
        start_command=tuple(str(item) for item in start_command),
        target_url=str(arguments.get("target_url") or ""),
        assertions=tuple(
            BrowserAssertion(
                selector=str(item.get("selector") or ""),
                expectation=str(item.get("expectation") or ""),
                value=(
                    None if item.get("value") is None else str(item.get("value"))
                ),
            )
            for item in assertions
        ),
        interactions=tuple(
            BrowserInteraction(
                action=str(item.get("action") or ""),
                selector=str(item.get("selector") or ""),
                value=(
                    None if item.get("value") is None else str(item.get("value"))
                ),
            )
            for item in interactions
        ),
        allowed_domains=tuple(str(item) for item in allowed_domains),
        network_fixtures={
            str(key): dict(value)
            for key, value in fixtures.items()
            if isinstance(value, dict)
        },
        timeout_seconds=float(arguments.get("timeout_seconds", 90.0)),
    )
