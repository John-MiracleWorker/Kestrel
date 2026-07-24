#!/usr/bin/env python3
"""Collect, build, and enforce Kestrel provider-certification evidence.

``collect`` accepts either ``kestrel.live_learning_eval.v1`` output or an
explicit case-result document. The latter has this exact shape::

    {
      "schema": "kestrel.provider_certification_cases.v1",
      "provider": "openai",
      "model": "example-model",
      "cases": {
        "generate": "pass",
        "stream": "pass",
        "native_tools": "pass",
        "tool_normalization": "pass",
        "learning_memory": "not_run",
        "learning_memvid": "not_run",
        "policy_unchanged": "not_run"
      }
    }

The collector also accepts pytest JUnit XML, but recognizes only the three
parameterized live-provider tests for the selected provider. A skip never
becomes a pass: it is ``not_supported`` only when the implementation registry
declares that capability unsupported, and ``not_run`` otherwise. Plain pytest
console output is never evidence.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
import xml.etree.ElementTree as ET
from collections.abc import Mapping, Sequence
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from nested_memvid_agent.config import AgentConfig
from nested_memvid_agent.llm.model_catalog import PROVIDER_OPTIONS
from nested_memvid_agent.provider_certification import (
    EVIDENCE_CASES,
    EVIDENCE_RUNNER_KINDS,
    PROVIDER_CERTIFICATION_POLICY_VERSION,
    PROVIDER_IMPLEMENTATION_REGISTRY,
    ProviderCertificationState,
    ProviderCertificationSubject,
    ProviderEvidenceLevel,
    ProviderEvidenceStatus,
    build_provider_certification_report,
    canonical_provider_evidence_id,
    parse_provider_certification_evidence,
    provider_config_digest,
)
from nested_memvid_agent.security_boundary import redact_secrets

EVIDENCE_SCHEMA = "kestrel.provider_certification_evidence.v1"
CASE_SOURCE_SCHEMA = "kestrel.provider_certification_cases.v1"
LIVE_LEARNING_SCHEMA = "kestrel.live_learning_eval.v1"
REPORT_SCHEMA = "kestrel.provider_certification.v2"
RUNNER_RESULT_SCHEMA = "kestrel.provider_certification_runner.v1"

CASE_NAMES = EVIDENCE_CASES
CASE_STATUSES = frozenset(status.value for status in ProviderEvidenceStatus)
EVIDENCE_LEVELS = tuple(level.value for level in ProviderEvidenceLevel)
RUNNER_KINDS = EVIDENCE_RUNNER_KINDS
CERTIFICATION_STATES = tuple(state.value for state in ProviderCertificationState)
STATE_RANK = {
    "implemented": 0,
    "mock_tested": 1,
    "credential_free_integration_tested": 2,
    "locally_live_tested": 3,
    "release_certified": 4,
}

_LIVE_CASE_NAMES = frozenset(
    {
        "provider_handshake",
        "durable_memory_reopen",
        "correction_frame",
        "procedural_promotion_gate",
        "task_capsule_learning_signal",
        "approval_gate_blocks_unapproved_high_risk_tool",
        "behavior_delta_activation_log",
        "postflight_memory_integrity",
    }
)
_JUNIT_CLASSNAME = "tests.integration.test_provider_live_integration"
_JUNIT_CASE_DIMENSIONS: dict[str, tuple[str, ...]] = {
    "test_live_provider_generate_smoke": ("generate",),
    "test_live_provider_stream_smoke": ("stream",),
    "test_live_provider_native_tool_call_certification": (
        "native_tools",
        "tool_normalization",
    ),
}
_MAX_SOURCE_BYTES = 10 * 1024 * 1024


class RunnerInputError(ValueError):
    """Raised when evidence cannot be safely or deterministically interpreted."""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    collect = commands.add_parser(
        "collect", help="Normalize executed case results into one sanitized evidence receipt."
    )
    collect.add_argument("--provider", required=True, choices=PROVIDER_OPTIONS)
    collect.add_argument("--model", required=True)
    collect.add_argument("--profile", required=True)
    collect.add_argument("--level", required=True, choices=EVIDENCE_LEVELS)
    collect.add_argument("--source", type=Path, action="append", required=True)
    collect.add_argument("--commit", required=True)
    collect.add_argument("--tree-digest", required=True)
    collect.add_argument("--runner-kind", required=True, choices=RUNNER_KINDS)
    collect.add_argument("--started-at", required=True)
    collect.add_argument("--completed-at", required=True)
    collect.add_argument(
        "--trusted-runner",
        action="store_true",
        help="Record a runner trust claim; this does not authenticate the receipt.",
    )
    collect.add_argument("--config", type=Path)
    collect.add_argument("--base-url")
    collect.add_argument("--api-key-env")
    collect.add_argument("--output", type=Path)

    build = commands.add_parser(
        "build", help="Build a provider matrix from zero or more evidence receipts."
    )
    build.add_argument("--commit", required=True)
    build.add_argument("--tree-digest", required=True)
    build.add_argument("--evidence", type=Path, action="append", default=[])
    build.add_argument(
        "--authenticated-evidence-id",
        action="append",
        default=[],
        help="Exact receipt ID authenticated by the caller-side evidence channel.",
    )
    build.add_argument("--release-target", choices=PROVIDER_OPTIONS, action="append")
    build.add_argument("--config", type=Path)
    build.add_argument("--now")
    build.add_argument("--max-evidence-age-hours", type=float)
    build.add_argument("--output", type=Path)

    check = commands.add_parser(
        "check", help="Fail closed unless a selected provider meets the requested state."
    )
    check.add_argument("--report", type=Path, required=True)
    check.add_argument("--provider", required=True, choices=PROVIDER_OPTIONS)
    check.add_argument(
        "--require-state",
        choices=CERTIFICATION_STATES,
        default="release_certified",
    )
    check.add_argument("--output", type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "collect":
            payload, exit_code = _run_collect(args)
        elif args.command == "build":
            payload, exit_code = _run_build(args)
        else:
            payload, exit_code = _run_check(args)
    except (RunnerInputError, ValueError, TypeError) as exc:
        payload = {
            "schema": RUNNER_RESULT_SCHEMA,
            "command": args.command,
            "ok": False,
            "error": str(redact_secrets(str(exc))),
        }
        exit_code = 2
    _emit_json(payload, getattr(args, "output", None))
    return exit_code


def _run_collect(args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    config = _provider_config(
        _load_config(args.config),
        provider=args.provider,
        model=args.model,
        base_url=args.base_url,
        api_key_env=args.api_key_env,
    )
    missing = provider_prerequisites(config, provider=args.provider, model=args.model)
    if missing:
        environment_names = [item for item in missing if _looks_like_environment_name(item)]
        return (
            {
                "schema": RUNNER_RESULT_SCHEMA,
                "command": "collect",
                "ok": False,
                "provider": args.provider,
                "missing_requirements": missing,
                "required_environment": [
                    {"name": name, "present": False} for name in environment_names
                ],
            },
            2,
        )

    cases = {name: "not_run" for name in CASE_NAMES}
    source_digests: list[str] = []
    for source_path in args.source:
        source_bytes = _read_bounded_source(source_path)
        source_digests.append(hashlib.sha256(source_bytes).hexdigest())
        source_cases = _cases_from_source_bytes(
            source_bytes,
            path=source_path,
            provider=args.provider,
            model=args.model,
        )
        cases = merge_case_results(cases, source_cases)

    receipt_without_id: dict[str, Any] = {
        "provider": args.provider,
        "model": args.model,
        "profile": args.profile,
        "level": args.level,
        "config_digest": provider_config_digest(config, args.provider, args.model),
        "subject": {"commit": args.commit, "tree_digest": args.tree_digest},
        "runner": {"trusted": args.trusted_runner, "kind": args.runner_kind},
        "started_at": args.started_at,
        "completed_at": args.completed_at,
        "cases": cases,
        "source_digests": sorted(set(source_digests)),
    }
    evidence_id = canonical_provider_evidence_id(receipt_without_id)
    receipt = {
        "schema": EVIDENCE_SCHEMA,
        "evidence_id": evidence_id,
        **receipt_without_id,
    }
    parsed = parse_provider_certification_evidence(receipt)
    return parsed.to_dict(), 0


def _run_build(args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    evidence = []
    seen_ids: set[str] = set()
    for path in args.evidence:
        payload = _load_json_object(path, label="provider certification evidence")
        parsed = parse_provider_certification_evidence(payload)
        if parsed.evidence_id in seen_ids:
            raise RunnerInputError(f"duplicate evidence_id: {parsed.evidence_id}")
        seen_ids.add(parsed.evidence_id)
        evidence.append(parsed)

    authenticated_ids = tuple(args.authenticated_evidence_id)
    unknown_authenticated = sorted(set(authenticated_ids) - seen_ids)
    if unknown_authenticated:
        raise RunnerInputError(
            "authenticated evidence IDs were not supplied as receipts: "
            + ", ".join(unknown_authenticated)
        )
    if len(authenticated_ids) != len(set(authenticated_ids)):
        raise RunnerInputError("authenticated evidence IDs must be unique")

    subject = ProviderCertificationSubject(commit=args.commit, tree_digest=args.tree_digest)
    kwargs: dict[str, Any] = {
        "evidence": tuple(evidence),
        "subject": subject,
        "authenticated_evidence_ids": frozenset(authenticated_ids),
    }
    if args.now:
        kwargs["now"] = _parse_timestamp(args.now, field_name="now")
    if args.max_evidence_age_hours is not None:
        if not math.isfinite(args.max_evidence_age_hours) or args.max_evidence_age_hours <= 0:
            raise RunnerInputError("max evidence age must be a finite value greater than zero")
        kwargs["max_evidence_age"] = timedelta(hours=args.max_evidence_age_hours)
    if args.release_target is not None:
        kwargs["release_targets"] = tuple(dict.fromkeys(args.release_target))
    report = build_provider_certification_report(_load_config(args.config), **kwargs)
    return report.to_dict(), 0


def _run_check(args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    report = _load_json_object(args.report, label="provider certification report")
    if report.get("schema") != REPORT_SCHEMA:
        raise RunnerInputError(f"provider certification report schema must be {REPORT_SCHEMA}")
    providers = report.get("providers")
    if not isinstance(providers, list):
        raise RunnerInputError("provider certification report providers must be a list")
    matches = [
        item
        for item in providers
        if isinstance(item, dict) and item.get("provider") == args.provider
    ]
    if len(matches) != 1:
        raise RunnerInputError(
            f"provider certification report must contain exactly one {args.provider} row"
        )
    row = cast(dict[str, Any], matches[0])
    actual_state = row.get("certification_state")
    if actual_state not in CERTIFICATION_STATES:
        raise RunnerInputError("provider certification state is missing or unsupported")
    raw_missing = row.get("missing_requirements", [])
    if not isinstance(raw_missing, list) or not all(
        isinstance(item, str) for item in raw_missing
    ):
        raise RunnerInputError("provider missing_requirements must be a list of names")
    missing = [
        item
        for item in raw_missing
        if not (args.require_state == "experimental" and item == "provider_is_experimental")
    ]
    state_passed = _state_satisfies(str(actual_state), args.require_state)
    structural_failures = (
        _release_report_failures(report, row, provider=args.provider)
        if args.require_state == "release_certified"
        else []
    )
    requirements_passed = not missing if args.require_state == "release_certified" else True
    passed = state_passed and requirements_passed and not structural_failures
    payload = {
        "schema": RUNNER_RESULT_SCHEMA,
        "command": "check",
        "ok": passed,
        "provider": args.provider,
        "required_state": args.require_state,
        "actual_state": actual_state,
        "missing_requirements": missing,
        "failed_checks": structural_failures,
    }
    return payload, 0 if passed else 1


def provider_prerequisites(
    config: AgentConfig,
    *,
    provider: str,
    model: str,
) -> list[str]:
    """Return prerequisite names only, never their values."""

    missing: list[str] = []
    if not model.strip():
        missing.append("model")
    if provider == "openai-compatible" and not config.base_url:
        missing.append("base_url")
    return missing


def cases_from_source(
    source: Mapping[str, Any],
    *,
    provider: str,
    model: str,
) -> dict[str, str]:
    schema = source.get("schema")
    if schema == CASE_SOURCE_SCHEMA:
        return _cases_from_explicit_source(source, provider=provider, model=model)
    if schema == LIVE_LEARNING_SCHEMA:
        return _cases_from_live_learning(source, provider=provider, model=model)
    raise RunnerInputError(
        "unsupported certification source schema; expected "
        f"{CASE_SOURCE_SCHEMA} or {LIVE_LEARNING_SCHEMA}"
    )


def cases_from_source_path(
    path: Path,
    *,
    provider: str,
    model: str,
) -> dict[str, str]:
    """Load an exact JSON result contract or conservative pytest JUnit XML."""

    raw = _read_bounded_source(path)
    return _cases_from_source_bytes(raw, path=path, provider=provider, model=model)


def _cases_from_source_bytes(
    raw: bytes,
    *,
    path: Path,
    provider: str,
    model: str,
) -> dict[str, str]:
    if path.suffix.casefold() == ".xml" or raw.lstrip().startswith(b"<"):
        return cases_from_junit_xml(raw, provider=provider)
    try:
        source = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RunnerInputError(f"certification source is not valid JSON or JUnit XML: {path}") from exc
    if not isinstance(source, dict):
        raise RunnerInputError("certification source must contain a JSON object")
    return cases_from_source(source, provider=provider, model=model)


def cases_from_junit_xml(raw: bytes, *, provider: str) -> dict[str, str]:
    """Map only the selected provider's exact live-integration tests."""

    try:
        root = ET.fromstring(raw)
    except ET.ParseError as exc:
        raise RunnerInputError("certification source is not valid JUnit XML") from exc
    if root.tag not in {"testsuite", "testsuites"}:
        raise RunnerInputError("JUnit certification source must use testsuite or testsuites root")

    selected: dict[str, ET.Element] = {}
    expected_suffix = f"[{provider}]"
    for testcase in root.iter("testcase"):
        classname = testcase.attrib.get("classname", "")
        file_name = testcase.attrib.get("file", "").replace("\\", "/")
        if classname != _JUNIT_CLASSNAME and not file_name.endswith(
            "tests/integration/test_provider_live_integration.py"
        ):
            continue
        parameterized_name = testcase.attrib.get("name", "")
        if not parameterized_name.endswith(expected_suffix):
            continue
        base_name = parameterized_name[: -len(expected_suffix)]
        if base_name not in _JUNIT_CASE_DIMENSIONS:
            continue
        if base_name in selected:
            raise RunnerInputError(f"JUnit source repeats selected provider case: {base_name}")
        selected[base_name] = testcase

    cases = {name: "not_run" for name in CASE_NAMES}
    spec = PROVIDER_IMPLEMENTATION_REGISTRY[provider]
    supported = {
        "generate": spec.generate,
        "stream": spec.stream,
        "native_tools": spec.native_tools,
        "tool_normalization": spec.tool_normalization,
    }
    for test_name, dimensions in _JUNIT_CASE_DIMENSIONS.items():
        selected_case = selected.get(test_name)
        if selected_case is None:
            continue
        has_failure = (
            selected_case.find("failure") is not None
            or selected_case.find("error") is not None
        )
        skipped = selected_case.find("skipped") is not None
        for dimension in dimensions:
            if has_failure:
                cases[dimension] = "fail"
            elif skipped:
                cases[dimension] = "not_supported" if not supported[dimension] else "not_run"
            else:
                cases[dimension] = "pass"
    return cases


