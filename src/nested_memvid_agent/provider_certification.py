from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import shutil
from collections.abc import Iterable, Mapping, Set
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from types import MappingProxyType
from typing import Any, cast

from .config import AgentConfig
from .event_log import redact_secrets
from .llm.model_catalog import (
    DEFAULT_BASE_URLS,
    PROVIDER_OPTIONS,
    STATIC_MODEL_SUGGESTIONS,
    default_api_key_env,
)

PROVIDER_CERTIFICATION_SCHEMA = "kestrel.provider_certification.v2"
PROVIDER_CERTIFICATION_EVIDENCE_SCHEMA = "kestrel.provider_certification_evidence.v1"
PROVIDER_CERTIFICATION_POLICY_VERSION = "kestrel.provider_certification_policy.v1"
DEFAULT_MAX_EVIDENCE_AGE = timedelta(days=30)

EVIDENCE_CASES: tuple[str, ...] = (
    "generate",
    "stream",
    "native_tools",
    "tool_normalization",
    "learning_memory",
    "learning_memvid",
    "policy_unchanged",
)
EVIDENCE_RUNNER_KINDS: tuple[str, ...] = ("local", "ci", "release_ci", "mock")
EVIDENCE_CONTENT_KEYS: frozenset[str] = frozenset(
    {
        "provider",
        "model",
        "profile",
        "level",
        "config_digest",
        "subject",
        "runner",
        "started_at",
        "completed_at",
        "cases",
        "source_digests",
    }
)

_SAFE_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/+-]{0,255}$")
_HEX_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_GIT_COMMIT = re.compile(r"^[0-9a-f]{7,64}$")


class ProviderCertificationStatus(StrEnum):
    """Environment-readiness status retained under its v1 public name.

    The v2 report never uses this field to raise a provider's assurance state.
    """

    CERTIFIED = "certified"
    CONFIGURED = "configured"
    BLOCKED = "blocked"
    MANUAL_VALIDATION_REQUIRED = "manual_validation_required"


class ProviderCertificationState(StrEnum):
    IMPLEMENTED = "implemented"
    MOCK_TESTED = "mock_tested"
    CREDENTIAL_FREE_INTEGRATION_TESTED = "credential_free_integration_tested"
    LOCALLY_LIVE_TESTED = "locally_live_tested"
    RELEASE_CERTIFIED = "release_certified"
    EXPERIMENTAL = "experimental"


class ProviderEvidenceStatus(StrEnum):
    PASS = "pass"
    FAIL = "fail"
    NOT_RUN = "not_run"
    NOT_SUPPORTED = "not_supported"
    STALE = "stale"


class ProviderEvidenceLevel(StrEnum):
    MOCK = "mock"
    CREDENTIAL_FREE = "credential_free"
    LIVE = "live"
    RELEASE = "release"


_EVIDENCE_LEVEL_PROFILES: Mapping[ProviderEvidenceLevel, frozenset[str]] = {
    ProviderEvidenceLevel.MOCK: frozenset({"default", "mock", "release"}),
    ProviderEvidenceLevel.CREDENTIAL_FREE: frozenset({"default", "credential_free", "release"}),
    ProviderEvidenceLevel.LIVE: frozenset({"default", "live", "release"}),
    ProviderEvidenceLevel.RELEASE: frozenset({"release"}),
}
_EVIDENCE_LEVEL_RUNNERS: Mapping[ProviderEvidenceLevel, frozenset[str]] = {
    ProviderEvidenceLevel.MOCK: frozenset({"mock", "ci", "local", "release_ci"}),
    ProviderEvidenceLevel.CREDENTIAL_FREE: frozenset({"ci", "local", "release_ci"}),
    ProviderEvidenceLevel.LIVE: frozenset({"local", "release_ci"}),
    ProviderEvidenceLevel.RELEASE: frozenset({"release_ci"}),
}


@dataclass(frozen=True)
class ProviderCertificationSubject:
    commit: str
    tree_digest: str

    def to_dict(self) -> dict[str, str]:
        return {"commit": self.commit, "tree_digest": self.tree_digest}


@dataclass(frozen=True)
class ProviderEvidenceRunner:
    trusted: bool
    kind: str

    def to_dict(self) -> dict[str, object]:
        return {"trusted": self.trusted, "kind": self.kind}


