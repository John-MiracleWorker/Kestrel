from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from nested_memvid_agent.config import AgentConfig
from nested_memvid_agent.llm.model_catalog import PROVIDER_OPTIONS
from nested_memvid_agent.provider_certification import (
    EVIDENCE_CASES,
    PROVIDER_CERTIFICATION_EVIDENCE_SCHEMA,
    PROVIDER_CERTIFICATION_POLICY_VERSION,
    PROVIDER_CERTIFICATION_SCHEMA,
    PROVIDER_IMPLEMENTATION_REGISTRY,
    ProviderCertificationState,
    ProviderCertificationStatus,
    ProviderCertificationSubject,
    ProviderEvidenceLevel,
    ProviderEvidenceStatus,
    build_provider_certification_report,
    canonical_provider_evidence_id,
    parse_provider_certification_evidence,
    provider_config_digest,
)

NOW = datetime(2026, 7, 22, 12, 0, tzinfo=UTC)
SUBJECT = ProviderCertificationSubject(commit="a" * 40, tree_digest="b" * 64)


def _config(**overrides: Any) -> AgentConfig:
    values: dict[str, Any] = {"provider": "mock", "model": "mock"}
    values.update(overrides)
    return AgentConfig(**values)


def _receipt(
    config: AgentConfig,
    *,
    provider: str = "mock",
    model: str | None = None,
    profile: str = "release",
    level: str = ProviderEvidenceLevel.RELEASE.value,
    subject: ProviderCertificationSubject = SUBJECT,
    runner_trusted: bool = True,
    runner_kind: str = "release_ci",
    completed_at: datetime = NOW - timedelta(hours=1),
    cases: dict[str, str] | None = None,
    config_digest: str | None = None,
    source_digests: tuple[str, ...] = ("f" * 64,),
) -> dict[str, Any]:
    active_model = model or config.model
    active_cases = {case: ProviderEvidenceStatus.PASS.value for case in EVIDENCE_CASES}
    if cases:
        active_cases.update(cases)
    content = {
        "provider": provider,
        "model": active_model,
        "profile": profile,
        "level": level,
        "config_digest": config_digest or provider_config_digest(config, provider, active_model),
        "subject": subject.to_dict(),
        "runner": {"trusted": runner_trusted, "kind": runner_kind},
        "started_at": (completed_at - timedelta(minutes=5)).isoformat(),
        "completed_at": completed_at.isoformat(),
        "cases": active_cases,
        "source_digests": list(source_digests),
    }
    return {
        "schema": PROVIDER_CERTIFICATION_EVIDENCE_SCHEMA,
        "evidence_id": canonical_provider_evidence_id(content),
        **content,
    }


def test_provider_registry_exactly_covers_supported_provider_options() -> None:
    assert tuple(PROVIDER_IMPLEMENTATION_REGISTRY) == PROVIDER_OPTIONS
    assert {spec.provider for spec in PROVIDER_IMPLEMENTATION_REGISTRY.values()} == set(
        PROVIDER_OPTIONS
    )
    assert PROVIDER_IMPLEMENTATION_REGISTRY["codex-cli"].experimental is True
    assert PROVIDER_IMPLEMENTATION_REGISTRY["codex-cli"].stream is False
    assert PROVIDER_IMPLEMENTATION_REGISTRY["codex-cli"].native_tools is False