def _cases_from_explicit_source(
    source: Mapping[str, Any],
    *,
    provider: str,
    model: str,
) -> dict[str, str]:
    if set(source) != {"schema", "provider", "model", "cases"}:
        raise RunnerInputError(
            "certification case source must contain exactly schema, provider, model, and cases"
        )
    _require_source_identity(source, provider=provider, model=model)
    raw_cases = source.get("cases")
    if not isinstance(raw_cases, dict):
        raise RunnerInputError("certification case source cases must be an object")
    if set(raw_cases) != set(CASE_NAMES):
        raise RunnerInputError("certification case source must contain exactly the seven cases")
    cases: dict[str, str] = {}
    for name in CASE_NAMES:
        status = raw_cases.get(name)
        if status not in CASE_STATUSES:
            raise RunnerInputError(
                f"case {name} must use a canonical status; skipped output is not evidence"
            )
        cases[name] = cast(str, status)
    return cases


def _cases_from_live_learning(
    source: Mapping[str, Any],
    *,
    provider: str,
    model: str,
) -> dict[str, str]:
    raw_provider = source.get("provider")
    if not isinstance(raw_provider, dict):
        raise RunnerInputError("live learning source provider must be an object")
    _require_source_identity(raw_provider, provider=provider, model=model)
    backend = raw_provider.get("backend")
    if backend not in {"memory", "memvid"}:
        raise RunnerInputError("live learning source backend must be memory or memvid")

    raw_results = source.get("results")
    if not isinstance(raw_results, list):
        raise RunnerInputError("live learning source results must be a list")
    results: dict[str, Mapping[str, Any]] = {}
    for raw in raw_results:
        if not isinstance(raw, dict) or not isinstance(raw.get("name"), str):
            raise RunnerInputError("live learning source contains an invalid result")
        name = cast(str, raw["name"])
        if name in results:
            raise RunnerInputError(f"live learning source repeats result: {name}")
        if not isinstance(raw.get("passed"), bool):
            raise RunnerInputError(f"live learning result {name} must contain boolean passed")
        results[name] = raw
    if set(results) != set(_LIVE_CASE_NAMES):
        raise RunnerInputError("live learning source must contain the complete v1 case set")

    summary = source.get("summary")
    if not isinstance(summary, dict) or not isinstance(summary.get("passed"), bool):
        raise RunnerInputError("live learning source summary must contain boolean passed")
    summary_passed = summary["passed"] is True
    all_results_passed = all(result["passed"] is True for result in results.values())
    learning_status = "pass" if summary_passed and all_results_passed else "fail"
    handshake_status = "pass" if results["provider_handshake"]["passed"] is True else "fail"

    postflight = results["postflight_memory_integrity"]
    metrics = postflight.get("metrics")
    policy_write_count = metrics.get("policy_write_count") if isinstance(metrics, dict) else None
    policy_unchanged = (
        postflight["passed"] is True
        and type(policy_write_count) is int
        and policy_write_count == 0
    )
    cases = {name: "not_run" for name in CASE_NAMES}
    cases["generate"] = handshake_status
    cases[f"learning_{backend}"] = learning_status
    cases["policy_unchanged"] = "pass" if policy_unchanged else "fail"
    return cases