@dataclass(frozen=True)
class ProviderCertificationEvidence:
    evidence_id: str
    provider: str
    model: str
    profile: str
    level: ProviderEvidenceLevel
    config_digest: str
    subject: ProviderCertificationSubject
    runner: ProviderEvidenceRunner
    started_at: datetime
    completed_at: datetime
    cases: Mapping[str, ProviderEvidenceStatus]
    source_digests: tuple[str, ...]
    schema: str = PROVIDER_CERTIFICATION_EVIDENCE_SCHEMA

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> ProviderCertificationEvidence:
        return cls._from_dict(payload, validate_evidence_id=True)

    @classmethod
    def _from_dict(
        cls,
        payload: Mapping[str, Any],
        *,
        validate_evidence_id: bool,
    ) -> ProviderCertificationEvidence:
        _require_exact_keys(
            payload,
            {*EVIDENCE_CONTENT_KEYS, "schema", "evidence_id"},
            "provider certification evidence",
        )
        schema = _required_string(payload, "schema")
        if schema != PROVIDER_CERTIFICATION_EVIDENCE_SCHEMA:
            raise ValueError(f"unsupported provider certification evidence schema: {schema}")

        evidence_id = _safe_identifier(_required_string(payload, "evidence_id"), "evidence_id")
        provider = _required_string(payload, "provider")
        if provider not in PROVIDER_OPTIONS:
            raise ValueError(f"unsupported evidence provider: {provider}")
        model = _safe_identifier(_required_string(payload, "model"), "model")
        profile = _safe_identifier(_required_string(payload, "profile"), "profile")

        try:
            level = ProviderEvidenceLevel(_required_string(payload, "level"))
        except ValueError as exc:
            raise ValueError("invalid provider certification evidence level") from exc

        config_digest = _required_string(payload, "config_digest")
        if not _HEX_DIGEST.fullmatch(config_digest):
            raise ValueError("config_digest must be a lowercase SHA-256 digest")

        subject_payload = _required_mapping(payload, "subject")
        _require_exact_keys(subject_payload, {"commit", "tree_digest"}, "evidence subject")
        commit = _required_string(subject_payload, "commit")
        tree_digest = _required_string(subject_payload, "tree_digest")
        if not _GIT_COMMIT.fullmatch(commit):
            raise ValueError("subject.commit must be a Git commit digest")
        if not _HEX_DIGEST.fullmatch(tree_digest):
            raise ValueError("subject.tree_digest must be a lowercase SHA-256 digest")

        runner_payload = _required_mapping(payload, "runner")
        _require_exact_keys(runner_payload, {"trusted", "kind"}, "evidence runner")
        trusted = runner_payload.get("trusted")
        if not isinstance(trusted, bool):
            raise ValueError("runner.trusted must be a boolean")
        runner_kind = _safe_identifier(_required_string(runner_payload, "kind"), "runner.kind")
        if runner_kind not in EVIDENCE_RUNNER_KINDS:
            raise ValueError(f"unsupported evidence runner kind: {runner_kind}")
        if profile not in _EVIDENCE_LEVEL_PROFILES[level]:
            raise ValueError(f"profile is not eligible for evidence level {level.value}")
        if runner_kind not in _EVIDENCE_LEVEL_RUNNERS[level]:
            raise ValueError(f"runner kind is not eligible for evidence level {level.value}")

        started_at = _parse_timestamp(_required_string(payload, "started_at"), "started_at")
        completed_at = _parse_timestamp(_required_string(payload, "completed_at"), "completed_at")
        if completed_at < started_at:
            raise ValueError("completed_at must not be before started_at")

        cases_payload = _required_mapping(payload, "cases")
        _require_exact_keys(cases_payload, set(EVIDENCE_CASES), "evidence cases")
        cases: dict[str, ProviderEvidenceStatus] = {}
        for case_name in EVIDENCE_CASES:
            try:
                cases[case_name] = ProviderEvidenceStatus(
                    _required_string(cases_payload, case_name)
                )
            except ValueError as exc:
                raise ValueError(f"invalid evidence status for {case_name}") from exc

        raw_source_digests = payload.get("source_digests")
        if not isinstance(raw_source_digests, list) or not raw_source_digests:
            raise ValueError("source_digests must be a non-empty list")
        if not all(
            isinstance(digest, str) and _HEX_DIGEST.fullmatch(digest)
            for digest in raw_source_digests
        ):
            raise ValueError("source_digests must contain lowercase SHA-256 digests")
        source_digests = tuple(cast(list[str], raw_source_digests))
        if source_digests != tuple(sorted(set(source_digests))):
            raise ValueError("source_digests must be sorted and unique")

        receipt = cls(
            schema=schema,
            evidence_id=evidence_id,
            provider=provider,
            model=model,
            profile=profile,
            level=level,
            config_digest=config_digest,
            subject=ProviderCertificationSubject(commit=commit, tree_digest=tree_digest),
            runner=ProviderEvidenceRunner(trusted=trusted, kind=runner_kind),
            started_at=started_at,
            completed_at=completed_at,
            cases=MappingProxyType(cases),
            source_digests=source_digests,
        )
        if validate_evidence_id and receipt.evidence_id != _canonical_evidence_id(receipt):
            raise ValueError("evidence_id does not match the canonical receipt digest")
        return receipt

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "schema": self.schema,
            "evidence_id": self.evidence_id,
            "provider": self.provider,
            "model": self.model,
            "profile": self.profile,
            "level": self.level.value,
            "config_digest": self.config_digest,
            "subject": self.subject.to_dict(),
            "runner": self.runner.to_dict(),
            "started_at": _format_timestamp(self.started_at),
            "completed_at": _format_timestamp(self.completed_at),
            "cases": {case: self.cases[case].value for case in EVIDENCE_CASES},
            "source_digests": list(self.source_digests),
        }
        return cast(dict[str, Any], redact_secrets(payload))


@dataclass(frozen=True)
class ProviderImplementationSpec:
    provider: str
    generate: bool = True
    stream: bool = True
    native_tools: bool = True
    tool_normalization: bool = True
    learning_e2e: bool = True
    experimental: bool = False


_PROVIDER_IMPLEMENTATION_SPECS: dict[str, ProviderImplementationSpec] = {
    provider: ProviderImplementationSpec(provider=provider) for provider in PROVIDER_OPTIONS
}
_PROVIDER_IMPLEMENTATION_SPECS["codex-cli"] = ProviderImplementationSpec(
    provider="codex-cli",
    stream=False,
    native_tools=False,
    experimental=True,
)
PROVIDER_IMPLEMENTATION_REGISTRY: Mapping[str, ProviderImplementationSpec] = MappingProxyType(
    _PROVIDER_IMPLEMENTATION_SPECS
)


@dataclass(frozen=True)
class ProviderCertificationDimension:
    status: ProviderEvidenceStatus
    evidence_ids: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {"status": self.status.value, "evidence_ids": list(self.evidence_ids)}


