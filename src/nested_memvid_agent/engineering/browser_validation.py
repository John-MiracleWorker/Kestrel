from __future__ import annotations

import base64
import binascii
import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from uuid import uuid4

from ..extension_runner import OCIContainerRunner
from ..repair_integrity import repair_snapshot
from ..security_boundary import redact_secrets, redact_text
from ..state_store import AgentStateStore, utc_now
from ..validation_runner import (
    ValidationContainerRunner,
    run_isolated_validation,
)
from .schema import ensure_engineering_schema

_PINNED_IMAGE = re.compile(r"[^@\s]+@sha256:[0-9a-f]{64}\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_SELECTOR_LIMIT = 1000
_SCREENSHOT_LIMIT = 256 * 1024
_REPORT_LIMIT = 512 * 1024
_SPEC_LIMIT = 128 * 1024
_SERIOUS_IMPACTS = {"serious", "critical"}


@dataclass(frozen=True)
class BrowserAssertion:
    selector: str
    expectation: str
    value: str | None = None


@dataclass(frozen=True)
class BrowserInteraction:
    action: str
    selector: str
    value: str | None = None


@dataclass(frozen=True)
class BrowserValidationRequest:
    run_id: str
    task_id: str
    candidate_id: str | None
    workspace: Path
    image: str
    start_command: tuple[str, ...]
    target_url: str
    assertions: tuple[BrowserAssertion, ...] = ()
    interactions: tuple[BrowserInteraction, ...] = ()
    allowed_domains: tuple[str, ...] = ()
    network_fixtures: dict[str, dict[str, Any]] | None = None
    timeout_seconds: float = 90.0


@dataclass(frozen=True)
class BrowserValidationRecord:
    validation_id: str
    run_id: str
    task_id: str
    candidate_id: str | None
    candidate_digest: str
    image: str
    target_url: str
    status: str
    failure_codes: tuple[str, ...]
    network_policy: dict[str, Any]
    report: dict[str, Any]
    screenshot_sha256: str | None
    evidence_refs: tuple[str, ...]
    created_at: str

    def to_payload(self) -> dict[str, Any]:
        return {
            "validation_id": self.validation_id,
            "run_id": self.run_id,
            "task_id": self.task_id,
            "candidate_id": self.candidate_id,
            "candidate_digest": self.candidate_digest,
            "image": self.image,
            "target_url": self.target_url,
            "status": self.status,
            "failure_codes": list(self.failure_codes),
            "network_policy": self.network_policy,
            "report": self.report,
            "screenshot_sha256": self.screenshot_sha256,
            "evidence_refs": list(self.evidence_refs),
            "created_at": self.created_at,
        }


class BrowserValidationService:
    """Run Playwright evidence in a digest-pinned, network-none OCI snapshot."""

    def __init__(
        self,
        state: AgentStateStore,
        *,
        runner: ValidationContainerRunner | None = None,
    ) -> None:
        self.state = state
        self.runner = runner or OCIContainerRunner()
        ensure_engineering_schema(state)

    def validate(
        self,
        request: BrowserValidationRequest,
        *,
        expected_candidate_digest: str,
    ) -> BrowserValidationRecord:
        run = self.state.get_run(request.run_id)
        task = self.state.get_task_node(request.task_id)
        if task.run_id != request.run_id:
            raise ValueError("browser validation task does not belong to the run")
        requested_workspace = Path(request.workspace).expanduser().resolve(strict=True)
        run_workspace = Path(run.workspace).expanduser().resolve(strict=True)
        workspace = run_workspace
        if request.candidate_id is not None:
            candidate_workspace = self._candidate_workspace(
                request.candidate_id,
                run_id=request.run_id,
                task_id=request.task_id,
            )
            if candidate_workspace is None:
                raise ValueError("browser validation candidate binding is invalid")
            workspace = candidate_workspace
        elif requested_workspace != run_workspace:
            raise ValueError("browser validation workspace is not the run workspace")
        image = str(request.image).strip()
        if _PINNED_IMAGE.fullmatch(image) is None:
            raise ValueError("browser validation image must be digest-pinned")
        expected_digest = _digest(
            expected_candidate_digest, "expected_candidate_digest"
        )
        snapshot = repair_snapshot(workspace)
        if snapshot["diff_digest"] != expected_digest:
            raise ValueError("browser validation candidate digest is stale")
        target_url = _container_local_url(request.target_url)
        start_command = _command(request.start_command)
        assertions = _assertions(request.assertions)
        interactions = _interactions(request.interactions)
        network_policy, fixtures = _network_policy(
            request.allowed_domains,
            request.network_fixtures or {},
        )
        timeout = _timeout(request.timeout_seconds)
        spec = {
            "schema": "kestrel.browser_validation_request.v1",
            "start_command": list(start_command),
            "target_url": target_url,
            "assertions": assertions,
            "interactions": interactions,
            "network_policy": network_policy,
            "network_fixtures": fixtures,
            "capture": {
                "screenshot": True,
                "dom_summary": True,
                "console_errors": True,
                "network_errors": True,
                "accessibility": "axe_serious_and_critical",
            },
            "limits": {
                "timeout_seconds": timeout,
                "screenshot_bytes": _SCREENSHOT_LIMIT,
            },
        }
        encoded_spec = _json(spec).encode()
        if len(encoded_spec) > _SPEC_LIMIT:
            raise ValueError("browser validation spec exceeds the 128 KiB bound")
        spec_argument = base64.urlsafe_b64encode(encoded_spec).decode()
        completed = run_isolated_validation(
            workspace=workspace,
            image=image,
            command=[
                "/opt/kestrel/browser-validate",
                "--spec-base64url",
                spec_argument,
            ],
            timeout_seconds=timeout,
            expected_repair_snapshot=snapshot,
            runner=self.runner,
        )
        if completed.returncode != 0:
            raise ValueError(
                f"browser validation container exited with {completed.returncode}"
            )
        report = _report(
            completed.stdout,
            expected_target=target_url,
            expected_assertions=assertions,
            expected_interactions=interactions,
        )
        screenshot_sha = _normalize_screenshot(report)
        failure_codes = _failure_codes(report)
        status = "passed" if not failure_codes else "failed"
        validation_id = "browser_validation_" + hashlib.sha256(
            (
                f"{request.run_id}:{request.task_id}:{expected_digest}:"
                f"{image}:{uuid4().hex}"
            ).encode()
        ).hexdigest()[:24]
        evidence_refs = (
            f"browser_validation:{validation_id}",
            f"candidate_digest:{expected_digest}",
        )
        created_at = utc_now()
        safe_report = redact_secrets(report)
        if not isinstance(safe_report, dict):
            raise ValueError("browser validation report could not be redacted safely")
        with self.state._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                """
                INSERT INTO browser_validations (
                    validation_id, run_id, task_id, candidate_id,
                    candidate_digest, image, target_url, status,
                    failure_codes_json, network_policy_json, report_json,
                    screenshot_sha256, evidence_refs_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    validation_id,
                    request.run_id,
                    request.task_id,
                    request.candidate_id,
                    expected_digest,
                    image,
                    target_url,
                    status,
                    _json(failure_codes),
                    _json(network_policy),
                    _json(safe_report),
                    screenshot_sha,
                    _json(evidence_refs),
                    created_at,
                ),
            )
        return self.get(validation_id)

    def get(self, validation_id: str) -> BrowserValidationRecord:
        with self.state._connect() as conn:
            row = conn.execute(
                "SELECT * FROM browser_validations WHERE validation_id = ?",
                (validation_id,),
            ).fetchone()
        if row is None:
            raise KeyError(f"Unknown browser validation: {validation_id}")
        return _record(row)

    def list(
        self,
        *,
        run_id: str,
        candidate_id: str | None = None,
    ) -> list[BrowserValidationRecord]:
        sql = "SELECT * FROM browser_validations WHERE run_id = ?"
        params: list[object] = [run_id]
        if candidate_id is not None:
            sql += " AND candidate_id = ?"
            params.append(candidate_id)
        sql += " ORDER BY created_at ASC, validation_id ASC"
        with self.state._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [_record(row) for row in rows]

    def _candidate_workspace(
        self,
        candidate_id: str | None,
        *,
        run_id: str,
        task_id: str,
    ) -> Path | None:
        if candidate_id is None:
            return None
        with self.state._connect() as conn:
            row = conn.execute(
                """
                SELECT workspace FROM candidate_attempts
                WHERE candidate_id = ? AND run_id = ? AND task_id = ?
                """,
                (candidate_id, run_id, task_id),
            ).fetchone()
        if row is None:
            raise ValueError("browser validation candidate binding is invalid")
        return Path(str(row["workspace"])).resolve(strict=True)


def _network_policy(
    allowed_domains: tuple[str, ...],
    fixtures: dict[str, dict[str, Any]],
) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    domains = tuple(sorted({_domain(item) for item in allowed_domains}))
    normalized_fixtures: dict[str, dict[str, Any]] = {}
    total_body_bytes = 0
    for raw_url, raw in fixtures.items():
        if not isinstance(raw, dict):
            raise ValueError("browser network fixture must be an object")
        fixture_url = str(raw_url)
        if redact_text(fixture_url) != fixture_url:
            raise ValueError("browser network fixtures must not contain registered secrets")
        parsed = urlsplit(fixture_url)
        hostname = (parsed.hostname or "").lower()
        if parsed.scheme not in {"http", "https"} or hostname not in domains:
            raise ValueError("browser network fixture URL is not in the domain allowlist")
        status = raw.get("status", 200)
        if isinstance(status, bool) or not isinstance(status, int) or not 100 <= status <= 599:
            raise ValueError("browser network fixture status is invalid")
        content_type = str(raw.get("content_type", "application/json")).strip()
        body = str(raw.get("body", ""))
        if redact_text(content_type) != content_type or redact_text(body) != body:
            raise ValueError("browser network fixtures must not contain registered secrets")
        body_bytes = body.encode("utf-8")
        total_body_bytes += len(body_bytes)
        if (
            len(content_type) > 160
            or any(ord(character) < 32 for character in content_type)
            or len(body_bytes) > 64 * 1024
            or total_body_bytes > 256 * 1024
        ):
            raise ValueError("browser network fixture exceeds its bounded payload")
        normalized_fixtures[fixture_url] = {
            "status": status,
            "content_type": content_type,
            "body": body,
        }
    fixture_domains = {
        (urlsplit(url).hostname or "").lower() for url in normalized_fixtures
    }
    if domains and set(domains) != fixture_domains:
        raise ValueError(
            "every allowed domain requires at least one deterministic network fixture"
        )
    policy = {
        "mode": "fixture_allowlist" if domains else "none",
        "allowed_domains": list(domains),
        "live_egress": False,
    }
    return policy, dict(sorted(normalized_fixtures.items()))


def _report(
    raw: str,
    *,
    expected_target: str,
    expected_assertions: list[dict[str, Any]],
    expected_interactions: list[dict[str, Any]],
) -> dict[str, Any]:
    encoded = raw.encode("utf-8", errors="strict")
    if len(encoded) > _REPORT_LIMIT:
        raise ValueError("browser validation report exceeds 512 KiB")
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError("browser validation did not return JSON evidence") from exc
    if not isinstance(value, dict):
        raise ValueError("browser validation report must be an object")
    if value.get("schema") != "kestrel.browser_validation.v1":
        raise ValueError("browser validation report schema is unsupported")
    if value.get("target_url") != expected_target:
        raise ValueError("browser validation report target changed")
    if not isinstance(value.get("rendered"), bool):
        raise ValueError("browser validation rendered status is invalid")
    for name in ("console_errors", "network_errors"):
        items = value.get(name)
        if (
            not isinstance(items, list)
            or len(items) > 128
            or any(not isinstance(item, dict) for item in items)
        ):
            raise ValueError(f"browser validation report {name} is invalid")
    _result_items(
        value.get("assertions"),
        expected=expected_assertions,
        discriminator="expectation",
        field="assertions",
    )
    _result_items(
        value.get("interactions"),
        expected=expected_interactions,
        discriminator="action",
        field="interactions",
    )
    accessibility = value.get("accessibility")
    if not isinstance(accessibility, dict) or not isinstance(
        accessibility.get("violations"), list
    ):
        raise ValueError("browser validation accessibility evidence is invalid")
    if len(accessibility["violations"]) > 128:
        raise ValueError("browser validation accessibility evidence is too large")
    if any(
        not isinstance(item, dict)
        or not isinstance(item.get("impact"), str)
        or isinstance(item.get("nodes"), bool)
        or not isinstance(item.get("nodes"), int)
        or not 0 <= item["nodes"] <= 1_000_000
        for item in accessibility["violations"]
    ):
        raise ValueError("browser validation accessibility evidence is invalid")
    dom = value.get("dom_summary")
    if (
        not isinstance(dom, dict)
        or not isinstance(dom.get("title"), str)
        or not isinstance(dom.get("url"), str)
        or not isinstance(dom.get("text_excerpt"), str)
        or not _bounded_string_list(dom.get("landmarks"), 32)
        or not _bounded_string_list(dom.get("headings"), 32)
    ):
        raise ValueError("browser validation DOM summary is missing")
    return value


def _result_items(
    value: Any,
    *,
    expected: list[dict[str, Any]],
    discriminator: str,
    field: str,
) -> None:
    if not isinstance(value, list) or len(value) != len(expected):
        raise ValueError(f"browser validation report {field} does not match request")
    for actual, requested in zip(value, expected, strict=True):
        if (
            not isinstance(actual, dict)
            or actual.get("selector") != requested["selector"]
            or actual.get(discriminator) != requested[discriminator]
            or not isinstance(actual.get("passed"), bool)
            or not isinstance(actual.get("detail", ""), str)
        ):
            raise ValueError(
                f"browser validation report {field} does not match request"
            )


def _bounded_string_list(value: Any, maximum: int) -> bool:
    return (
        isinstance(value, list)
        and len(value) <= maximum
        and all(isinstance(item, str) for item in value)
    )


def _normalize_screenshot(report: dict[str, Any]) -> str | None:
    screenshot = report.get("screenshot")
    if screenshot is None:
        return None
    if not isinstance(screenshot, dict):
        raise ValueError("browser validation screenshot is invalid")
    media_type = str(screenshot.get("media_type") or "")
    if media_type not in {"image/png", "image/webp"}:
        raise ValueError("browser validation screenshot media type is unsupported")
    encoded = str(screenshot.get("data_base64") or "")
    try:
        binary = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("browser validation screenshot is not valid base64") from exc
    if not binary or len(binary) > _SCREENSHOT_LIMIT:
        raise ValueError("browser validation screenshot exceeds its bounded size")
    if media_type == "image/png" and not binary.startswith(b"\x89PNG\r\n\x1a\n"):
        raise ValueError("browser validation PNG signature is invalid")
    if media_type == "image/webp" and not (
        binary.startswith(b"RIFF") and binary[8:12] == b"WEBP"
    ):
        raise ValueError("browser validation WebP signature is invalid")
    digest = hashlib.sha256(binary).hexdigest()
    if screenshot.get("sha256") != digest:
        raise ValueError("browser validation screenshot digest is invalid")
    width = screenshot.get("width")
    height = screenshot.get("height")
    if (
        isinstance(width, bool)
        or not isinstance(width, int)
        or isinstance(height, bool)
        or not isinstance(height, int)
        or not 1 <= width <= 16_384
        or not 1 <= height <= 16_384
    ):
        raise ValueError("browser validation screenshot dimensions are invalid")
    report["screenshot"] = {
        "media_type": media_type,
        "data_url": f"data:{media_type};base64,{encoded}",
        "sha256": digest,
        "width": width,
        "height": height,
        "bytes": len(binary),
    }
    return digest


def _failure_codes(report: dict[str, Any]) -> tuple[str, ...]:
    failures: set[str] = set()
    if report.get("rendered") is not True:
        failures.add("route_did_not_render")
    if any(item.get("passed") is not True for item in report["assertions"] if isinstance(item, dict)):
        failures.add("dom_assertion_failed")
    if any(
        item.get("passed") is not True
        for item in report["interactions"]
        if isinstance(item, dict)
    ):
        failures.add("interaction_failed")
    if report["console_errors"]:
        failures.add("console_error")
    if report["network_errors"]:
        failures.add("network_error")
    for violation in report["accessibility"]["violations"]:
        if (
            isinstance(violation, dict)
            and str(violation.get("impact") or "").lower() in _SERIOUS_IMPACTS
        ):
            failures.add("serious_accessibility_violation")
    return tuple(sorted(failures))


def _assertions(values: tuple[BrowserAssertion, ...]) -> list[dict[str, Any]]:
    if len(values) > 32:
        raise ValueError("browser validation has too many DOM assertions")
    allowed = {"visible", "hidden", "text", "count", "attribute"}
    result = []
    for item in values:
        expectation = str(item.expectation).strip().lower()
        if expectation not in allowed:
            raise ValueError("browser assertion expectation is unsupported")
        result.append(
            {
                "selector": _selector(item.selector),
                "expectation": expectation,
                "value": _optional_text(item.value, 2000),
            }
        )
    return result


def _interactions(values: tuple[BrowserInteraction, ...]) -> list[dict[str, Any]]:
    if len(values) > 32:
        raise ValueError("browser validation has too many interactions")
    allowed = {"click", "fill", "check", "select", "press"}
    result = []
    for item in values:
        action = str(item.action).strip().lower()
        if action not in allowed:
            raise ValueError("browser interaction action is unsupported")
        result.append(
            {
                "action": action,
                "selector": _selector(item.selector),
                "value": _optional_text(item.value, 2000),
            }
        )
    return result


def _selector(value: str) -> str:
    text = str(value or "").strip()
    if (
        not text
        or len(text) > _SELECTOR_LIMIT
        or any(ord(character) < 32 for character in text)
        or redact_text(text) != text
    ):
        raise ValueError("browser selector is invalid")
    return text


def _optional_text(value: str | None, limit: int) -> str | None:
    if value is None:
        return None
    text = str(value)
    if (
        len(text) > limit
        or any(
            ord(character) < 32 and character not in "\n\r\t"
            for character in text
        )
        or redact_text(text) != text
    ):
        raise ValueError("browser validation value exceeds its bounded text contract")
    return text


def _command(values: tuple[str, ...]) -> tuple[str, ...]:
    if not values or len(values) > 64:
        raise ValueError("browser validation start command is required and bounded")
    normalized = tuple(str(item) for item in values)
    if any(
        not item
        or len(item) > 4000
        or any(ord(character) < 32 for character in item)
        or redact_text(item) != item
        for item in normalized
    ):
        raise ValueError("browser validation start command contains an invalid token")
    return normalized


def _container_local_url(value: str) -> str:
    text = str(value or "").strip()
    parsed = urlsplit(text)
    if (
        redact_text(text) != text
        or parsed.scheme != "http"
        or (parsed.hostname or "").lower() not in {"127.0.0.1", "localhost"}
        or parsed.username is not None
        or parsed.password is not None
        or parsed.port is None
        or not 1 <= parsed.port <= 65535
        or parsed.fragment
    ):
        raise ValueError(
            "browser target URL must be an explicit container-local HTTP port"
        )
    return text


def _domain(value: str) -> str:
    text = str(value or "").strip().lower().rstrip(".")
    if (
        not text
        or len(text) > 253
        or "*" in text
        or "/" in text
        or ":" in text
        or re.fullmatch(
            r"(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+"
            r"[a-z](?:[a-z0-9-]{0,61}[a-z0-9])?",
            text,
        )
        is None
    ):
        raise ValueError("browser allowed domain is invalid")
    return text


def _timeout(value: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("browser validation timeout must be a number")
    timeout = float(value)
    if not 5 <= timeout <= 600:
        raise ValueError("browser validation timeout must be between 5 and 600 seconds")
    return timeout


def _digest(value: Any, field: str) -> str:
    text = str(value or "").strip()
    if _SHA256.fullmatch(text) is None:
        raise ValueError(f"{field} must be a SHA-256 digest")
    return text


def _record(row: Any) -> BrowserValidationRecord:
    return BrowserValidationRecord(
        validation_id=str(row["validation_id"]),
        run_id=str(row["run_id"]),
        task_id=str(row["task_id"]),
        candidate_id=None if row["candidate_id"] is None else str(row["candidate_id"]),
        candidate_digest=str(row["candidate_digest"]),
        image=str(row["image"]),
        target_url=str(row["target_url"]),
        status=str(row["status"]),
        failure_codes=tuple(json.loads(str(row["failure_codes_json"]))),
        network_policy=dict(json.loads(str(row["network_policy_json"]))),
        report=dict(json.loads(str(row["report_json"]))),
        screenshot_sha256=(
            None
            if row["screenshot_sha256"] is None
            else str(row["screenshot_sha256"])
        ),
        evidence_refs=tuple(json.loads(str(row["evidence_refs_json"]))),
        created_at=str(row["created_at"]),
    )


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
