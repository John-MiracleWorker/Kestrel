from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from scripts.run_provider_certification import (
    CASE_NAMES,
    CASE_SOURCE_SCHEMA,
    EVIDENCE_SCHEMA,
    LIVE_LEARNING_SCHEMA,
    REPORT_SCHEMA,
    cases_from_junit_xml,
    cases_from_source,
    main,
)

COMMIT = "a" * 40
TREE_DIGEST = "b" * 64
STARTED_AT = "2026-07-22T01:00:00Z"
COMPLETED_AT = "2026-07-22T01:01:00Z"


def test_collect_emits_deterministic_canonical_receipt(tmp_path: Path) -> None:
    source = tmp_path / "cases.json"
    first = tmp_path / "first.json"
    second = tmp_path / "second.json"
    changed_source = tmp_path / "changed-source.json"
    _write_json(source, _case_source("mock", "mock"))

    args = _collect_args(source, first)
    assert main(args) == 0
    args[-1] = str(second)
    assert main(args) == 0

    first_payload = _read_json(first)
    second_payload = _read_json(second)
    assert first_payload == second_payload
    assert first_payload["schema"] == EVIDENCE_SCHEMA
    assert first_payload["provider"] == "mock"
    assert first_payload["model"] == "mock"
    assert first_payload["profile"] == "default"
    assert first_payload["level"] == "mock"
    assert first_payload["runner"] == {"kind": "mock", "trusted": False}
    assert first_payload["subject"] == {"commit": COMMIT, "tree_digest": TREE_DIGEST}
    assert set(first_payload["cases"]) == set(CASE_NAMES)
    assert first_payload["evidence_id"].startswith("pce_")
    assert len(first_payload["evidence_id"]) == 68
    assert len(first_payload["config_digest"]) == 64
    assert first_payload["source_digests"] == [
        hashlib.sha256(source.read_bytes()).hexdigest()
    ]

    changed_source.write_text(
        json.dumps(_case_source("mock", "mock"), indent=2) + "\n",
        encoding="utf-8",
    )
    changed_output = tmp_path / "changed.json"
    assert main(_collect_args(changed_source, changed_output)) == 0
    assert _read_json(changed_output)["evidence_id"] != first_payload["evidence_id"]