def merge_case_results(
    current: Mapping[str, str],
    incoming: Mapping[str, str],
) -> dict[str, str]:
    merged = dict(current)
    for name in CASE_NAMES:
        old = merged[name]
        new = incoming.get(name, "not_run")
        if new == "not_run" or new == old:
            continue
        if old == "not_run":
            merged[name] = new
            continue
        if "fail" in {old, new}:
            merged[name] = "fail"
            continue
        raise RunnerInputError(f"conflicting non-failure evidence for case {name}")
    return merged


def _require_source_identity(
    source: Mapping[str, Any],
    *,
    provider: str,
    model: str,
) -> None:
    if source.get("provider") != provider:
        raise RunnerInputError("certification source provider does not match --provider")
    if source.get("model") != model:
        raise RunnerInputError("certification source model does not match --model")


def _provider_config(
    config: AgentConfig,
    *,
    provider: str,
    model: str,
    base_url: str | None,
    api_key_env: str | None,
) -> AgentConfig:
    existing_provider = config.provider == provider
    return replace(
        config,
        provider=provider,
        model=model,
        base_url=base_url if base_url is not None else config.base_url if existing_provider else None,
        api_key_env=(
            api_key_env
            if api_key_env is not None
            else config.api_key_env
            if existing_provider
            else None
        ),
    )