@dataclass(frozen=True)
class ProviderCertificationEntry:
    provider: str
    status: ProviderCertificationStatus
    model_suggestions: tuple[str, ...]
    api_key_env: dict[str, Any] | None
    base_url_configured: bool
    evidence: tuple[str, ...]
    next_action: str
    live_validation_command: str
    generate: ProviderCertificationDimension
    stream: ProviderCertificationDimension
    native_tools: ProviderCertificationDimension
    tool_normalization: ProviderCertificationDimension
    learning_e2e: ProviderCertificationDimension
    last_tested: str | None
    certification_state: ProviderCertificationState
    tested_models: tuple[str, ...]
    tested_profiles: tuple[str, ...]
    missing_requirements: tuple[str, ...]
    evidence_ids: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        readiness = {
            "status": _readiness_status(self.status),
            "api_key_env": self.api_key_env,
            "endpoint_configured": self.base_url_configured,
        }
        return {
            "provider": self.provider,
            "status": self.status.value,
            "readiness": readiness,
            "model_suggestions": list(self.model_suggestions),
            "api_key_env": self.api_key_env,
            "base_url_configured": self.base_url_configured,
            "evidence": list(self.evidence),
            "next_action": self.next_action,
            "live_validation_command": self.live_validation_command,
            "generate": self.generate.to_dict(),
            "stream": self.stream.to_dict(),
            "native_tools": self.native_tools.to_dict(),
            "tool_normalization": self.tool_normalization.to_dict(),
            "learning_e2e": self.learning_e2e.to_dict(),
            "last_tested": self.last_tested,
            "certification_state": self.certification_state.value,
            "tested_models": list(self.tested_models),
            "tested_profiles": list(self.tested_profiles),
            "missing_requirements": list(self.missing_requirements),
            "evidence_ids": list(self.evidence_ids),
        }


@dataclass(frozen=True)
class ProviderCertificationHeadline:
    total_providers: int
    certified_count: int
    release_certified_count: int
    configured_count: int
    blocked_count: int
    manual_validation_required_count: int
    release_certified: bool
    state_counts: Mapping[str, int]
    readiness_counts: Mapping[str, int]
    evidence_receipt_count: int
    release_targets: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "total_providers": self.total_providers,
            "certified_count": self.certified_count,
            "release_certified_count": self.release_certified_count,
            "configured_count": self.configured_count,
            "blocked_count": self.blocked_count,
            "manual_validation_required_count": self.manual_validation_required_count,
            "release_certified": self.release_certified,
            "state_counts": dict(self.state_counts),
            "readiness_counts": dict(self.readiness_counts),
            "evidence_receipt_count": self.evidence_receipt_count,
            "release_targets": list(self.release_targets),
        }


@dataclass(frozen=True)
class ProviderCertificationReport:
    schema: str
    policy_version: str
    generated_at: str
    subject: ProviderCertificationSubject
    max_evidence_age_seconds: int
    headline: ProviderCertificationHeadline
    providers: tuple[ProviderCertificationEntry, ...]

    def provider(self, provider_name: str) -> ProviderCertificationEntry:
        for provider in self.providers:
            if provider.provider == provider_name:
                return provider
        raise KeyError(provider_name)

    def to_dict(self) -> dict[str, Any]:
        payload = {
            "schema": self.schema,
            "policy_version": self.policy_version,
            "generated_at": self.generated_at,
            "subject": self.subject.to_dict(),
            "max_evidence_age_seconds": self.max_evidence_age_seconds,
            "headline": self.headline.to_dict(),
            "providers": [provider.to_dict() for provider in self.providers],
        }
        return cast(dict[str, Any], redact_secrets(payload))


def parse_provider_certification_evidence(
    payload: Mapping[str, Any],
) -> ProviderCertificationEvidence:
    return ProviderCertificationEvidence.from_dict(payload)


def canonical_provider_evidence_id(payload: Mapping[str, Any]) -> str:
    """Return the content-addressed ID for exact validated receipt fields.

    ``payload`` intentionally excludes ``schema`` and ``evidence_id`` so a
    collector cannot accidentally authenticate a caller-chosen identifier.
    """

    _require_exact_keys(payload, set(EVIDENCE_CONTENT_KEYS), "provider evidence content")
    provisional = ProviderCertificationEvidence._from_dict(
        {
            "schema": PROVIDER_CERTIFICATION_EVIDENCE_SCHEMA,
            "evidence_id": "pending",
            **payload,
        },
        validate_evidence_id=False,
    )
    return _canonical_evidence_id(provisional)