def test_no_evidence_reports_implementation_separately_from_environment_readiness(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "sk-provider-certification-must-not-leak-123456"
    endpoint = "https://provider.example.invalid/private-marker/v1"
    monkeypatch.setenv("OPENAI_API_KEY", secret)
    config = _config(
        provider="openai",
        model="gpt-test",
        base_url=endpoint,
        api_key_env="OPENAI_API_KEY",
    )

    report = build_provider_certification_report(config, now=NOW)
    payload = report.to_dict()
    openai = report.provider("openai")

    assert payload["schema"] == PROVIDER_CERTIFICATION_SCHEMA
    assert payload["policy_version"] == PROVIDER_CERTIFICATION_POLICY_VERSION
    assert payload["generated_at"] == "2026-07-22T12:00:00Z"
    assert payload["subject"] == {"commit": "unknown", "tree_digest": "unknown"}
    assert payload["max_evidence_age_seconds"] == 30 * 24 * 60 * 60
    assert openai.status == ProviderCertificationStatus.CONFIGURED
    assert openai.certification_state == ProviderCertificationState.IMPLEMENTED
    assert openai.generate.status == ProviderEvidenceStatus.NOT_RUN
    assert openai.last_tested is None
    assert "evidence_required" in openai.missing_requirements
    assert report.provider("mock").certification_state == ProviderCertificationState.IMPLEMENTED
    assert (
        report.provider("codex-cli").certification_state == ProviderCertificationState.EXPERIMENTAL
    )
    assert report.headline.release_certified is False
    assert report.headline.certified_count == 1
    assert report.headline.release_certified_count == 0
    assert sum(report.headline.readiness_counts.values()) == len(PROVIDER_OPTIONS)
    mock_payload = next(row for row in payload["providers"] if row["provider"] == "mock")
    assert mock_payload["status"] == "certified"
    assert mock_payload["readiness"]["status"] == "configured"
    assert report.headline.release_targets == ()
    serialized = json.dumps(payload, sort_keys=True)
    assert secret not in serialized
    assert endpoint not in serialized


def test_receipt_round_trip_is_strict_and_sanitizable() -> None:
    config = _config()
    payload = _receipt(config)

    receipt = parse_provider_certification_evidence(payload)

    assert receipt.to_dict() == {
        **payload,
        "started_at": "2026-07-22T10:55:00Z",
        "completed_at": "2026-07-22T11:00:00Z",
    }
    with pytest.raises(TypeError):
        receipt.cases["generate"] = ProviderEvidenceStatus.FAIL  # type: ignore[index]
    with pytest.raises(ValueError, match="keys are invalid"):
        parse_provider_certification_evidence({**payload, "endpoint": "https://secret.invalid"})
    skipped = {
        **payload,
        "cases": {**payload["cases"], "generate": "skipped"},
    }
    with pytest.raises(ValueError, match="invalid evidence status for generate"):
        parse_provider_certification_evidence(skipped)
    bad_runner = {
        **payload,
        "runner": {"trusted": True, "kind": "self_attested"},
    }
    with pytest.raises(ValueError, match="unsupported evidence runner kind"):
        parse_provider_certification_evidence(bad_runner)


def test_content_mutation_invalidates_an_authenticated_evidence_id() -> None:
    config = _config()
    receipt = _receipt(config)
    tampered = {
        **receipt,
        "cases": {
            **receipt["cases"],
            "generate": ProviderEvidenceStatus.FAIL.value,
        },
    }

    with pytest.raises(ValueError, match="canonical receipt digest"):
        build_provider_certification_report(
            config,
            evidence=[tampered],
            subject=SUBJECT,
            now=NOW,
            authenticated_evidence_ids={receipt["evidence_id"]},
        )


def test_source_digests_are_required_sorted_and_unique() -> None:
    config = _config()
    content = {
        key: value
        for key, value in _receipt(config).items()
        if key not in {"schema", "evidence_id"}
    }
    content["source_digests"] = []
    with pytest.raises(ValueError, match="non-empty list"):
        canonical_provider_evidence_id(content)
    with pytest.raises(ValueError, match="sorted and unique"):
        _receipt(config, source_digests=("f" * 64, "e" * 64))


def test_current_exact_scoped_release_evidence_requires_caller_authentication() -> None:
    config = _config()
    receipt = _receipt(config)

    forged = build_provider_certification_report(
        config,
        evidence=[receipt],
        subject=SUBJECT,
        now=NOW,
    )
    authenticated = build_provider_certification_report(
        config,
        evidence=[receipt],
        subject=SUBJECT,
        now=NOW,
        authenticated_evidence_ids={receipt["evidence_id"]},
    )

    forged_mock = forged.provider("mock")
    assert forged_mock.certification_state == ProviderCertificationState.IMPLEMENTED
    assert "authenticated_evidence_required" in forged_mock.missing_requirements
    assert forged_mock.generate.status == ProviderEvidenceStatus.NOT_RUN
    assert forged_mock.last_tested is None
    assert forged_mock.tested_profiles == ()
    assert forged_mock.evidence_ids == ()
    assert forged.headline.release_certified is False

    certified_mock = authenticated.provider("mock")
    assert certified_mock.certification_state == ProviderCertificationState.RELEASE_CERTIFIED
    assert certified_mock.generate.status == ProviderEvidenceStatus.PASS
    assert certified_mock.stream.status == ProviderEvidenceStatus.PASS
    assert certified_mock.native_tools.status == ProviderEvidenceStatus.PASS
    assert certified_mock.tool_normalization.status == ProviderEvidenceStatus.PASS
    assert certified_mock.learning_e2e.status == ProviderEvidenceStatus.PASS
    assert certified_mock.last_tested == "2026-07-22T11:00:00Z"
    assert certified_mock.tested_models == ("mock",)
    assert certified_mock.tested_profiles == ("release",)
    assert certified_mock.evidence_ids == (receipt["evidence_id"],)
    assert certified_mock.missing_requirements == ()
    assert authenticated.headline.release_targets == ("mock",)
    assert authenticated.headline.release_certified is True


@pytest.mark.parametrize(
    ("level", "runner_kind", "expected_state"),
    [
        (
            ProviderEvidenceLevel.MOCK.value,
            "mock",
            ProviderCertificationState.MOCK_TESTED,
        ),
        (
            ProviderEvidenceLevel.CREDENTIAL_FREE.value,
            "ci",
            ProviderCertificationState.CREDENTIAL_FREE_INTEGRATION_TESTED,
        ),
        (
            ProviderEvidenceLevel.LIVE.value,
            "local",
            ProviderCertificationState.LOCALLY_LIVE_TESTED,
        ),
    ],
)
def test_evidence_levels_map_to_distinct_assurance_states(
    level: str,
    runner_kind: str,
    expected_state: ProviderCertificationState,
) -> None:
    config = _config()
    receipt = _receipt(
        config,
        profile="default",
        level=level,
        runner_trusted=False,
        runner_kind=runner_kind,
    )

    row = build_provider_certification_report(
        config,
        evidence=[receipt],
        subject=SUBJECT,
        now=NOW,
    ).provider("mock")

    assert row.certification_state == expected_state
    assert row.missing_requirements == ()


@pytest.mark.parametrize(
    ("mutation", "missing_requirement"),
    [
        ({"model": "other-model"}, "model_scoped_evidence_required"),
        ({"config_digest": "c" * 64}, "config_scoped_evidence_required"),
        (
            {
                "subject": ProviderCertificationSubject(
                    commit="d" * 40,
                    tree_digest="e" * 64,
                )
            },
            "current_subject_evidence_required",
        ),
    ],
)
def test_wrong_scoped_evidence_cannot_raise_assurance(
    mutation: dict[str, Any],
    missing_requirement: str,
) -> None:
    config = _config()
    receipt = _receipt(config, **mutation)

    report = build_provider_certification_report(
        config,
        evidence=[receipt],
        subject=SUBJECT,
        now=NOW,
        authenticated_evidence_ids={receipt["evidence_id"]},
    )
    row = report.provider("mock")

    assert row.certification_state == ProviderCertificationState.IMPLEMENTED
    assert row.generate.status == ProviderEvidenceStatus.NOT_RUN
    assert missing_requirement in row.missing_requirements
    assert "matching_evidence_required" in row.missing_requirements


def test_stale_failing_and_not_supported_claims_fail_closed() -> None:
    config = _config()
    stale = _receipt(
        config,
        level=ProviderEvidenceLevel.MOCK.value,
        completed_at=NOW - timedelta(days=31),
    )
    failing = _receipt(
        config,
        level=ProviderEvidenceLevel.MOCK.value,
        cases={"generate": ProviderEvidenceStatus.FAIL.value},
    )
    false_unsupported = _receipt(
        config,
        level=ProviderEvidenceLevel.MOCK.value,
        cases={"native_tools": ProviderEvidenceStatus.NOT_SUPPORTED.value},
    )

    stale_row = build_provider_certification_report(
        config, evidence=[stale], subject=SUBJECT, now=NOW
    ).provider("mock")
    failing_row = build_provider_certification_report(
        config, evidence=[failing], subject=SUBJECT, now=NOW
    ).provider("mock")
    unsupported_row = build_provider_certification_report(
        config, evidence=[false_unsupported], subject=SUBJECT, now=NOW
    ).provider("mock")

    assert stale_row.certification_state == ProviderCertificationState.IMPLEMENTED
    assert stale_row.generate.status == ProviderEvidenceStatus.STALE
    assert "stale_evidence" in stale_row.missing_requirements
    assert failing_row.certification_state == ProviderCertificationState.IMPLEMENTED
    assert failing_row.generate.status == ProviderEvidenceStatus.FAIL
    assert "generate_pass_required" in failing_row.missing_requirements
    assert unsupported_row.certification_state == ProviderCertificationState.IMPLEMENTED
    assert unsupported_row.native_tools.status == ProviderEvidenceStatus.FAIL
    assert "native_tools_pass_required" in unsupported_row.missing_requirements


def test_newer_failure_supersedes_older_pass_at_the_same_level() -> None:
    config = _config()
    older_pass = _receipt(
        config,
        level=ProviderEvidenceLevel.MOCK.value,
        completed_at=NOW - timedelta(hours=2),
    )
    newer_fail = _receipt(
        config,
        level=ProviderEvidenceLevel.MOCK.value,
        completed_at=NOW - timedelta(hours=1),
        cases={"generate": ProviderEvidenceStatus.FAIL.value},
    )

    row = build_provider_certification_report(
        config,
        evidence=[older_pass, newer_fail],
        subject=SUBJECT,
        now=NOW,
    ).provider("mock")

    assert row.certification_state == ProviderCertificationState.IMPLEMENTED
    assert row.generate == row.generate.__class__(
        status=ProviderEvidenceStatus.FAIL,
        evidence_ids=(newer_fail["evidence_id"],),
    )


def test_newer_pass_supersedes_older_failure_at_the_same_level() -> None:
    config = _config()
    older_fail = _receipt(
        config,
        level=ProviderEvidenceLevel.MOCK.value,
        completed_at=NOW - timedelta(hours=2),
        cases={"generate": ProviderEvidenceStatus.FAIL.value},
    )
    newer_pass = _receipt(
        config,
        level=ProviderEvidenceLevel.MOCK.value,
        completed_at=NOW - timedelta(hours=1),
    )

    row = build_provider_certification_report(
        config,
        evidence=[older_fail, newer_pass],
        subject=SUBJECT,
        now=NOW,
    ).provider("mock")

    assert row.certification_state == ProviderCertificationState.MOCK_TESTED
    assert row.generate.status == ProviderEvidenceStatus.PASS
    assert "generate_pass_required" not in row.missing_requirements


def test_valid_release_dominates_an_older_lower_level_failure() -> None:
    config = _config()
    older_mock_failure = _receipt(
        config,
        level=ProviderEvidenceLevel.MOCK.value,
        completed_at=NOW - timedelta(hours=2),
        cases={"generate": ProviderEvidenceStatus.FAIL.value},
    )
    release = _receipt(
        config,
        completed_at=NOW - timedelta(hours=1),
    )

    report = build_provider_certification_report(
        config,
        evidence=[older_mock_failure, release],
        subject=SUBJECT,
        now=NOW,
        authenticated_evidence_ids={release["evidence_id"]},
    )
    row = report.provider("mock")

    assert row.certification_state == ProviderCertificationState.RELEASE_CERTIFIED
    assert row.missing_requirements == ()
    assert row.generate.status == ProviderEvidenceStatus.PASS
    assert row.generate.evidence_ids == (release["evidence_id"],)
    assert report.headline.release_certified is True


def test_newer_lower_level_failure_revokes_an_older_release_claim() -> None:
    config = _config()
    release = _receipt(
        config,
        completed_at=NOW - timedelta(hours=2),
    )
    newer_mock_failure = _receipt(
        config,
        level=ProviderEvidenceLevel.MOCK.value,
        completed_at=NOW - timedelta(hours=1),
        cases={"generate": ProviderEvidenceStatus.FAIL.value},
    )

    report = build_provider_certification_report(
        config,
        evidence=[release, newer_mock_failure],
        subject=SUBJECT,
        now=NOW,
        authenticated_evidence_ids={release["evidence_id"]},
    )
    row = report.provider("mock")

    assert row.certification_state == ProviderCertificationState.IMPLEMENTED
    assert row.generate.status == ProviderEvidenceStatus.FAIL
    assert "generate_pass_required" in row.missing_requirements
    assert report.headline.release_certified is False


def test_newer_failure_in_another_profile_does_not_revoke_release() -> None:
    config = _config()
    release = _receipt(
        config,
        completed_at=NOW - timedelta(hours=2),
    )
    default_profile_failure = _receipt(
        config,
        profile="default",
        level=ProviderEvidenceLevel.MOCK.value,
        runner_kind="mock",
        completed_at=NOW - timedelta(hours=1),
        cases={"generate": ProviderEvidenceStatus.FAIL.value},
    )

    report = build_provider_certification_report(
        config,
        evidence=[release, default_profile_failure],
        subject=SUBJECT,
        now=NOW,
        authenticated_evidence_ids={release["evidence_id"]},
    )
    row = report.provider("mock")

    assert row.certification_state == ProviderCertificationState.RELEASE_CERTIFIED
    assert row.generate.status == ProviderEvidenceStatus.PASS
    assert row.generate.evidence_ids == (release["evidence_id"],)
    assert row.missing_requirements == ()
    assert report.headline.release_certified is True


def test_unauthenticated_release_failure_cannot_revoke_older_release() -> None:
    config = _config()
    release = _receipt(
        config,
        completed_at=NOW - timedelta(hours=2),
    )
    unauthenticated_failure = _receipt(
        config,
        completed_at=NOW - timedelta(hours=1),
        cases={"generate": ProviderEvidenceStatus.FAIL.value},
    )

    report = build_provider_certification_report(
        config,
        evidence=[release, unauthenticated_failure],
        subject=SUBJECT,
        now=NOW,
        authenticated_evidence_ids={release["evidence_id"]},
    )
    row = report.provider("mock")

    assert row.certification_state == ProviderCertificationState.RELEASE_CERTIFIED
    assert row.generate.status == ProviderEvidenceStatus.PASS
    assert row.generate.evidence_ids == (release["evidence_id"],)
    assert row.last_tested == "2026-07-22T10:00:00Z"
    assert row.evidence_ids == (release["evidence_id"],)
    assert report.headline.release_certified is True


def test_lower_assurance_keeps_supplied_release_blockers_visible() -> None:
    config = _config()
    live = _receipt(
        config,
        profile="live",
        level=ProviderEvidenceLevel.LIVE.value,
        runner_trusted=False,
        runner_kind="local",
        completed_at=NOW - timedelta(hours=2),
    )
    unauthenticated_release = _receipt(
        config,
        completed_at=NOW - timedelta(hours=1),
    )

    report = build_provider_certification_report(
        config,
        evidence=[live, unauthenticated_release],
        subject=SUBJECT,
        now=NOW,
    )
    row = report.provider("mock")

    assert row.certification_state == ProviderCertificationState.LOCALLY_LIVE_TESTED
    assert row.generate.status == ProviderEvidenceStatus.PASS
    assert row.generate.evidence_ids == (live["evidence_id"],)
    assert "authenticated_evidence_required" in row.missing_requirements
    assert report.headline.release_certified is False


def test_authenticated_release_failure_revokes_older_release() -> None:
    config = _config()
    release = _receipt(
        config,
        completed_at=NOW - timedelta(hours=2),
    )
    authenticated_failure = _receipt(
        config,
        completed_at=NOW - timedelta(hours=1),
        cases={"generate": ProviderEvidenceStatus.FAIL.value},
    )

    report = build_provider_certification_report(
        config,
        evidence=[release, authenticated_failure],
        subject=SUBJECT,
        now=NOW,
        authenticated_evidence_ids={
            release["evidence_id"],
            authenticated_failure["evidence_id"],
        },
    )
    row = report.provider("mock")

    assert row.certification_state == ProviderCertificationState.IMPLEMENTED
    assert row.generate.status == ProviderEvidenceStatus.FAIL
    assert row.generate.evidence_ids == (authenticated_failure["evidence_id"],)
    assert report.headline.release_certified is False


def test_incomplete_release_evidence_does_not_downgrade_to_live() -> None:
    config = _config()
    receipt = _receipt(
        config,
        cases={"policy_unchanged": ProviderEvidenceStatus.NOT_RUN.value},
    )

    row = build_provider_certification_report(
        config,
        evidence=[receipt],
        subject=SUBJECT,
        now=NOW,
        authenticated_evidence_ids={receipt["evidence_id"]},
    ).provider("mock")

    assert row.certification_state == ProviderCertificationState.IMPLEMENTED
    assert row.learning_e2e.status == ProviderEvidenceStatus.NOT_RUN
    assert "policy_unchanged_pass_required" in row.missing_requirements


def test_level_profile_and_runner_pairs_are_strictly_validated() -> None:
    config = _config()
    valid = _receipt(config)
    content = {key: value for key, value in valid.items() if key not in {"schema", "evidence_id"}}

    with pytest.raises(ValueError, match="profile is not eligible"):
        canonical_provider_evidence_id({**content, "profile": "developer"})
    with pytest.raises(ValueError, match="runner kind is not eligible"):
        canonical_provider_evidence_id({**content, "runner": {"trusted": True, "kind": "local"}})


def test_evidence_from_different_profiles_cannot_combine_into_release_claim() -> None:
    config = _config()
    release_profile = _receipt(
        config,
        profile="release",
        completed_at=NOW - timedelta(hours=2),
        cases={"learning_memvid": ProviderEvidenceStatus.NOT_RUN.value},
    )
    default_profile = _receipt(
        config,
        profile="default",
        level=ProviderEvidenceLevel.MOCK.value,
        runner_kind="mock",
        completed_at=NOW - timedelta(hours=1),
        cases={"generate": ProviderEvidenceStatus.NOT_RUN.value},
    )

    row = build_provider_certification_report(
        config,
        evidence=[release_profile, default_profile],
        subject=SUBJECT,
        now=NOW,
        authenticated_evidence_ids={
            release_profile["evidence_id"],
        },
    ).provider("mock")

    assert row.certification_state == ProviderCertificationState.IMPLEMENTED
    assert row.tested_profiles == ("default", "release")
    assert row.generate.status == ProviderEvidenceStatus.NOT_RUN
    assert "generate_pass_required" in row.missing_requirements


def test_explicit_release_targets_are_nonempty_validated_and_all_must_pass() -> None:
    config = _config()
    receipt = _receipt(config)

    no_target = build_provider_certification_report(
        config,
        evidence=[receipt],
        subject=SUBJECT,
        now=NOW,
        authenticated_evidence_ids={receipt["evidence_id"]},
        release_targets=[],
    )
    multi_target = build_provider_certification_report(
        config,
        evidence=[receipt],
        subject=SUBJECT,
        now=NOW,
        authenticated_evidence_ids={receipt["evidence_id"]},
        release_targets=["mock", "openai"],
    )

    assert no_target.headline.release_certified is False
    assert no_target.headline.release_targets == ()
    assert multi_target.headline.release_certified is False
    assert multi_target.headline.release_targets == ("mock", "openai")
    with pytest.raises(ValueError, match="unsupported release certification targets"):
        build_provider_certification_report(config, release_targets=["unknown-provider"])


def test_duplicate_evidence_ids_are_rejected() -> None:
    config = _config()
    receipt = _receipt(config)

    with pytest.raises(ValueError, match="duplicate provider certification evidence ids"):
        build_provider_certification_report(
            config,
            evidence=[receipt, receipt],
            subject=SUBJECT,
            now=NOW,
        )


def test_report_is_deterministic_under_injected_clock_and_subject() -> None:
    config = _config()
    receipt = _receipt(config, level=ProviderEvidenceLevel.MOCK.value)

    first = build_provider_certification_report(
        config,
        evidence=[receipt],
        subject=SUBJECT,
        now=NOW,
    ).to_dict()
    second = build_provider_certification_report(
        config,
        evidence=[receipt],
        subject=SUBJECT,
        now=NOW,
    ).to_dict()

    assert first == second


def test_evidence_freshness_window_can_only_be_tightened() -> None:
    config = _config()

    shortened = build_provider_certification_report(
        config,
        now=NOW,
        max_evidence_age=timedelta(days=7),
    )

    assert shortened.max_evidence_age_seconds == 7 * 24 * 60 * 60
    with pytest.raises(ValueError, match="must not exceed"):
        build_provider_certification_report(
            config,
            now=NOW,
            max_evidence_age=timedelta(days=31),
        )