def _load_config(path: Path | None) -> AgentConfig:
    if path is None:
        return AgentConfig.from_env()
    try:
        return AgentConfig.from_json_file(path)
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        raise RunnerInputError(f"provider configuration could not be loaded: {exc}") from None


def _load_json_object(path: Path, *, label: str) -> dict[str, Any]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise RunnerInputError(f"{label} file could not be read: {path}") from exc
    except json.JSONDecodeError as exc:
        raise RunnerInputError(f"{label} file is not valid JSON: {path}") from exc
    if not isinstance(raw, dict):
        raise RunnerInputError(f"{label} must contain a JSON object")
    return cast(dict[str, Any], raw)


def _read_bounded_source(path: Path) -> bytes:
    try:
        size = path.stat().st_size
        if size > _MAX_SOURCE_BYTES:
            raise RunnerInputError("certification source exceeds the 10 MiB size limit")
        raw = path.read_bytes()
        if len(raw) > _MAX_SOURCE_BYTES:
            raise RunnerInputError("certification source exceeds the 10 MiB size limit")
        return raw
    except RunnerInputError:
        raise
    except OSError as exc:
        raise RunnerInputError(f"certification source file could not be read: {path}") from exc


def _parse_timestamp(value: str, *, field_name: str) -> datetime:
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        raise RunnerInputError(f"{field_name} must be an RFC3339 timestamp") from None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise RunnerInputError(f"{field_name} must include a timezone")
    return parsed.astimezone(UTC)


