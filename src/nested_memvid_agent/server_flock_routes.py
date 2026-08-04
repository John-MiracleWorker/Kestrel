"""Owner-facing Flock qualification/activation HTTP routes (Adaptive Flock plan, Task 17).

Thin adapters only: every business rule lives in the qualification preview,
the durable qualification runner, and the activation service.  The mutation
schemas forbid extras, reject raw secrets, and require expected revisions;
activation and revocation are owner-only.  Errors use stable machine codes:
``409`` for conflicts/drift, ``422`` for schema violations, ``400`` for
invalid scope/corpus/cap input, ``403`` for non-owner mutations, and ``404``
for unknown IDs.  No response ever contains raw secret values.
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import asdict
from typing import Annotated, Any, Literal, cast
from uuid import uuid4

from fastapi import Request
from pydantic import BaseModel, ConfigDict, Field

from .routing.activation_service import (
    ActivationBindings,
    ActivationConflict,
    ActivationRequest,
    ActivationService,
)
from .routing.models import RoutePolicy
from .routing.qualification_digest import canonical_digest
from .routing.qualification_ledger import QualificationLedger
from .routing.qualification_models import (
    CorpusItem,
    CorpusManifest,
    MoneyMicros,
    QualificationScope,
    QualificationThresholds,
)
from .routing.qualification_preview import (
    QualificationPreviewDraft,
    QualificationPreviewService,
)
from .routing.qualification_records import (
    ActivationGrant,
    ActivationTransition,
    QualificationRevisionConflict,
    QualificationRun,
    QualificationRunDraft,
)
from .routing.qualification_runner import QualificationRunner
from .security_boundary import redact_text

__all__ = ["FLOCK_OWNER_PRINCIPAL", "register_flock_routes"]

#: Fixed local owner principal; identity headers are never consulted.
FLOCK_OWNER_PRINCIPAL = "owner:local-runtime:v1"

RiskLevel = Literal["low", "medium", "high", "critical"]
EvidenceKind = Literal["synthetic", "real_project"]
PrivacyClass = Literal["local_required", "local_preferred", "approved_cloud", "any"]

_HEX_DIGEST = r"^[0-9a-f]{64}$"
_CURSOR_PATTERN = re.compile(r"[1-9][0-9]{0,18}")
_MAX_EVENT_BATCH = 500
_HEARTBEAT_SECONDS = 15.0
_POLL_SECONDS = 0.2
_USD_TEXT = r"^[0-9]{1,9}(\.[0-9]{1,6})?$"

_DigestField = Annotated[str, Field(pattern=_HEX_DIGEST)]
_UsdField = Annotated[str, Field(pattern=_USD_TEXT)]


# --- request schemas (mutations forbid extras) ----------------------------------------


class CorpusItemRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    item_id: str = Field(min_length=1, max_length=240)
    task_family: str = Field(min_length=1, max_length=240)
    risk: RiskLevel
    capabilities: list[str] = Field(min_length=1, max_length=32)
    task_contract_digest: _DigestField
    acceptance_plan_digest: _DigestField
    evidence_kind: EvidenceKind
    actionable: bool = True
    exclusion_reasons: list[str] = Field(default_factory=list, max_length=32)


class ThresholdsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    min_examples_per_scope: int = Field(default=5, ge=1, strict=True)
    min_examples_per_target: int = Field(default=3, ge=1, strict=True)
    confidence_threshold: float = Field(default=0.70, ge=0.0, le=1.0)
    utility_margin: float = Field(default=0.08, ge=0.0)
    cost_coverage_threshold: float = Field(default=0.80, ge=0.0, le=1.0)
    decay_half_life_days: int = Field(default=30, ge=1, strict=True)
    max_guardrail_violations: int = Field(default=0, ge=0, strict=True)
    replay_runs: int = Field(default=20, ge=1, strict=True)
    replay_successes_required: int = Field(default=20, ge=1, strict=True)


class QualificationPreviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project_id: str = Field(min_length=1, max_length=240)
    task_families: list[str] = Field(min_length=1, max_length=64)
    corpus: list[CorpusItemRequest] = Field(min_length=1, max_length=512)
    policy_id: str = Field(default="balanced", min_length=1, max_length=240)
    policy_revision: int = Field(default=1, ge=1, strict=True)
    maximum_spend_usd: _UsdField = "50.00"
    default_privacy_class: PrivacyClass = "approved_cloud"
    project_authority: dict[str, Any] = Field(default_factory=dict)
    learned_config: dict[str, Any] = Field(default_factory=dict)


class ScopeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project_id: str = Field(min_length=1, max_length=240)
    task_family: str = Field(min_length=1, max_length=240)
    risk: RiskLevel
    capability_key: str = Field(min_length=1, max_length=512)
    policy_id: str = Field(min_length=1, max_length=240)
    policy_revision: int = Field(ge=1, strict=True)
    target_ids: list[str] = Field(min_length=2, max_length=64)
    target_inventory_digest: _DigestField
    price_digest: _DigestField
    learned_config_digest: _DigestField
    project_authority_digest: _DigestField


class QualificationCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scope: ScopeRequest
    corpus: list[CorpusItemRequest] = Field(min_length=1, max_length=512)
    thresholds: ThresholdsRequest | None = None
    target_snapshot: dict[str, Any]
    price_snapshot: dict[str, Any]
    policy_payload: dict[str, Any]
    learned_payload: dict[str, Any]
    project_authority: dict[str, Any]
    build: dict[str, Any] = Field(default_factory=dict)
    maximum_spend_usd: _UsdField = "50.00"
    effective_stop_cap_usd: _UsdField | None = None
    attempt_ceiling_usd: _UsdField = "5.00"


class ExpectedRevisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_revision: int = Field(ge=1, strict=True)


class LowerCapRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    maximum_spend_usd: _UsdField
    expected_revision: int = Field(ge=1, strict=True)


class ActivationBindingsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project_authority: dict[str, Any]
    target_snapshot: dict[str, Any]
    price_snapshot: dict[str, Any]
    policy_payload: dict[str, Any]
    learned_payload: dict[str, Any]


class ActivationPreviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    receipt_id: str = Field(min_length=1, max_length=240)
    scope_digests: list[str] = Field(min_length=1, max_length=64)


class ActivationCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    receipt_id: str = Field(min_length=1, max_length=240)
    scope_digests: list[str] = Field(min_length=1, max_length=64)
    expected_receipt_digest: str = Field(min_length=1, max_length=240)
    expected_run_revision: int = Field(ge=1, strict=True)
    bindings: ActivationBindingsRequest


class RevokeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_revision: int = Field(ge=1, strict=True)
    reason: str = Field(default="owner_revocation", min_length=1, max_length=240)


# --- registration -----------------------------------------------------------------------


def register_flock_routes(
    app: Any,
    *,
    qualification_runner: QualificationRunner,
    activation_service: ActivationService,
    preview_service: QualificationPreviewService,
    ledger: QualificationLedger,
    http_exception: type[Exception],
    streaming_response: Callable[..., Any],
    owner_principal: str = FLOCK_OWNER_PRINCIPAL,
    owner_authorized: Callable[[], bool] | None = None,
) -> None:
    """Register the strict Flock qualification and activation routes."""

    runner = qualification_runner

    def owner_gate() -> bool:
        if owner_authorized is None:
            return False
        return bool(owner_authorized())

    def error(status: int, code: str, **extra: Any) -> Exception:
        factory = cast(Callable[..., Exception], http_exception)
        detail: dict[str, Any] = {"code": code}
        detail.update(extra)
        return factory(status_code=status, detail=detail)

    def require_owner() -> None:
        if not owner_gate():
            raise error(403, "flock_mutation_requires_api_auth")

    def reject_raw_secrets(value: Any, field_name: str) -> None:
        if isinstance(value, str):
            if redact_text(value) != value:
                raise error(400, "flock_raw_secret_rejected", field=field_name)
            return
        if isinstance(value, Mapping):
            for key, item in value.items():
                reject_raw_secrets(key, field_name)
                reject_raw_secrets(item, field_name)
            return
        if isinstance(value, (list, tuple)):
            for item in value:
                reject_raw_secrets(item, field_name)

    def usd_text(money: MoneyMicros) -> str:
        text = money.to_usd_text()
        whole, dot, fraction = text.partition(".")
        if not dot:
            return f"{whole}.00"
        if len(fraction) == 1:
            return f"{whole}.{fraction}0"
        return text

    def parse_money(text: str | None, field_name: str) -> MoneyMicros:
        try:
            return MoneyMicros.from_usd_text("" if text is None else text)
        except ValueError as exc:
            raise error(400, "flock_cap_invalid", field=field_name) from exc

    def corpus_from_request(items: Sequence[CorpusItemRequest]) -> CorpusManifest:
        return CorpusManifest(
            schema_version=1,
            items=tuple(
                CorpusItem(
                    item_id=item.item_id,
                    task_family=item.task_family,
                    risk=item.risk,
                    capabilities=tuple(item.capabilities),
                    task_contract_digest=item.task_contract_digest,
                    acceptance_plan_digest=item.acceptance_plan_digest,
                    evidence_kind=item.evidence_kind,
                    actionable=item.actionable,
                    exclusion_reasons=tuple(item.exclusion_reasons),
                )
                for item in items
            ),
        )

    def thresholds_from_request(request: ThresholdsRequest | None) -> QualificationThresholds:
        if request is None:
            return QualificationThresholds()
        return QualificationThresholds(**request.model_dump())

    def run_payload(run: QualificationRun, *, blockers: Sequence[str] = ()) -> dict[str, Any]:
        return {
            "run_id": run.run_id,
            "status": run.status,
            "revision": run.revision,
            "owner_principal": run.owner_principal,
            "scope_digest": run.scope_digest,
            "corpus_digest": run.corpus_digest,
            "target_digest": run.target_digest,
            "price_digest": run.price_digest,
            "policy_digest": run.policy_digest,
            "learned_digest": run.learned_digest,
            "project_authority_digest": run.project_authority_digest,
            "thresholds_digest": run.thresholds_digest,
            "build_digest": run.build_digest,
            "caps": {
                "max_spend_micros": run.max_spend.micros,
                "max_spend_usd": usd_text(run.max_spend),
                "effective_stop_cap_micros": run.effective_stop_cap.micros,
                "effective_stop_cap_usd": usd_text(run.effective_stop_cap),
                "attempt_ceiling_micros": run.attempt_ceiling.micros,
                "attempt_ceiling_usd": usd_text(run.attempt_ceiling),
            },
            "spend": {
                "actual_spend_micros": run.actual_spend.micros,
                "actual_spend_usd": usd_text(run.actual_spend),
                "unresolved_reserve_micros": run.unresolved_reserve.micros,
                "inflight_reserve_micros": run.inflight_reserve.micros,
            },
            "blockers": list(blockers),
            "created_at": run.created_at,
            "updated_at": run.updated_at,
            "started_at": run.started_at,
            "finished_at": run.finished_at,
            "terminal_reason": run.terminal_reason,
        }

    def grant_payload(grant: ActivationGrant) -> dict[str, Any]:
        return {
            "grant_id": grant.grant_id,
            "run_id": grant.run_id,
            "target_id": grant.target_id,
            "scope": json.loads(grant.scope_json),
            "scope_digest": grant.scope_digest,
            "policy_id": grant.policy_id,
            "policy_revision": grant.policy_revision,
            "qualification_receipt_id": grant.qualification_receipt_id,
            "created_by": grant.created_by,
            "created_at": grant.created_at,
        }

    def transition_payload(transition: ActivationTransition) -> dict[str, Any]:
        return asdict(transition)

    def require_run(run_id: str) -> QualificationRun:
        run = ledger.get_run(run_id)
        if run is None:
            raise error(404, "flock_qualification_not_found")
        return run

    def expect_run_revision(run: QualificationRun, expected_revision: int) -> None:
        if run.revision != expected_revision:
            raise error(
                409,
                "flock_revision_conflict",
                resource="qualification_run",
                resource_id=run.run_id,
                current_revision=run.revision,
            )

    def lifecycle_error(exc: Exception, run_id: str) -> Exception:
        if isinstance(exc, KeyError):
            return error(404, "flock_qualification_not_found")
        if isinstance(exc, QualificationRevisionConflict):
            return error(
                409,
                "flock_revision_conflict",
                resource=exc.resource,
                resource_id=exc.resource_id,
                current_revision=exc.current_revision,
            )
        if isinstance(exc, RuntimeError):
            return error(409, "flock_execution_unavailable")
        return error(409, "flock_run_state_conflict")

    def activation_error(exc: Exception) -> Exception:
        if isinstance(exc, PermissionError):
            return error(403, "flock_activation_requires_owner")
        if isinstance(exc, ActivationConflict):
            return error(409, exc.reason)
        if isinstance(exc, QualificationRevisionConflict):
            return error(
                409,
                "flock_revision_conflict",
                resource=exc.resource,
                resource_id=exc.resource_id,
                current_revision=exc.current_revision,
            )
        message = str(exc)
        if message.startswith("unknown "):
            return error(404, "flock_activation_not_found")
        return error(400, "flock_activation_invalid")

    # -- qualification preview / CRUD -----------------------------------------

    @app.post("/api/flock/qualifications/preview")  # type: ignore[untyped-decorator]
    def preview_qualification(request: QualificationPreviewRequest) -> Any:
        reject_raw_secrets(request.model_dump(), "preview")
        cap = parse_money(request.maximum_spend_usd, "maximum_spend_usd")
        try:
            draft = QualificationPreviewDraft(
                project_authority={
                    **request.project_authority,
                    "project_id": request.project_id,
                },
                task_families=tuple(request.task_families),
                policy=RoutePolicy(policy_id=request.policy_id),
                policy_revision=request.policy_revision,
                corpus=corpus_from_request(request.corpus),
                learned_config=dict(request.learned_config),
                default_privacy_class=request.default_privacy_class,
            )
            preview = preview_service.preview(draft)
        except ValueError as exc:
            raise error(400, "flock_preview_invalid") from exc
        payload = preview.to_payload()
        payload["budget"] = {
            "maximum_spend_micros": cap.micros,
            "maximum_spend_usd": usd_text(cap),
            "estimated_reserved_cost_range_micros": [
                preview.estimated_reserved_cost_range[0].micros,
                preview.estimated_reserved_cost_range[1].micros,
            ],
        }
        payload["preview_digest"] = preview.digest
        return payload

    @app.post("/api/flock/qualifications", status_code=201)  # type: ignore[untyped-decorator]
    def create_qualification(request: QualificationCreateRequest) -> Any:
        require_owner()
        reject_raw_secrets(request.model_dump(), "qualification")
        max_spend = parse_money(request.maximum_spend_usd, "maximum_spend_usd")
        stop_cap = (
            max_spend
            if request.effective_stop_cap_usd is None
            else parse_money(request.effective_stop_cap_usd, "effective_stop_cap_usd")
        )
        ceiling = parse_money(request.attempt_ceiling_usd, "attempt_ceiling_usd")
        try:
            scope_request = request.scope
            scope = QualificationScope(
                project_id=scope_request.project_id,
                task_family=scope_request.task_family,
                risk=scope_request.risk,
                capabilities=tuple(
                    part for part in scope_request.capability_key.split("+") if part
                ),
                policy_id=scope_request.policy_id,
                policy_revision=scope_request.policy_revision,
                target_ids=tuple(scope_request.target_ids),
                target_inventory_digest=scope_request.target_inventory_digest,
                price_digest=scope_request.price_digest,
                learned_config_digest=scope_request.learned_config_digest,
                project_authority_digest=scope_request.project_authority_digest,
            )
            draft = QualificationRunDraft(
                run_id=f"qual_{uuid4().hex[:24]}",
                owner_principal=owner_principal,
                scope=scope,
                corpus=corpus_from_request(request.corpus),
                thresholds=thresholds_from_request(request.thresholds),
                target_snapshot=dict(request.target_snapshot),
                price_snapshot=dict(request.price_snapshot),
                policy_payload=dict(request.policy_payload),
                learned_payload=dict(request.learned_payload),
                project_authority=dict(request.project_authority),
                build=dict(request.build),
                max_spend=max_spend,
                effective_stop_cap=stop_cap,
                attempt_ceiling=ceiling,
            )
        except ValueError as exc:
            message = str(exc)
            code = (
                "flock_cap_invalid"
                if ("cap" in message or "spend" in message or "ceiling" in message)
                else "flock_run_invalid"
            )
            raise error(400, code) from exc
        run = runner.create(draft)
        return run_payload(run)

    @app.get("/api/flock/qualifications")  # type: ignore[untyped-decorator]
    def list_qualifications() -> Any:
        return {"runs": [run_payload(run) for run in ledger.list_runs()]}

    @app.get("/api/flock/qualifications/{run_id}")  # type: ignore[untyped-decorator]
    def get_qualification(run_id: str) -> Any:
        try:
            view = runner.get(run_id)
        except KeyError as exc:
            raise error(404, "flock_qualification_not_found") from exc
        return run_payload(view.run, blockers=view.blockers)

    # -- qualification lifecycle ------------------------------------------------

    @app.post("/api/flock/qualifications/{run_id}/start")  # type: ignore[untyped-decorator]
    def start_qualification(run_id: str, request: ExpectedRevisionRequest) -> Any:
        require_owner()
        expect_run_revision(require_run(run_id), request.expected_revision)
        try:
            view = runner.start(run_id)
        except (KeyError, RuntimeError, ValueError) as exc:
            raise lifecycle_error(exc, run_id) from exc
        return run_payload(view.run, blockers=view.blockers)

    @app.post("/api/flock/qualifications/{run_id}/pause")  # type: ignore[untyped-decorator]
    def pause_qualification(run_id: str, request: ExpectedRevisionRequest) -> Any:
        require_owner()
        expect_run_revision(require_run(run_id), request.expected_revision)
        try:
            view = runner.pause(run_id)
        except (KeyError, RuntimeError, ValueError) as exc:
            raise lifecycle_error(exc, run_id) from exc
        return run_payload(view.run, blockers=view.blockers)

    @app.post("/api/flock/qualifications/{run_id}/resume")  # type: ignore[untyped-decorator]
    def resume_qualification(run_id: str, request: ExpectedRevisionRequest) -> Any:
        require_owner()
        expect_run_revision(require_run(run_id), request.expected_revision)
        try:
            view = runner.resume(run_id)
        except (KeyError, RuntimeError, ValueError) as exc:
            raise lifecycle_error(exc, run_id) from exc
        return run_payload(view.run, blockers=view.blockers)

    @app.post("/api/flock/qualifications/{run_id}/cancel")  # type: ignore[untyped-decorator]
    def cancel_qualification(run_id: str, request: ExpectedRevisionRequest) -> Any:
        require_owner()
        expect_run_revision(require_run(run_id), request.expected_revision)
        try:
            view = runner.cancel(run_id)
        except (KeyError, RuntimeError, ValueError) as exc:
            raise lifecycle_error(exc, run_id) from exc
        return run_payload(view.run, blockers=view.blockers)

    @app.post("/api/flock/qualifications/{run_id}/lower-cap")  # type: ignore[untyped-decorator]
    def lower_qualification_cap(run_id: str, request: LowerCapRequest) -> Any:
        require_owner()
        reject_raw_secrets(request.model_dump(), "lower-cap")
        run = require_run(run_id)
        expect_run_revision(run, request.expected_revision)
        new_cap = parse_money(request.maximum_spend_usd, "maximum_spend_usd")
        try:
            view = runner.lower_cap(run_id, new_cap)
        except KeyError as exc:
            raise error(404, "flock_qualification_not_found") from exc
        except QualificationRevisionConflict as exc:
            raise error(
                409,
                "flock_revision_conflict",
                resource=exc.resource,
                resource_id=exc.resource_id,
                current_revision=exc.current_revision,
            ) from exc
        except ValueError as exc:
            if "cannot be raised" in str(exc):
                raise error(
                    409,
                    "qualification_cap_cannot_increase",
                    run_id=run_id,
                    current_revision=run.revision,
                ) from exc
            raise error(409, "flock_run_state_conflict") from exc
        return run_payload(view.run, blockers=view.blockers)

    # -- receipt and durable events --------------------------------------------

    @app.get("/api/flock/qualifications/{run_id}/receipt")  # type: ignore[untyped-decorator]
    def qualification_receipt(run_id: str) -> Any:
        require_run(run_id)
        terminal = [
            receipt
            for receipt in ledger.list_receipts(run_id)
            if receipt.receipt_type == "run_terminal"
        ]
        if not terminal:
            raise error(404, "flock_receipt_not_found")
        receipt = terminal[-1]
        return {
            "receipt_id": receipt.receipt_id,
            "run_id": receipt.run_id,
            "receipt_type": receipt.receipt_type,
            "payload_digest": receipt.payload_digest,
            "payload": dict(receipt.payload),
            "created_at": receipt.created_at,
        }

    @app.get("/api/flock/qualifications/{run_id}/events")  # type: ignore[untyped-decorator]
    def qualification_events(run_id: str, request: Request) -> Any:
        cursor_values = request.headers.getlist("last-event-id")
        if len(cursor_values) > 1:
            raise error(400, "flock_event_cursor_invalid")
        cursor = 0
        if cursor_values:
            value = cursor_values[0].strip()
            if _CURSOR_PATTERN.fullmatch(value) is None:
                raise error(400, "flock_event_cursor_invalid")
            cursor = int(value)
        require_run(run_id)

        def stream() -> Iterator[str]:
            after = cursor
            last_heartbeat = time.monotonic()
            while True:
                batch = [
                    event
                    for event in ledger.list_events(run_id)
                    if event.sequence > after
                ][:_MAX_EVENT_BATCH]
                for event in batch:
                    after = event.sequence
                    data = json.dumps(
                        {
                            "sequence": event.sequence,
                            "event_type": event.event_type,
                            "payload": dict(event.payload),
                            "created_at": event.created_at,
                        },
                        sort_keys=True,
                    )
                    yield f"id: {event.sequence}\nevent: {event.event_type}\ndata: {data}\n\n"
                current = ledger.get_run(run_id)
                if current is None:
                    return
                if current.is_terminal and not batch:
                    # Terminal runs replay their persisted events and close; a
                    # renderer disconnect never changes run state.
                    return
                if not batch:
                    now = time.monotonic()
                    if now - last_heartbeat >= _HEARTBEAT_SECONDS:
                        yield ": heartbeat\n\n"
                        last_heartbeat = now
                    time.sleep(_POLL_SECONDS)

        return streaming_response(
            stream(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-store, no-transform",
                "X-Accel-Buffering": "no",
            },
        )

    # -- activations -------------------------------------------------------------

    @app.post("/api/flock/activations/preview")  # type: ignore[untyped-decorator]
    def preview_activation(request: ActivationPreviewRequest) -> Any:
        reject_raw_secrets(request.model_dump(), "activation_preview")
        try:
            preview = activation_service.preview_activation(
                request.receipt_id,
                tuple(request.scope_digests),
            )
        except ActivationConflict as exc:
            raise error(409, exc.reason) from exc
        except ValueError as exc:
            message = str(exc)
            if message.startswith("unknown receipt scope"):
                raise error(409, "scope_not_in_receipt") from exc
            if message.startswith("unknown "):
                raise error(404, "flock_activation_not_found") from exc
            raise error(400, "flock_activation_invalid") from exc
        return asdict(preview)

    @app.post("/api/flock/activations", status_code=201)  # type: ignore[untyped-decorator]
    def create_activation(request: ActivationCreateRequest) -> Any:
        require_owner()
        reject_raw_secrets(request.model_dump(), "activation")
        bindings = ActivationBindings(
            project_authority=dict(request.bindings.project_authority),
            target_snapshot=dict(request.bindings.target_snapshot),
            price_snapshot=dict(request.bindings.price_snapshot),
            policy_payload=dict(request.bindings.policy_payload),
            learned_payload=dict(request.bindings.learned_payload),
        )
        try:
            result = activation_service.activate_scopes(
                ActivationRequest(
                    receipt_id=request.receipt_id,
                    scope_digests=tuple(request.scope_digests),
                    principal=owner_principal,
                    expected_receipt_digest=request.expected_receipt_digest,
                    expected_run_revision=request.expected_run_revision,
                    bindings=bindings,
                )
            )
        except (PermissionError, ActivationConflict, QualificationRevisionConflict) as exc:
            raise activation_error(exc) from exc
        except ValueError as exc:
            raise activation_error(exc) from exc
        return {
            "grants": [grant_payload(grant) for grant in result.grants],
            "transitions": [transition_payload(item) for item in result.transitions],
            "superseded": [transition_payload(item) for item in result.superseded],
        }

    @app.get("/api/flock/activations")  # type: ignore[untyped-decorator]
    def list_activations(receipt_id: str | None = None) -> Any:
        grants = activation_service.list_grants(receipt_id=receipt_id)
        return {"grants": [grant_payload(grant) for grant in grants]}

    @app.get("/api/flock/activations/{grant_id}/evaluate")  # type: ignore[untyped-decorator]
    def evaluate_activation(grant_id: str) -> Any:
        grant = ledger.get_grant(grant_id)
        if grant is None:
            raise error(404, "flock_grant_not_found")
        transitions = ledger.list_transitions(grant_id)
        latest = transitions[-1] if transitions else None
        status = "inactive"
        if latest is not None:
            status = {
                "activated": "active",
                "resumed": "active",
                "suspended": "suspended",
                "revoked": "revoked",
            }.get(latest.transition_type, "inactive")
        try:
            envelope = ledger.receipt_envelope(grant.qualification_receipt_id)
            receipt_authenticates = bool(ledger.verify_receipt_envelope(envelope))
        except Exception:  # noqa: BLE001 - verification fails closed
            receipt_authenticates = False
        reasons = {
            "target_inventory": "target_inventory_changed",
            "price": "price_snapshot_changed",
            "policy": "routing_policy_changed",
            "learned": "learned_configuration_changed",
            "project_authority": "project_authority_changed",
        }
        binding_changes: dict[str, bool] = {}
        run = ledger.get_run(grant.run_id)
        if run is not None:
            stored = (
                ("target_inventory", run.target_json, run.target_digest),
                ("price", run.price_json, run.price_digest),
                ("policy", run.policy_json, run.policy_digest),
                ("learned", run.learned_json, run.learned_digest),
                (
                    "project_authority",
                    run.project_authority_json,
                    run.project_authority_digest,
                ),
            )
            for key, raw, digest in stored:
                try:
                    changed = canonical_digest(json.loads(raw)) != digest
                except (ValueError, TypeError):
                    changed = True
                binding_changes[key] = changed
        reason_codes: list[str] = []
        if not receipt_authenticates:
            reason_codes.append("receipt_authentication_failed")
        for key, changed in binding_changes.items():
            if changed:
                reason_codes.append(reasons[key])
        if status == "suspended":
            reason_codes.append("grant_suspended")
        if status == "revoked":
            reason_codes.append("grant_revoked")
        effective = (
            status == "active" and receipt_authenticates and not any(binding_changes.values())
        )
        return {
            "grant_id": grant.grant_id,
            "run_id": grant.run_id,
            "scope_digest": grant.scope_digest,
            "status": status,
            "effective": effective,
            "reason_codes": reason_codes,
            "receipt_authenticates": receipt_authenticates,
            "binding_changes": binding_changes,
            "latest_transition": transition_payload(latest) if latest is not None else None,
            "transition_count": len(transitions),
        }

    @app.post("/api/flock/activations/{grant_id}/revoke")  # type: ignore[untyped-decorator]
    def revoke_activation(grant_id: str, request: RevokeRequest) -> Any:
        require_owner()
        reject_raw_secrets(request.model_dump(), "revoke")
        grant = ledger.get_grant(grant_id)
        if grant is None:
            raise error(404, "flock_grant_not_found")
        if grant.created_by != owner_principal:
            raise error(403, "flock_activation_requires_owner")
        try:
            transition = activation_service.revoke(
                grant_id,
                expected_revision=request.expected_revision,
                reason=request.reason,
            )
        except QualificationRevisionConflict as exc:
            raise error(
                409,
                "flock_revision_conflict",
                resource=exc.resource,
                resource_id=exc.resource_id,
                current_revision=exc.current_revision,
            ) from exc
        except ValueError as exc:
            message = str(exc)
            if "already revoked" in message:
                raise error(409, "flock_grant_already_revoked", grant_id=grant_id) from exc
            if message.startswith("unknown "):
                raise error(404, "flock_grant_not_found") from exc
            raise error(400, "flock_activation_invalid") from exc
        return transition_payload(transition)