def test_collect_and_check_preserve_assurance_after_ephemeral_credential_is_unset(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    source = tmp_path / "cases.json"
    receipt_path = tmp_path / "receipt.json"
    report_path = tmp_path / "report.json"
    check_path = tmp_path / "check.json"
    config_path = tmp_path / "config.json"
    _write_json(source, _case_source("openai", "gpt-test"))
    _write_json(
        config_path,
        {"provider": "openai", "model": "gpt-test", "api_key_env": "OPENAI_API_KEY"},
    )

    exit_code = main(
        _collect_args(
            source,
            receipt_path,
            provider="openai",
            model="gpt-test",
            level="live",
            runner_kind="local",
        )
    )

    assert exit_code == 0
    assert _read_json(receipt_path)["schema"] == EVIDENCE_SCHEMA
    assert (
        main(
            [
                "build",
                "--commit",
                COMMIT,
                "--tree-digest",
                TREE_DIGEST,
                "--evidence",
                str(receipt_path),
                "--config",
                str(config_path),
                "--now",
                "2026-07-22T01:02:00Z",
                "--output",
                str(report_path),
            ]
        )
        == 0
    )
    report = _read_json(report_path)
    openai = _provider_row(report, "openai")
    assert openai["status"] == "blocked"
    assert openai["api_key_env"] == {"name": "OPENAI_API_KEY", "present": False}
    assert (
        main(
            [
                "check",
                "--report",
                str(report_path),
                "--provider",
                "openai",
                "--require-state",
                "locally_live_tested",
                "--output",
                str(check_path),
            ]
        )
        == 0
    )
    assert _read_json(check_path)["missing_requirements"] == []


def test_collect_fails_closed_when_selected_endpoint_identity_is_missing(
    tmp_path: Path,
) -> None:
    source = tmp_path / "cases.json"
    output = tmp_path / "result.json"
    config = tmp_path / "config.json"
    _write_json(source, _case_source("openai-compatible", "local-model"))
    _write_json(config, {"provider": "mock", "model": "mock"})

    args = _collect_args(
        source,
        output,
        provider="openai-compatible",
        model="local-model",
        level="credential_free",
        runner_kind="local",
    )
    args.extend(["--config", str(config)])
    exit_code = main(args)

    payload = _read_json(output)
    assert exit_code == 2
    assert payload["missing_requirements"] == ["base_url"]
    assert payload["required_environment"] == []


def test_collect_junit_ignores_failure_text_and_never_serializes_credentials(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    credential = "sk-proj-provider-certification-secret-123456"
    failure_secret = "failure-output-secret-987654"
    monkeypatch.setenv("OPENAI_API_KEY", credential)
    source = tmp_path / "provider.xml"
    source.write_text(
        f"""<?xml version="1.0" encoding="utf-8"?>
<testsuites><testsuite name="pytest">
  <testcase classname="tests.integration.test_provider_live_integration"
            name="test_live_provider_generate_smoke[openai]" />
  <testcase classname="tests.integration.test_provider_live_integration"
            name="test_live_provider_stream_smoke[openai]">
    <skipped message="{failure_secret}" />
  </testcase>
  <testcase classname="tests.integration.test_provider_live_integration"
            name="test_live_provider_native_tool_call_certification[openai]">
    <failure message="{failure_secret}">{credential}</failure>
  </testcase>
  <testcase classname="tests.integration.test_provider_live_integration"
            name="test_live_provider_generate_smoke[anthropic]" />
  <testcase classname="tests.other"
            name="test_live_provider_stream_smoke[openai]" />
</testsuite></testsuites>
""",
        encoding="utf-8",
    )
    output = tmp_path / "receipt.json"

    exit_code = main(
        _collect_args(
            source,
            output,
            provider="openai",
            model="gpt-test",
            level="live",
            runner_kind="local",
        )
    )

    rendered = output.read_text(encoding="utf-8")
    receipt = json.loads(rendered)
    assert exit_code == 0
    assert receipt["cases"]["generate"] == "pass"
    assert receipt["cases"]["stream"] == "not_run"
    assert receipt["cases"]["native_tools"] == "fail"
    assert receipt["cases"]["tool_normalization"] == "fail"
    assert credential not in rendered
    assert failure_secret not in rendered


def test_junit_skips_are_not_supported_only_for_unsupported_capabilities() -> None:
    raw = b"""<testsuite>
      <testcase classname="tests.integration.test_provider_live_integration"
                name="test_live_provider_generate_smoke[codex-cli]"><skipped /></testcase>
      <testcase classname="tests.integration.test_provider_live_integration"
                name="test_live_provider_stream_smoke[codex-cli]"><skipped /></testcase>
      <testcase classname="tests.integration.test_provider_live_integration"
                name="test_live_provider_native_tool_call_certification[codex-cli]"><skipped /></testcase>
    </testsuite>"""

    cases = cases_from_junit_xml(raw, provider="codex-cli")

    assert cases["generate"] == "not_run"
    assert cases["stream"] == "not_supported"
    assert cases["native_tools"] == "not_supported"
    assert cases["tool_normalization"] == "not_run"


def test_explicit_case_source_rejects_skipped_status() -> None:
    source = _case_source("mock", "mock")
    source["cases"]["stream"] = "skipped"

    with pytest.raises(ValueError, match="skipped output is not evidence"):
        cases_from_source(source, provider="mock", model="mock")


def test_explicit_case_source_rejects_unknown_top_level_fields() -> None:
    source = _case_source("mock", "mock")
    source["untrusted_claim"] = "ignored"

    with pytest.raises(ValueError, match="must contain exactly"):
        cases_from_source(source, provider="mock", model="mock")


def test_live_learning_source_maps_only_proven_dimensions() -> None:
    results = [
        {
            "name": name,
            "passed": True,
            "latency_ms": 1.0,
            "metrics": {"policy_write_count": 0}
            if name == "postflight_memory_integrity"
            else {},
        }
        for name in (
            "provider_handshake",
            "durable_memory_reopen",
            "correction_frame",
            "procedural_promotion_gate",
            "task_capsule_learning_signal",
            "approval_gate_blocks_unapproved_high_risk_tool",
            "behavior_delta_activation_log",
            "postflight_memory_integrity",
        )
    ]
    source = {
        "schema": LIVE_LEARNING_SCHEMA,
        "provider": {"provider": "mock", "model": "mock", "backend": "memory"},
        "results": results,
        "summary": {"case_count": 8, "pass_count": 8, "fail_count": 0, "passed": True},
    }

    cases = cases_from_source(source, provider="mock", model="mock")

    assert cases == {
        "generate": "pass",
        "stream": "not_run",
        "native_tools": "not_run",
        "tool_normalization": "not_run",
        "learning_memory": "pass",
        "learning_memvid": "not_run",
        "policy_unchanged": "pass",
    }


def test_live_learning_policy_count_requires_an_integer_zero() -> None:
    results = [
        {
            "name": name,
            "passed": True,
            "metrics": {"policy_write_count": False}
            if name == "postflight_memory_integrity"
            else {},
        }
        for name in (
            "provider_handshake",
            "durable_memory_reopen",
            "correction_frame",
            "procedural_promotion_gate",
            "task_capsule_learning_signal",
            "approval_gate_blocks_unapproved_high_risk_tool",
            "behavior_delta_activation_log",
            "postflight_memory_integrity",
        )
    ]
    source = {
        "schema": LIVE_LEARNING_SCHEMA,
        "provider": {"provider": "mock", "model": "mock", "backend": "memory"},
        "results": results,
        "summary": {"passed": True},
    }

    cases = cases_from_source(source, provider="mock", model="mock")

    assert cases["policy_unchanged"] == "fail"


def test_build_does_not_treat_trusted_claim_as_authentication(
    tmp_path: Path,
) -> None:
    source = tmp_path / "cases.json"
    receipt_path = tmp_path / "receipt.json"
    config_path = tmp_path / "config.json"
    unauthenticated_report = tmp_path / "unauthenticated.json"
    authenticated_report = tmp_path / "authenticated.json"
    check_result = tmp_path / "check.json"
    _write_json(source, _case_source("mock", "mock"))
    _write_json(config_path, {"provider": "mock", "model": "mock"})

    collect_args = _collect_args(
        source,
        receipt_path,
        level="release",
        profile="release",
        runner_kind="release_ci",
        trusted=True,
    )
    collect_args.extend(["--config", str(config_path)])
    assert main(collect_args) == 0
    evidence_id = _read_json(receipt_path)["evidence_id"]

    build_args = _build_args(receipt_path, config_path, unauthenticated_report)
    assert main(build_args) == 0
    unauthenticated = _provider_row(_read_json(unauthenticated_report), "mock")
    assert unauthenticated["certification_state"] == "implemented"
    assert "authenticated_evidence_required" in unauthenticated["missing_requirements"]
    assert (
        main(
            [
                "check",
                "--report",
                str(unauthenticated_report),
                "--provider",
                "mock",
                "--output",
                str(check_result),
            ]
        )
        == 1
    )

    authenticated_args = _build_args(receipt_path, config_path, authenticated_report)
    authenticated_args.extend(["--authenticated-evidence-id", evidence_id])
    assert main(authenticated_args) == 0
    authenticated = _provider_row(_read_json(authenticated_report), "mock")
    assert authenticated["certification_state"] == "release_certified"
    assert authenticated["missing_requirements"] == []
    assert (
        main(
            [
                "check",
                "--report",
                str(authenticated_report),
                "--provider",
                "mock",
                "--output",
                str(check_result),
            ]
        )
        == 0
    )


def test_build_rejects_authentication_for_receipt_not_supplied(tmp_path: Path) -> None:
    config_path = tmp_path / "config.json"
    output = tmp_path / "report.json"
    _write_json(config_path, {"provider": "mock", "model": "mock"})

    exit_code = main(
        [
            "build",
            "--commit",
            COMMIT,
            "--tree-digest",
            TREE_DIGEST,
            "--config",
            str(config_path),
            "--authenticated-evidence-id",
            "pce_missing",
            "--output",
            str(output),
        ]
    )

    assert exit_code == 2
    assert "not supplied as receipts" in _read_json(output)["error"]


def test_check_fails_closed_when_report_has_missing_requirements(tmp_path: Path) -> None:
    report = tmp_path / "report.json"
    output = tmp_path / "check.json"
    _write_json(
        report,
        {
            "schema": REPORT_SCHEMA,
            "providers": [
                {
                    "provider": "openai",
                    "certification_state": "release_certified",
                    "missing_requirements": ["OPENAI_API_KEY"],
                }
            ],
        },
    )

    exit_code = main(
        [
            "check",
            "--report",
            str(report),
            "--provider",
            "openai",
            "--output",
            str(output),
        ]
    )

    payload = _read_json(output)
    assert exit_code == 1
    assert payload["ok"] is False
    assert payload["missing_requirements"] == ["OPENAI_API_KEY"]
    assert "policy_version_mismatch" in payload["failed_checks"]
    assert "headline_missing" in payload["failed_checks"]
    assert "evidence_ids_required" in payload["failed_checks"]
    assert "generate_pass_required" in payload["failed_checks"]


def test_check_can_explicitly_accept_experimental_state(tmp_path: Path) -> None:
    report = tmp_path / "report.json"
    output = tmp_path / "check.json"
    _write_json(
        report,
        {
            "schema": REPORT_SCHEMA,
            "providers": [
                {
                    "provider": "codex-cli",
                    "certification_state": "experimental",
                    "missing_requirements": ["provider_is_experimental"],
                }
            ],
        },
    )

    exit_code = main(
        [
            "check",
            "--report",
            str(report),
            "--provider",
            "codex-cli",
            "--require-state",
            "experimental",
            "--output",
            str(output),
        ]
    )

    assert exit_code == 0
    assert _read_json(output)["ok"] is True


def test_check_lower_assurance_ignores_only_higher_level_requirements(tmp_path: Path) -> None:
    report = tmp_path / "report.json"
    output = tmp_path / "check.json"
    _write_json(
        report,
        {
            "schema": REPORT_SCHEMA,
            "providers": [
                {
                    "provider": "openai",
                    "certification_state": "locally_live_tested",
                    "missing_requirements": ["authenticated_evidence_required"],
                }
            ],
        },
    )

    exit_code = main(
        [
            "check",
            "--report",
            str(report),
            "--provider",
            "openai",
            "--require-state",
            "locally_live_tested",
            "--output",
            str(output),
        ]
    )

    payload = _read_json(output)
    assert exit_code == 0
    assert payload["ok"] is True
    assert payload["missing_requirements"] == ["authenticated_evidence_required"]


def _case_source(provider: str, model: str) -> dict[str, object]:
    return {
        "schema": CASE_SOURCE_SCHEMA,
        "provider": provider,
        "model": model,
        "cases": {name: "pass" for name in CASE_NAMES},
    }


def _collect_args(
    source: Path,
    output: Path,
    *,
    provider: str = "mock",
    model: str = "mock",
    profile: str = "default",
    level: str = "mock",
    runner_kind: str = "mock",
    trusted: bool = False,
) -> list[str]:
    args = [
        "collect",
        "--provider",
        provider,
        "--model",
        model,
        "--profile",
        profile,
        "--level",
        level,
        "--source",
        str(source),
        "--commit",
        COMMIT,
        "--tree-digest",
        TREE_DIGEST,
        "--runner-kind",
        runner_kind,
        "--started-at",
        STARTED_AT,
        "--completed-at",
        COMPLETED_AT,
    ]
    if trusted:
        args.append("--trusted-runner")
    args.extend(["--output", str(output)])
    return args


def _build_args(receipt: Path, config: Path, output: Path) -> list[str]:
    return [
        "build",
        "--commit",
        COMMIT,
        "--tree-digest",
        TREE_DIGEST,
        "--evidence",
        str(receipt),
        "--config",
        str(config),
        "--now",
        "2026-07-22T01:02:00Z",
        "--release-target",
        "mock",
        "--output",
        str(output),
    ]


def _provider_row(report: dict[str, object], provider: str) -> dict[str, object]:
    rows = report["providers"]
    assert isinstance(rows, list)
    return next(row for row in rows if row["provider"] == provider)


def _write_json(path: Path, payload: object) -> None:
    path.write_text(json.dumps(payload), encoding="utf-8")


def _read_json(path: Path) -> dict[str, object]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(payload, dict)
    return payload