def build_provider_certification_report(
    config: AgentConfig,
    *,
    evidence: Iterable[ProviderCertificationEvidence | Mapping[str, Any]] = (),
    subject: ProviderCertificationSubject | None = None,
    now: datetime | None = None,
    max_evidence_age: timedelta = DEFAULT_MAX_EVIDENCE_AGE,
    authenticated_evidence_ids: Set[str] = frozenset(),
    release_targets: Iterable[str] | None = None,
) -> ProviderCertificationReport:
    _validate_registry()
    if max_evidence_age <= timedelta(0):
        raise ValueError("max_evidence_age must be greater than zero")
    if max_evidence_age > DEFAULT_MAX_EVIDENCE_AGE:
        raise ValueError("max_evidence_age must not exceed the certification policy maximum")
    compared_at = _as_utc(now or datetime.now(UTC))
    active_subject = subject or ProviderCertificationSubject(
        commit="unknown",
        tree_digest="unknown",
    )
    _validate_subject(active_subject, allow_unknown=True)
    parsed_evidence = tuple(
        parse_provider_certification_evidence(item.to_dict())
        if isinstance(item, ProviderCertificationEvidence)
        else parse_provider_certification_evidence(item)
        for item in evidence
    )
    _reject_duplicate_evidence_ids(parsed_evidence)
    active_release_targets = _release_targets(parsed_evidence, release_targets)

    providers = tuple(
        _provider_entry(
            config,
            provider,
            parsed_evidence,
            subject=active_subject,
            now=compared_at,
            max_evidence_age=max_evidence_age,
            authenticated_evidence_ids=authenticated_evidence_ids,
        )
        for provider in PROVIDER_OPTIONS
    )
    release_certified_count = sum(
        1
        for provider in providers
        if provider.certification_state == ProviderCertificationState.RELEASE_CERTIFIED
    )
    certified_count = sum(
        1 for provider in providers if provider.status == ProviderCertificationStatus.CERTIFIED
    )
    configured_count = sum(
        1 for provider in providers if provider.status == ProviderCertificationStatus.CONFIGURED
    )
    blocked_count = sum(
        1 for provider in providers if provider.status == ProviderCertificationStatus.BLOCKED
    )
    manual_count = sum(
        1
        for provider in providers
        if provider.status == ProviderCertificationStatus.MANUAL_VALIDATION_REQUIRED
    )
    state_counts = {
        state.value: sum(1 for provider in providers if provider.certification_state == state)
        for state in ProviderCertificationState
    }
    readiness_counts = {
        status: sum(1 for provider in providers if _readiness_status(provider.status) == status)
        for status in ("configured", "blocked", "manual_validation_required")
    }
    return ProviderCertificationReport(
        schema=PROVIDER_CERTIFICATION_SCHEMA,
        policy_version=PROVIDER_CERTIFICATION_POLICY_VERSION,
        generated_at=_format_timestamp(compared_at),
        subject=active_subject,
        max_evidence_age_seconds=int(max_evidence_age.total_seconds()),
        headline=ProviderCertificationHeadline(
            total_providers=len(providers),
            certified_count=certified_count,
            release_certified_count=release_certified_count,
            configured_count=configured_count,
            blocked_count=blocked_count,
            manual_validation_required_count=manual_count,
            release_certified=bool(active_release_targets)
            and all(
                next(
                    provider.certification_state
                    for provider in providers
                    if provider.provider == target
                )
                == ProviderCertificationState.RELEASE_CERTIFIED
                for target in active_release_targets
            ),
            state_counts=state_counts,
            readiness_counts=readiness_counts,
            evidence_receipt_count=len(parsed_evidence),
            release_targets=active_release_targets,
        ),
        providers=providers,
    )


def provider_config_digest(
    config: AgentConfig,
    provider: str,
    model: str | None = None,
) -> str:
    if provider not in PROVIDER_OPTIONS:
        raise ValueError(f"unsupported provider: {provider}")
    active_model = model or _model_for(config, provider)
    material = {
        "provider": provider,
        "model": active_model,
        "endpoint": _base_url_for(config, provider) or "<none>",
        "api_key_env": _api_key_env_for(config, provider) or "<none>",
    }
    encoded = json.dumps(material, sort_keys=True, separators=(",", ":")).encode("utf-8")
    # This is a deterministic configuration fingerprint, not password storage or
    # an authentication tag.  A fixed domain key keeps the digest separate from
    # raw SHA-256 uses while preserving exact, non-secret configuration identity.
    return hmac.digest(b"kestrel-provider-config-v1", encoded, "sha256").hex()