def _looks_like_environment_name(value: str) -> bool:
    return value.isupper() and all(character.isalnum() or character == "_" for character in value)


def _state_satisfies(actual: str, required: str) -> bool:
    if required == "experimental" or actual == "experimental":
        return actual == required
    return STATE_RANK[actual] >= STATE_RANK[required]


def _release_report_failures(
    report: Mapping[str, Any],
    row: Mapping[str, Any],
    *,
    provider: str,
) -> list[str]:
    """Reject internally inconsistent release claims before treating them as a gate."""

    failures: list[str] = []
    if report.get("policy_version") != PROVIDER_CERTIFICATION_POLICY_VERSION:
        failures.append("policy_version_mismatch")

    subject = report.get("subject")
    if not isinstance(subject, Mapping):
        failures.append("subject_missing")
    else:
        commit = subject.get("commit")
        tree_digest = subject.get("tree_digest")
        if not _is_lower_hex(commit, minimum=7, maximum=64) or commit == "unknown":
            failures.append("subject_commit_invalid")
        if not _is_lower_hex(tree_digest, minimum=64, maximum=64) or tree_digest == "unknown":
            failures.append("subject_tree_digest_invalid")

    headline = report.get("headline")
    if not isinstance(headline, Mapping):
        failures.append("headline_missing")
    else:
        targets = headline.get("release_targets")
        if not isinstance(targets, list) or provider not in targets:
            failures.append("provider_not_release_target")
        if headline.get("release_certified") is not True:
            failures.append("headline_not_release_certified")

    evidence_ids = row.get("evidence_ids")
    if (
        not isinstance(evidence_ids, list)
        or not evidence_ids
        or not all(isinstance(item, str) and item for item in evidence_ids)
    ):
        failures.append("evidence_ids_required")

    spec = PROVIDER_IMPLEMENTATION_REGISTRY[provider]
    expected_support = {
        "generate": spec.generate,
        "stream": spec.stream,
        "native_tools": spec.native_tools,
        "tool_normalization": spec.tool_normalization,
        "learning_e2e": spec.learning_e2e,
    }
    for dimension, supported in expected_support.items():
        raw_dimension = row.get(dimension)
        status = _dimension_status(raw_dimension)
        expected = "pass" if supported else "not_supported"
        if status != expected:
            failures.append(f"{dimension}_{expected}_required")
        if supported and not _dimension_has_evidence(raw_dimension):
            failures.append(f"{dimension}_evidence_required")
    return failures


def _dimension_status(value: Any) -> str | None:
    if not isinstance(value, Mapping):
        return None
    status = value.get("status")
    return status if isinstance(status, str) else None


def _dimension_has_evidence(value: Any) -> bool:
    if not isinstance(value, Mapping):
        return False
    evidence_ids = value.get("evidence_ids")
    return (
        isinstance(evidence_ids, list)
        and bool(evidence_ids)
        and all(isinstance(item, str) and item for item in evidence_ids)
    )


def _is_lower_hex(value: Any, *, minimum: int, maximum: int) -> bool:
    return (
        isinstance(value, str)
        and minimum <= len(value) <= maximum
        and all(character in "0123456789abcdef" for character in value)
    )


def _emit_json(payload: Mapping[str, Any], output: Path | None) -> None:
    safe = redact_secrets(dict(payload))
    rendered = json.dumps(safe, indent=2, sort_keys=True) + "\n"
    if output is None:
        sys.stdout.write(rendered)
        return
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(rendered, encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