def _canonical_evidence_id(receipt: ProviderCertificationEvidence) -> str:
    material = {
        "schema": receipt.schema,
        "provider": receipt.provider,
        "model": receipt.model,
        "profile": receipt.profile,
        "level": receipt.level.value,
        "config_digest": receipt.config_digest,
        "subject": receipt.subject.to_dict(),
        "runner": receipt.runner.to_dict(),
        "started_at": _format_timestamp(receipt.started_at),
        "completed_at": _format_timestamp(receipt.completed_at),
        "cases": {case: receipt.cases[case].value for case in EVIDENCE_CASES},
        "source_digests": list(receipt.source_digests),
    }
    encoded = json.dumps(
        material,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return "pce_" + hashlib.sha256(encoded).hexdigest()


def _provider_entry(
    config: AgentConfig,
    provider: str,
    receipts: tuple[ProviderCertificationEvidence, ...],
    *,
    subject: ProviderCertificationSubject,
    now: datetime,
    max_evidence_age: timedelta,
    authenticated_evidence_ids: Set[str],
) -> ProviderCertificationEntry:
    readiness = _provider_readiness(config, provider)
    spec = PROVIDER_IMPLEMENTATION_REGISTRY[provider]
    expected_model = _model_for(config, provider)
    expected_digest = provider_config_digest(config, provider, expected_model)
    provider_receipts = tuple(receipt for receipt in receipts if receipt.provider == provider)
    scoped_receipts = tuple(
        receipt
        for receipt in provider_receipts
        if receipt.model == expected_model
        and receipt.config_digest == expected_digest
        and receipt.subject == subject
    )
    reportable_receipts = _reportable_receipts(
        scoped_receipts,
        authenticated_evidence_ids=authenticated_evidence_ids,
    )

    assessment = _certification_assessment(
        spec,
        reportable_receipts,
        subject=subject,
        now=now,
        max_evidence_age=max_evidence_age,
        authenticated_evidence_ids=authenticated_evidence_ids,
    )
    certification_state = assessment.state
    dimension_receipts = (
        (assessment.evidence,)
        if assessment.evidence is not None
        else _latest_receipt(reportable_receipts)
    )
    dimensions = _dimensions_for(
        spec,
        dimension_receipts,
        now=now,
        max_evidence_age=max_evidence_age,
    )
    missing_requirements = _missing_requirements(
        spec,
        provider_receipts,
        scoped_receipts,
        expected_model=expected_model,
        expected_digest=expected_digest,
        subject=subject,
        now=now,
        max_evidence_age=max_evidence_age,
        certification_state=certification_state,
        assessment_evidence=assessment.evidence,
        authenticated_evidence_ids=authenticated_evidence_ids,
    )
    evidence_ids = tuple(sorted(receipt.evidence_id for receipt in reportable_receipts))
    last_tested = (
        _format_timestamp(max(receipt.completed_at for receipt in reportable_receipts))
        if reportable_receipts
        else None
    )
    tested_models = tuple(sorted({receipt.model for receipt in reportable_receipts}))
    tested_profiles = tuple(sorted({receipt.profile for receipt in reportable_receipts}))
    return ProviderCertificationEntry(
        provider=provider,
        status=readiness.status,
        model_suggestions=STATIC_MODEL_SUGGESTIONS.get(provider, ()),
        api_key_env=readiness.api_key_env,
        base_url_configured=readiness.endpoint_configured,
        evidence=readiness.evidence,
        next_action=readiness.next_action,
        live_validation_command=_live_validation_command(provider),
        generate=dimensions["generate"],
        stream=dimensions["stream"],
        native_tools=dimensions["native_tools"],
        tool_normalization=dimensions["tool_normalization"],
        learning_e2e=dimensions["learning_e2e"],
        last_tested=last_tested,
        certification_state=certification_state,
        tested_models=tested_models,
        tested_profiles=tested_profiles,
        missing_requirements=missing_requirements,
        evidence_ids=evidence_ids,
    )


@dataclass(frozen=True)
class _ProviderReadiness:
    status: ProviderCertificationStatus
    api_key_env: dict[str, Any] | None
    endpoint_configured: bool
    evidence: tuple[str, ...]
    next_action: str


def _provider_readiness(config: AgentConfig, provider: str) -> _ProviderReadiness:
    if provider == "mock":
        return _ProviderReadiness(
            status=ProviderCertificationStatus.CERTIFIED,
            api_key_env=None,
            endpoint_configured=False,
            evidence=("Deterministic mock provider is available without credentials.",),
            next_action="Provide a current mock test evidence receipt to raise assurance.",
        )
    if provider == "codex-cli":
        configured = shutil.which("codex") is not None
        return _ProviderReadiness(
            status=ProviderCertificationStatus.CONFIGURED
            if configured
            else ProviderCertificationStatus.MANUAL_VALIDATION_REQUIRED,
            api_key_env=None,
            endpoint_configured=False,
            evidence=(
                "Codex CLI provider is local-process backed and must be validated on the target host.",
                "codex executable is present."
                if configured
                else "codex executable was not found on PATH.",
            ),
            next_action="Run the Codex CLI provider smoke on a reviewed workstation.",
        )
    if provider in {"lm-studio", "ollama", "openai-compatible"}:
        endpoint_configured = bool(_base_url_for(config, provider))
        api_key_env = _api_key_env_for(config, provider)
        return _ProviderReadiness(
            status=ProviderCertificationStatus.CONFIGURED
            if endpoint_configured
            else ProviderCertificationStatus.BLOCKED,
            api_key_env=_env_presence(api_key_env),
            endpoint_configured=endpoint_configured,
            evidence=(
                "Provider uses a configured local or OpenAI-compatible endpoint."
                if endpoint_configured
                else "Provider endpoint is missing.",
            ),
            next_action="Run provider integration against the configured endpoint."
            if endpoint_configured
            else "Configure the provider endpoint before running certification.",
        )

    env_name = _api_key_env_for(config, provider)
    env_present = bool(env_name and os.getenv(env_name))
    return _ProviderReadiness(
        status=ProviderCertificationStatus.CONFIGURED
        if env_present
        else ProviderCertificationStatus.BLOCKED,
        api_key_env=_env_presence(env_name),
        endpoint_configured=bool(_base_url_for(config, provider)),
        evidence=(
            f"{provider} provider adapter exists.",
            f"{env_name} is present."
            if env_present
            else f"{env_name or 'API key environment reference'} is missing.",
        ),
        next_action="Run provider integration and learning checks."
        if env_present
        else "Configure the provider API-key environment variable before live validation.",
    )


def _dimensions_for(
    spec: ProviderImplementationSpec,
    receipts: tuple[ProviderCertificationEvidence, ...],
    *,
    now: datetime,
    max_evidence_age: timedelta,
) -> dict[str, ProviderCertificationDimension]:
    return {
        "generate": _dimension_for(
            spec.generate,
            ("generate",),
            receipts,
            now=now,
            max_evidence_age=max_evidence_age,
        ),
        "stream": _dimension_for(
            spec.stream,
            ("stream",),
            receipts,
            now=now,
            max_evidence_age=max_evidence_age,
        ),
        "native_tools": _dimension_for(
            spec.native_tools,
            ("native_tools",),
            receipts,
            now=now,
            max_evidence_age=max_evidence_age,
        ),
        "tool_normalization": _dimension_for(
            spec.tool_normalization,
            ("tool_normalization",),
            receipts,
            now=now,
            max_evidence_age=max_evidence_age,
        ),
        "learning_e2e": _dimension_for(
            spec.learning_e2e,
            ("learning_memory", "learning_memvid", "policy_unchanged"),
            receipts,
            now=now,
            max_evidence_age=max_evidence_age,
        ),
    }


def _dimension_for(
    supported: bool,
    case_names: tuple[str, ...],
    receipts: tuple[ProviderCertificationEvidence, ...],
    *,
    now: datetime,
    max_evidence_age: timedelta,
) -> ProviderCertificationDimension:
    if not supported:
        return ProviderCertificationDimension(status=ProviderEvidenceStatus.NOT_SUPPORTED)
    if not receipts:
        return ProviderCertificationDimension(status=ProviderEvidenceStatus.NOT_RUN)

    ordered = sorted(receipts, key=lambda item: (item.completed_at, item.evidence_id), reverse=True)
    for receipt in ordered:
        evidence_ids = (receipt.evidence_id,)
        if _is_stale(receipt, now=now, max_evidence_age=max_evidence_age):
            return ProviderCertificationDimension(
                status=ProviderEvidenceStatus.STALE,
                evidence_ids=evidence_ids,
            )
        statuses = tuple(receipt.cases[case_name] for case_name in case_names)
        if any(
            status in {ProviderEvidenceStatus.FAIL, ProviderEvidenceStatus.NOT_SUPPORTED}
            for status in statuses
        ):
            return ProviderCertificationDimension(
                status=ProviderEvidenceStatus.FAIL,
                evidence_ids=evidence_ids,
            )
        if any(status == ProviderEvidenceStatus.STALE for status in statuses):
            return ProviderCertificationDimension(
                status=ProviderEvidenceStatus.STALE,
                evidence_ids=evidence_ids,
            )
        if all(status == ProviderEvidenceStatus.PASS for status in statuses):
            return ProviderCertificationDimension(
                status=ProviderEvidenceStatus.PASS,
                evidence_ids=evidence_ids,
            )
        return ProviderCertificationDimension(
            status=ProviderEvidenceStatus.NOT_RUN,
            evidence_ids=evidence_ids,
        )
    return ProviderCertificationDimension(status=ProviderEvidenceStatus.NOT_RUN)


_STATE_RANK: dict[ProviderCertificationState, int] = {
    ProviderCertificationState.IMPLEMENTED: 0,
    ProviderCertificationState.MOCK_TESTED: 1,
    ProviderCertificationState.CREDENTIAL_FREE_INTEGRATION_TESTED: 2,
    ProviderCertificationState.LOCALLY_LIVE_TESTED: 3,
    ProviderCertificationState.RELEASE_CERTIFIED: 4,
    ProviderCertificationState.EXPERIMENTAL: -1,
}


@dataclass(frozen=True)
class _ProviderAssuranceAssessment:
    state: ProviderCertificationState
    evidence: ProviderCertificationEvidence | None


def _certification_assessment(
    spec: ProviderImplementationSpec,
    receipts: tuple[ProviderCertificationEvidence, ...],
    *,
    subject: ProviderCertificationSubject,
    now: datetime,
    max_evidence_age: timedelta,
    authenticated_evidence_ids: Set[str],
) -> _ProviderAssuranceAssessment:
    if spec.experimental:
        return _ProviderAssuranceAssessment(
            state=ProviderCertificationState.EXPERIMENTAL,
            evidence=None,
        )
    candidates = [
        _ProviderAssuranceAssessment(
            state=ProviderCertificationState.IMPLEMENTED,
            evidence=None,
        )
    ]
    for receipt in _newest_receipts_by_level(receipts):
        if not _evidence_level_is_eligible(receipt):
            continue
        if _is_stale(receipt, now=now, max_evidence_age=max_evidence_age):
            continue
        if not _capability_cases_pass(spec, receipt):
            continue
        if _has_newer_negative_receipt(
            spec,
            receipt,
            receipts,
            now=now,
            max_evidence_age=max_evidence_age,
        ):
            continue
        requested = _state_for_evidence_level(receipt.level)
        if receipt.level == ProviderEvidenceLevel.RELEASE:
            if _release_requirements_pass(
                spec,
                receipt,
                subject=subject,
                authenticated_evidence_ids=authenticated_evidence_ids,
            ):
                candidates.append(
                    _ProviderAssuranceAssessment(
                        state=ProviderCertificationState.RELEASE_CERTIFIED,
                        evidence=receipt,
                    )
                )
            continue
        candidates.append(_ProviderAssuranceAssessment(state=requested, evidence=receipt))
    return max(
        candidates,
        key=lambda item: (
            _STATE_RANK[item.state],
            item.evidence.completed_at
            if item.evidence is not None
            else datetime.min.replace(tzinfo=UTC),
            item.evidence.evidence_id if item.evidence is not None else "",
        ),
    )


def _has_newer_negative_receipt(
    spec: ProviderImplementationSpec,
    candidate: ProviderCertificationEvidence,
    receipts: tuple[ProviderCertificationEvidence, ...],
    *,
    now: datetime,
    max_evidence_age: timedelta,
) -> bool:
    candidate_order = (candidate.completed_at, candidate.evidence_id)
    return any(
        receipt.profile == candidate.profile
        and (receipt.completed_at, receipt.evidence_id) > candidate_order
        and _receipt_has_negative_result(
            spec,
            receipt,
            now=now,
            max_evidence_age=max_evidence_age,
        )
        for receipt in receipts
    )


def _receipt_has_negative_result(
    spec: ProviderImplementationSpec,
    receipt: ProviderCertificationEvidence,
    *,
    now: datetime,
    max_evidence_age: timedelta,
) -> bool:
    if _is_stale(receipt, now=now, max_evidence_age=max_evidence_age):
        return True
    if not _capability_cases_pass(spec, receipt):
        return True
    return any(
        receipt.cases[case_name] in {ProviderEvidenceStatus.FAIL, ProviderEvidenceStatus.STALE}
        for case_name in ("learning_memory", "learning_memvid", "policy_unchanged")
    )


def _capability_cases_pass(
    spec: ProviderImplementationSpec,
    receipt: ProviderCertificationEvidence,
) -> bool:
    expected = {
        "generate": spec.generate,
        "stream": spec.stream,
        "native_tools": spec.native_tools,
        "tool_normalization": spec.tool_normalization,
    }
    for case_name, supported in expected.items():
        status = receipt.cases[case_name]
        if supported and status != ProviderEvidenceStatus.PASS:
            return False
        if not supported and status != ProviderEvidenceStatus.NOT_SUPPORTED:
            return False
    return True


def _release_requirements_pass(
    spec: ProviderImplementationSpec,
    receipt: ProviderCertificationEvidence,
    *,
    subject: ProviderCertificationSubject,
    authenticated_evidence_ids: Set[str],
) -> bool:
    if subject.commit == "unknown" or subject.tree_digest == "unknown":
        return False
    if receipt.profile != "release":
        return False
    if receipt.runner.kind != "release_ci":
        return False
    if not receipt.runner.trusted or receipt.evidence_id not in authenticated_evidence_ids:
        return False
    if not _capability_cases_pass(spec, receipt):
        return False
    return all(
        receipt.cases[case_name] == ProviderEvidenceStatus.PASS
        for case_name in ("learning_memory", "learning_memvid", "policy_unchanged")
    )


def _missing_requirements(
    spec: ProviderImplementationSpec,
    provider_receipts: tuple[ProviderCertificationEvidence, ...],
    scoped_receipts: tuple[ProviderCertificationEvidence, ...],
    *,
    expected_model: str,
    expected_digest: str,
    subject: ProviderCertificationSubject,
    now: datetime,
    max_evidence_age: timedelta,
    certification_state: ProviderCertificationState,
    assessment_evidence: ProviderCertificationEvidence | None,
    authenticated_evidence_ids: Set[str],
) -> tuple[str, ...]:
    if spec.experimental:
        return ("provider_is_experimental",)
    missing: set[str] = set()
    if not provider_receipts:
        missing.add("evidence_required")
    else:
        if not any(receipt.model == expected_model for receipt in provider_receipts):
            missing.add("model_scoped_evidence_required")
        if not any(receipt.config_digest == expected_digest for receipt in provider_receipts):
            missing.add("config_scoped_evidence_required")
        if not any(receipt.subject == subject for receipt in provider_receipts):
            missing.add("current_subject_evidence_required")
    if not scoped_receipts:
        missing.add("matching_evidence_required")
        return tuple(sorted(missing))

    if certification_state == ProviderCertificationState.RELEASE_CERTIFIED:
        return ()

    effective_receipts = (
        (assessment_evidence,)
        if assessment_evidence is not None
        else _newest_receipts_by_level(scoped_receipts)
    )
    current_receipts = tuple(
        receipt
        for receipt in effective_receipts
        if not _is_stale(receipt, now=now, max_evidence_age=max_evidence_age)
    )
    if not current_receipts:
        missing.add("current_evidence_required")
    for receipt in effective_receipts:
        if _is_stale(receipt, now=now, max_evidence_age=max_evidence_age):
            missing.add("stale_evidence")
        if receipt.profile not in _EVIDENCE_LEVEL_PROFILES[receipt.level]:
            missing.add("profile_not_eligible_for_level")
        if receipt.runner.kind not in _EVIDENCE_LEVEL_RUNNERS[receipt.level]:
            missing.add("runner_kind_not_eligible_for_level")
        for case_name, supported in {
            "generate": spec.generate,
            "stream": spec.stream,
            "native_tools": spec.native_tools,
            "tool_normalization": spec.tool_normalization,
        }.items():
            status = receipt.cases[case_name]
            if supported and status != ProviderEvidenceStatus.PASS:
                missing.add(f"{case_name}_pass_required")
            if not supported and status != ProviderEvidenceStatus.NOT_SUPPORTED:
                missing.add(f"{case_name}_must_be_not_supported")

    release_claims = _newest_receipts_by_level(
        tuple(
            receipt for receipt in scoped_receipts if receipt.level == ProviderEvidenceLevel.RELEASE
        )
    )
    if release_claims:
        for receipt in release_claims:
            if _is_stale(receipt, now=now, max_evidence_age=max_evidence_age):
                missing.add("current_release_evidence_required")
            if receipt.profile != "release":
                missing.add("release_profile_required")
            if receipt.runner.kind != "release_ci":
                missing.add("release_ci_runner_required")
            if not receipt.runner.trusted:
                missing.add("trusted_runner_claim_required")
            if receipt.evidence_id not in authenticated_evidence_ids:
                missing.add("authenticated_evidence_required")
            for case_name, supported in {
                "generate": spec.generate,
                "stream": spec.stream,
                "native_tools": spec.native_tools,
                "tool_normalization": spec.tool_normalization,
            }.items():
                status = receipt.cases[case_name]
                if supported and status != ProviderEvidenceStatus.PASS:
                    missing.add(f"{case_name}_pass_required")
                if not supported and status != ProviderEvidenceStatus.NOT_SUPPORTED:
                    missing.add(f"{case_name}_must_be_not_supported")
            for case_name in ("learning_memory", "learning_memvid", "policy_unchanged"):
                if receipt.cases[case_name] != ProviderEvidenceStatus.PASS:
                    missing.add(f"{case_name}_pass_required")
    return tuple(sorted(missing))


def _is_stale(
    receipt: ProviderCertificationEvidence,
    *,
    now: datetime,
    max_evidence_age: timedelta,
) -> bool:
    if receipt.completed_at > now:
        return True
    return now - receipt.completed_at > max_evidence_age


def _newest_receipts_by_level(
    receipts: tuple[ProviderCertificationEvidence, ...],
) -> tuple[ProviderCertificationEvidence, ...]:
    newest: dict[tuple[str, ProviderEvidenceLevel], ProviderCertificationEvidence] = {}
    for receipt in receipts:
        key = (receipt.profile, receipt.level)
        current = newest.get(key)
        if current is None or (receipt.completed_at, receipt.evidence_id) > (
            current.completed_at,
            current.evidence_id,
        ):
            newest[key] = receipt
    return tuple(newest[key] for key in sorted(newest, key=lambda item: (item[0], item[1].value)))


def _latest_receipt(
    receipts: tuple[ProviderCertificationEvidence, ...],
) -> tuple[ProviderCertificationEvidence, ...]:
    if not receipts:
        return ()
    return (max(receipts, key=lambda item: (item.completed_at, item.evidence_id)),)


def _reportable_receipts(
    receipts: tuple[ProviderCertificationEvidence, ...],
    *,
    authenticated_evidence_ids: Set[str],
) -> tuple[ProviderCertificationEvidence, ...]:
    return tuple(
        receipt
        for receipt in receipts
        if _evidence_level_is_eligible(receipt)
        and (
            receipt.level != ProviderEvidenceLevel.RELEASE
            or (receipt.runner.trusted and receipt.evidence_id in authenticated_evidence_ids)
        )
    )


def _validate_registry() -> None:
    registry_names = set(PROVIDER_IMPLEMENTATION_REGISTRY)
    provider_names = set(PROVIDER_OPTIONS)
    if registry_names != provider_names:
        missing = sorted(provider_names - registry_names)
        extra = sorted(registry_names - provider_names)
        raise RuntimeError(
            f"provider implementation registry mismatch: missing={missing}, extra={extra}"
        )
    for provider, spec in PROVIDER_IMPLEMENTATION_REGISTRY.items():
        if spec.provider != provider:
            raise RuntimeError(f"provider implementation registry key mismatch: {provider}")


def _reject_duplicate_evidence_ids(
    receipts: tuple[ProviderCertificationEvidence, ...],
) -> None:
    seen: set[str] = set()
    duplicates: set[str] = set()
    for receipt in receipts:
        if receipt.evidence_id in seen:
            duplicates.add(receipt.evidence_id)
        seen.add(receipt.evidence_id)
    if duplicates:
        raise ValueError(f"duplicate provider certification evidence ids: {sorted(duplicates)}")


def _release_targets(
    receipts: tuple[ProviderCertificationEvidence, ...],
    requested: Iterable[str] | None,
) -> tuple[str, ...]:
    targets = (
        set(requested)
        if requested is not None
        else {
            receipt.provider
            for receipt in receipts
            if receipt.level == ProviderEvidenceLevel.RELEASE
        }
    )
    unsupported = sorted(targets - set(PROVIDER_OPTIONS))
    if unsupported:
        raise ValueError(f"unsupported release certification targets: {unsupported}")
    return tuple(provider for provider in PROVIDER_OPTIONS if provider in targets)


def _state_for_evidence_level(level: ProviderEvidenceLevel) -> ProviderCertificationState:
    return {
        ProviderEvidenceLevel.MOCK: ProviderCertificationState.MOCK_TESTED,
        ProviderEvidenceLevel.CREDENTIAL_FREE: (
            ProviderCertificationState.CREDENTIAL_FREE_INTEGRATION_TESTED
        ),
        ProviderEvidenceLevel.LIVE: ProviderCertificationState.LOCALLY_LIVE_TESTED,
        ProviderEvidenceLevel.RELEASE: ProviderCertificationState.RELEASE_CERTIFIED,
    }[level]


def _evidence_level_is_eligible(receipt: ProviderCertificationEvidence) -> bool:
    return (
        receipt.profile in _EVIDENCE_LEVEL_PROFILES[receipt.level]
        and receipt.runner.kind in _EVIDENCE_LEVEL_RUNNERS[receipt.level]
    )


def _readiness_status(status: ProviderCertificationStatus) -> str:
    if status in {
        ProviderCertificationStatus.CERTIFIED,
        ProviderCertificationStatus.CONFIGURED,
    }:
        return "configured"
    return status.value


def _env_presence(name: str | None) -> dict[str, Any] | None:
    if not name:
        return None
    return {"name": name, "present": bool(os.getenv(name))}


def _model_for(config: AgentConfig, provider: str) -> str:
    if provider == config.provider:
        return config.model
    if provider == config.fallback_provider:
        return config.fallback_model or config.model
    suggestions = STATIC_MODEL_SUGGESTIONS.get(provider, ())
    return suggestions[0] if suggestions else "unspecified"


def _api_key_env_for(config: AgentConfig, provider: str) -> str | None:
    if provider == config.provider:
        return default_api_key_env(provider, config.api_key_env)
    if provider == config.fallback_provider:
        return default_api_key_env(provider, config.fallback_api_key_env)
    return default_api_key_env(provider)


def _base_url_for(config: AgentConfig, provider: str) -> str | None:
    if provider == config.provider and config.base_url:
        return config.base_url
    if provider == config.fallback_provider and config.fallback_base_url:
        return config.fallback_base_url
    return DEFAULT_BASE_URLS.get(provider)


def _live_validation_command(provider: str) -> str:
    if provider == "mock":
        return "python scripts/run_golden_evals.py --backend memory --provider mock"
    return (
        "RUN_PROVIDER_INTEGRATION=1 python -m pytest -q "
        f"'tests/integration/test_provider_live_integration.py::test_live_provider_generate_smoke[{provider}]' "
        f"'tests/integration/test_provider_live_integration.py::test_live_provider_stream_smoke[{provider}]'"
    )


def _required_mapping(payload: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    value = payload.get(key)
    if not isinstance(value, Mapping):
        raise ValueError(f"{key} must be an object")
    return value


def _required_string(payload: Mapping[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"{key} must be a non-empty string")
    return value


def _require_exact_keys(
    payload: Mapping[str, Any],
    expected: set[str],
    context: str,
) -> None:
    actual = set(payload)
    if actual != expected:
        missing = sorted(expected - actual)
        extra = sorted(actual - expected)
        raise ValueError(f"{context} keys are invalid: missing={missing}, extra={extra}")


def _safe_identifier(value: str, field_name: str) -> str:
    if not _SAFE_IDENTIFIER.fullmatch(value) or "://" in value:
        raise ValueError(f"{field_name} contains unsupported characters")
    return value


def _parse_timestamp(value: str, field_name: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{field_name} must be an RFC 3339 timestamp") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{field_name} must include a timezone")
    return parsed.astimezone(UTC)


def _validate_subject(subject: ProviderCertificationSubject, *, allow_unknown: bool) -> None:
    if allow_unknown and subject.commit == "unknown" and subject.tree_digest == "unknown":
        return
    if not _GIT_COMMIT.fullmatch(subject.commit):
        raise ValueError("subject.commit must be a Git commit digest")
    if not _HEX_DIGEST.fullmatch(subject.tree_digest):
        raise ValueError("subject.tree_digest must be a lowercase SHA-256 digest")


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("now must include a timezone")
    return value.astimezone(UTC)


def _format_timestamp(value: datetime) -> str:
    return _as_utc(value).isoformat().replace("+00:00", "Z")
