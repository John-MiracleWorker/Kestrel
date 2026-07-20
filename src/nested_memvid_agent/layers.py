from __future__ import annotations

import hmac
import json
import os
import re
import secrets
import stat
import time
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from difflib import SequenceMatcher
from hashlib import sha256
from pathlib import Path

from .backends.base import MemoryBackend, MemorySearchPage
from .context_frames import MV2ContextFrame, make_conflict_set_frame, to_memory_record
from .file_lock import lock_exclusive, unlock
from .models import EvidenceRef, MemoryHit, MemoryKind, MemoryLayer, MemoryRecord, RetrievalQuery
from .private_artifacts import (
    ensure_owner_only_directory,
    ensure_private_directory,
    harden_memory_artifact_files,
    harden_private_sqlite_files,
    open_private_file_descriptor,
    read_private_text,
    write_private_text_exclusive,
)
from .promotion_ledger import PromotionEntry, PromotionLedger, make_outcome
from .security_boundary import sanitize_memory_record
from .vector_sidecar import TextEmbedder, VectorSidecar, VectorSidecarStatus, make_local_embedder

_RETRIEVAL_CANDIDATE_PAGE_SIZE = 64
_RETRIEVAL_MAX_CANDIDATES_PER_LAYER = 4_096
_STABLE_LAYERS = frozenset(
    {
        MemoryLayer.SEMANTIC,
        MemoryLayer.PROCEDURAL,
        MemoryLayer.SELF,
        MemoryLayer.POLICY,
    }
)
_STABLE_WRITE_ENVELOPE_VERSION = 1
_MEMORY_INTEGRITY_KEY_FILE = ".validation-integrity.key"
_MEMORY_INTEGRITY_KEY_LOCK_FILE = ".validation-integrity.lock"
_MEMORY_INTEGRITY_KEY_TEMP_FILE = ".validation-integrity.key.tmp"
_MEMORY_INTEGRITY_KEY_BYTES = 32
_RUNTIME_VALIDATION_RECEIPT_SCHEMA = "kestrel.runtime_validation_receipt.v1"
_VALIDATION_EVIDENCE_BUCKETS = frozenset({"test", "lint", "repair", "review", "task", "human"})
_STABLE_WRITE_AUTHORITIES = frozenset(
    {"nested_learning", "lesson_resolution", "approved_correction"}
)


@dataclass(frozen=True)
class LayerSpec:
    layer: MemoryLayer
    description: str
    mv2_file: str
    update_cadence: str
    retrieval_k: int
    context_budget_chars: int
    min_write_confidence: float
    promotion_threshold: float
    min_repeat_count_for_promotion: int
    retention_days: int
    provisional_threshold: float | None = None
    search_mode: str = "auto"
    vector_search_enabled: bool = False
    vector_embedding_provider: str | None = None
    vector_embedding_model: str | None = None
    vector_index_path: str | None = None
    hybrid_search_enabled: bool = False


DEFAULT_LAYER_SPECS: dict[MemoryLayer, LayerSpec] = {
    MemoryLayer.WORKING: LayerSpec(
        layer=MemoryLayer.WORKING,
        description="Current task state, volatile scratch memory, active assumptions.",
        mv2_file="working.mv2",
        update_cadence="every_step",
        retrieval_k=6,
        context_budget_chars=3500,
        min_write_confidence=0.2,
        promotion_threshold=0.65,
        provisional_threshold=None,
        min_repeat_count_for_promotion=1,
        retention_days=2,
        search_mode="lex",
    ),
    MemoryLayer.EPISODIC: LayerSpec(
        layer=MemoryLayer.EPISODIC,
        description="Meaningful events, tool results, failures, decisions, session summaries.",
        mv2_file="episodic.mv2",
        update_cadence="event_or_session",
        retrieval_k=8,
        context_budget_chars=4500,
        min_write_confidence=0.5,
        promotion_threshold=0.65,
        provisional_threshold=None,
        min_repeat_count_for_promotion=1,
        retention_days=90,
        search_mode="auto",
    ),
    MemoryLayer.SEMANTIC: LayerSpec(
        layer=MemoryLayer.SEMANTIC,
        description="Stable facts about codebases, APIs, users, projects, and domains.",
        mv2_file="semantic.mv2",
        update_cadence="validated_fact",
        retrieval_k=8,
        context_budget_chars=4500,
        min_write_confidence=0.75,
        promotion_threshold=0.78,
        provisional_threshold=None,
        min_repeat_count_for_promotion=1,
        retention_days=365,
        search_mode="auto",
    ),
    MemoryLayer.PROCEDURAL: LayerSpec(
        layer=MemoryLayer.PROCEDURAL,
        description="Reusable methods, debug recipes, known workflows, tool-use skills.",
        mv2_file="procedural.mv2",
        update_cadence="validated_repeated_success",
        retrieval_k=6,
        context_budget_chars=4000,
        min_write_confidence=0.82,
        promotion_threshold=0.78,
        provisional_threshold=None,
        min_repeat_count_for_promotion=2,
        retention_days=365,
        search_mode="auto",
    ),
    MemoryLayer.SELF: LayerSpec(
        layer=MemoryLayer.SELF,
        description="Validated self-model, identity, capability snapshots, and user-specific workflow preferences.",
        mv2_file="self.mv2",
        update_cadence="validated_self_reflection",
        retrieval_k=6,
        context_budget_chars=3500,
        min_write_confidence=0.78,
        promotion_threshold=0.78,
        provisional_threshold=None,
        min_repeat_count_for_promotion=1,
        retention_days=365,
        search_mode="auto",
    ),
    MemoryLayer.POLICY: LayerSpec(
        layer=MemoryLayer.POLICY,
        description="Slow-changing behavior rules and safety constraints. Write rarely.",
        mv2_file="policy.mv2",
        update_cadence="rare_manual_or_high_confidence",
        retrieval_k=5,
        context_budget_chars=3000,
        min_write_confidence=0.95,
        promotion_threshold=0.97,
        provisional_threshold=None,
        min_repeat_count_for_promotion=5,
        retention_days=730,
        search_mode="lex",
    ),
}


def prepare_private_memory_artifacts(
    memory_dir: Path,
    *,
    specs: dict[MemoryLayer, LayerSpec] | None = None,
    harden_existing: bool = True,
) -> None:
    """Validate memory paths and optionally harden the finite existing artifact set.

    Backend construction during concurrent runs leaves hardening to each backend
    after it has acquired that layer's lifecycle lock. Startup preflight may set
    ``harden_existing`` to repair legacy permissions before any backend opens.
    """

    layer_specs = specs or DEFAULT_LAYER_SPECS
    validate_layer_artifact_paths(layer_specs)
    ensure_private_directory(memory_dir)
    if not harden_existing:
        return
    for spec in layer_specs.values():
        harden_memory_artifact_files(memory_dir / spec.mv2_file)
        index_path = _configured_vector_index_path(memory_dir=memory_dir, spec=spec)
        if index_path is not None:
            ensure_private_directory(index_path.parent)
            harden_private_sqlite_files(index_path)


def prepare_private_runs_root(runs_dir: Path) -> None:
    """Protect an explicit full-runtime capsule root without scanning its children."""

    ensure_owner_only_directory(runs_dir)


def validate_layer_artifact_paths(specs: dict[MemoryLayer, LayerSpec]) -> None:
    """Keep every configured memory artifact unique and directly inside memory_dir."""

    mv2_names: set[str] = set()
    reserved_names: set[str] = {
        _artifact_collision_key(name)
        for name in {
            _MEMORY_INTEGRITY_KEY_FILE,
            _MEMORY_INTEGRITY_KEY_LOCK_FILE,
            _MEMORY_INTEGRITY_KEY_TEMP_FILE,
        }
    }
    for layer, spec in specs.items():
        if spec.layer != layer:
            raise ValueError(f"Layer spec key does not match declared layer: {layer.value}")
        name = _validated_artifact_name(
            spec.mv2_file,
            label=f"{layer.value} mv2_file",
            required_suffix=".mv2",
        )
        collision_key = _artifact_collision_key(name)
        if collision_key in mv2_names:
            raise ValueError(f"Duplicate memory layer filename: {name}")
        mv2_names.add(collision_key)
        path = Path(name)
        reserved_names.update(
            _artifact_collision_key(candidate)
            for candidate in {
                name,
                path.with_suffix(".memory.json").name,
                path.with_suffix(f"{path.suffix}.records.json").name,
                f".{name}.kestrel.lock",
            }
        )

    vector_artifact_names: set[str] = set()
    for layer, spec in specs.items():
        if not spec.vector_search_enabled or not spec.vector_index_path:
            continue
        name = _validated_artifact_name(
            spec.vector_index_path,
            label=f"{layer.value} vector index_path",
        )
        names = {name, f"{name}-wal", f"{name}-shm", f"{name}-journal"}
        collision_keys = {_artifact_collision_key(candidate) for candidate in names}
        conflicts = collision_keys & (reserved_names | vector_artifact_names)
        if conflicts:
            raise ValueError(f"Duplicate or conflicting memory artifact filename: {name}")
        vector_artifact_names.update(collision_keys)


def _validated_artifact_name(
    value: str,
    *,
    label: str,
    required_suffix: str | None = None,
) -> str:
    path = Path(value)
    if (
        not value
        or path.is_absolute()
        or len(path.parts) != 1
        or path.name != value
        or "/" in value
        or "\\" in value
        or value in {".", ".."}
        or (required_suffix is not None and path.suffix != required_suffix)
    ):
        suffix_note = f" ending in {required_suffix}" if required_suffix else ""
        raise ValueError(f"{label} must be a single filename{suffix_note} inside memory_dir")
    return value


def _artifact_collision_key(value: str) -> str:
    return unicodedata.normalize("NFC", unicodedata.normalize("NFC", value).casefold())


def _load_or_create_memory_integrity_key(memory_dir: Path) -> bytes:
    ensure_private_directory(memory_dir)
    descriptor = open_private_file_descriptor(
        memory_dir / _MEMORY_INTEGRITY_KEY_LOCK_FILE
    )
    try:
        with os.fdopen(descriptor, "r+", encoding="utf-8") as handle:
            descriptor = -1
            lock_exclusive(handle)
            try:
                _recover_memory_integrity_key_temp(memory_dir)
                return _load_or_create_memory_integrity_key_locked(memory_dir)
            finally:
                unlock(handle)
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _load_or_create_memory_integrity_key_locked(memory_dir: Path) -> bytes:
    path = memory_dir / _MEMORY_INTEGRITY_KEY_FILE
    encoded = read_private_text(path, missing_ok=True)
    if encoded is None:
        candidate = secrets.token_bytes(_MEMORY_INTEGRITY_KEY_BYTES)
        try:
            write_private_text_exclusive(path, candidate.hex())
            encoded = candidate.hex()
        except FileExistsError:
            encoded = read_private_text(path)
    if encoded is None:
        raise ValueError("Memory integrity key could not be loaded.")
    try:
        key = bytes.fromhex(encoded.strip())
    except ValueError as exc:
        raise ValueError("Memory integrity key has invalid encoding.") from exc
    if len(key) != _MEMORY_INTEGRITY_KEY_BYTES:
        raise ValueError("Memory integrity key has an invalid size.")
    return key


def _recover_memory_integrity_key_temp(memory_dir: Path) -> None:
    temporary = memory_dir / _MEMORY_INTEGRITY_KEY_TEMP_FILE
    final = memory_dir / _MEMORY_INTEGRITY_KEY_FILE
    try:
        temp_metadata = os.lstat(temporary)
    except FileNotFoundError:
        return
    try:
        final_metadata = os.lstat(final)
    except FileNotFoundError:
        final_metadata = None
    same_inode = final_metadata is not None and os.path.samestat(
        temp_metadata,
        final_metadata,
    )
    _validate_memory_integrity_temp(
        temp_metadata,
        expected_links=2 if same_inode else 1,
    )
    if same_inode:
        if final_metadata is None:
            raise RuntimeError("Published memory integrity key metadata disappeared.")
        _validate_memory_integrity_temp(final_metadata, expected_links=2)
        if temp_metadata.st_size != _MEMORY_INTEGRITY_KEY_BYTES * 2:
            raise ValueError("Published memory integrity key has an invalid size.")
    temporary.unlink()
    _fsync_memory_integrity_directory(memory_dir)


def _validate_memory_integrity_temp(
    metadata: os.stat_result,
    *,
    expected_links: int,
) -> None:
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != expected_links:
        raise ValueError("Temporary memory integrity key has unsafe link metadata.")
    geteuid = getattr(os, "geteuid", None)
    if callable(geteuid) and metadata.st_uid != geteuid():
        raise PermissionError("Temporary memory integrity key has an unsafe owner.")
    if os.name != "nt" and stat.S_IMODE(metadata.st_mode) & 0o077:
        raise PermissionError("Temporary memory integrity key is not owner-only.")


def _fsync_memory_integrity_directory(memory_dir: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(
        memory_dir,
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _validation_receipt_signature(payload: dict[str, object], *, key: bytes) -> str:
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")
    return hmac.new(key, encoded, sha256).hexdigest()


def _stable_claim_digest(record: MemoryRecord) -> str:
    payload = {
        "content": record.content,
        "kind": record.kind.value,
        "title": record.title,
    }
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")
    return "sha256:" + sha256(encoded).hexdigest()


class LayeredMemorySystem:
    """Routes reads/writes across nested memory layers."""

    def __init__(
        self,
        backends: dict[MemoryLayer, MemoryBackend],
        specs: dict[MemoryLayer, LayerSpec] | None = None,
        ledger: PromotionLedger | None = None,
        vector_sidecars: dict[MemoryLayer, VectorSidecar] | None = None,
        enforce_stable_write_integrity: bool = True,
        integrity_key: bytes | None = None,
    ) -> None:
        self.specs = specs or DEFAULT_LAYER_SPECS
        missing = set(self.specs) - set(backends)
        if missing:
            missing_names = ", ".join(layer.value for layer in sorted(missing, key=str))
            raise ValueError(f"Missing backends for layers: {missing_names}")
        self.backends = backends
        self.ledger = ledger
        self.vector_sidecars = vector_sidecars or {}
        self.enforce_stable_write_integrity = enforce_stable_write_integrity
        self._integrity_key = integrity_key or secrets.token_bytes(_MEMORY_INTEGRITY_KEY_BYTES)
        if len(self._integrity_key) != _MEMORY_INTEGRITY_KEY_BYTES:
            raise ValueError("Memory integrity key has an invalid size.")
        self._writes_since_seal = 0
        self._dirty_layers: set[MemoryLayer] = set()
        self._last_seal_monotonic = time.monotonic()

    @classmethod
    def from_backend_factory(
        cls,
        memory_dir: Path,
        backend_factory: type[MemoryBackend],
        specs: dict[MemoryLayer, LayerSpec] | None = None,
        ledger: PromotionLedger | None = None,
        vector_embedder: TextEmbedder | None = None,
        enforce_stable_write_integrity: bool = True,
        **backend_kwargs: object,
    ) -> LayeredMemorySystem:
        layer_specs = specs or DEFAULT_LAYER_SPECS
        prepare_private_memory_artifacts(
            memory_dir,
            specs=layer_specs,
            harden_existing=False,
        )
        backends: dict[MemoryLayer, MemoryBackend] = {}
        vector_sidecars: dict[MemoryLayer, VectorSidecar] = {}
        try:
            integrity_key = _load_or_create_memory_integrity_key(memory_dir)
            for layer, spec in layer_specs.items():
                path = memory_dir / spec.mv2_file
                layer_backend_kwargs = dict(backend_kwargs)
                backend = backend_factory(path=path, layer=layer, **layer_backend_kwargs)
                backends[layer] = backend
                backend.open()
                sidecar = _make_vector_sidecar(
                    memory_dir=memory_dir,
                    mv2_path=path,
                    spec=spec,
                    embedder=vector_embedder,
                )
                if sidecar is not None:
                    vector_sidecars[layer] = sidecar
                    sidecar.open()
        except Exception:
            for sidecar in reversed(tuple(vector_sidecars.values())):
                try:
                    sidecar.close()
                except Exception:
                    pass
            for backend in reversed(tuple(backends.values())):
                try:
                    backend.close()
                except Exception:
                    pass
            raise
        return cls(
            backends=backends,
            specs=layer_specs,
            ledger=ledger,
            vector_sidecars=vector_sidecars,
            enforce_stable_write_integrity=enforce_stable_write_integrity,
            integrity_key=integrity_key,
        )

    def put_runtime_validation_receipt(
        self,
        *,
        tool_name: str,
        tool_call_id: str,
        evidence_bucket: str,
        command: Iterable[str] = (),
        output_sha256: str,
        session_id: str,
        run_id: str | None,
        signed_artifact_source: str | None = None,
        signed_artifact_locator: str | None = None,
        subject_record_id: str | None = None,
    ) -> str:
        """Create a durable, authenticated receipt for successful runtime validation."""

        normalized_tool = tool_name.strip()
        normalized_call_id = tool_call_id.strip()
        normalized_bucket = evidence_bucket.strip()
        if (
            not normalized_tool
            or not normalized_call_id
            or normalized_bucket not in _VALIDATION_EVIDENCE_BUCKETS
            or not output_sha256.strip()
        ):
            raise ValueError("Runtime validation receipt fields are incomplete.")
        artifact_source = (signed_artifact_source or "").strip()
        artifact_locator = (signed_artifact_locator or "").strip()
        if bool(artifact_source) != bool(artifact_locator):
            raise ValueError("Signed artifact source and locator must be supplied together.")
        normalized_subject_id = (subject_record_id or "").strip()
        subject_record = (
            self.get_record(None, normalized_subject_id, include_inactive=False)
            if normalized_subject_id
            else None
        )
        if normalized_subject_id and subject_record is None:
            raise ValueError("Runtime validation subject record is missing or inactive.")
        receipt_id = f"validation_receipt_{sha256(secrets.token_bytes(32)).hexdigest()[:24]}"
        payload: dict[str, object] = {
            "schema": _RUNTIME_VALIDATION_RECEIPT_SCHEMA,
            "receipt_id": receipt_id,
            "tool_name": normalized_tool,
            "tool_call_id": normalized_call_id,
            "evidence_bucket": normalized_bucket,
            "command": [str(item) for item in command],
            "output_sha256": output_sha256.strip(),
            "session_id": session_id,
            "run_id": run_id,
            "signed_artifact_source": artifact_source or None,
            "signed_artifact_locator": artifact_locator or None,
            "stable_learning_eligible": subject_record is not None,
            "subject_record_id": subject_record.id if subject_record is not None else None,
            "subject_claim_digest": _stable_claim_digest(subject_record)
            if subject_record is not None
            else None,
        }
        signature = _validation_receipt_signature(payload, key=self._integrity_key)
        key_id = sha256(self._integrity_key).hexdigest()[:16]
        record = MemoryRecord(
            id=receipt_id,
            title=f"Validated {normalized_bucket} receipt: {normalized_tool}",
            content=json.dumps(payload, sort_keys=True),
            layer=MemoryLayer.EPISODIC,
            kind=MemoryKind.EVENT,
            confidence=0.95,
            importance=0.7,
            metadata={
                "frame_type": "trace_stub",
                "validation_status": "runtime_validated",
                "runtime_validation_receipt": True,
                "validation_receipt_schema": _RUNTIME_VALIDATION_RECEIPT_SCHEMA,
                "validation_receipt_payload": payload,
                "validation_receipt_signature": signature,
                "validation_receipt_key_id": key_id,
                "evidence_bucket": normalized_bucket,
                "signed_artifact_source": artifact_source or None,
                "signed_artifact_locator": artifact_locator or None,
            },
            evidence=[
                EvidenceRef(
                    source=f"tool://{normalized_tool}",
                    locator=normalized_call_id,
                )
            ],
        )
        return self.put(record)

    def is_authenticated_validation_receipt(
        self,
        record: MemoryRecord,
        *,
        evidence_bucket: str | None = None,
        require_subject_binding: bool = False,
    ) -> bool:
        metadata = record.metadata
        payload = metadata.get("validation_receipt_payload")
        if (
            record.layer != MemoryLayer.EPISODIC
            or record.kind != MemoryKind.EVENT
            or metadata.get("runtime_validation_receipt") is not True
            or metadata.get("validation_status") != "runtime_validated"
            or metadata.get("validation_receipt_schema") != _RUNTIME_VALIDATION_RECEIPT_SCHEMA
            or not isinstance(payload, dict)
            or payload.get("schema") != _RUNTIME_VALIDATION_RECEIPT_SCHEMA
            or payload.get("receipt_id") != record.id
            or metadata.get("validation_receipt_key_id")
            != sha256(self._integrity_key).hexdigest()[:16]
            or not isinstance(metadata.get("validation_receipt_signature"), str)
        ):
            return False
        bucket = str(payload.get("evidence_bucket") or "")
        if bucket not in _VALIDATION_EVIDENCE_BUCKETS or (
            evidence_bucket is not None and evidence_bucket != bucket
        ):
            return False
        if require_subject_binding and (
            payload.get("stable_learning_eligible") is not True
            or not str(payload.get("subject_record_id") or "").strip()
            or not str(payload.get("subject_claim_digest") or "").strip()
        ):
            return False
        tool_name = str(payload.get("tool_name") or "")
        tool_call_id = str(payload.get("tool_call_id") or "")
        if (
            not tool_name
            or not tool_call_id
            or not any(
                ref.source == f"tool://{tool_name}" and ref.locator == tool_call_id
                for ref in record.evidence
            )
        ):
            return False
        try:
            content_payload = json.loads(record.content)
        except json.JSONDecodeError:
            return False
        if content_payload != payload:
            return False
        expected = _validation_receipt_signature(payload, key=self._integrity_key)
        return hmac.compare_digest(
            str(metadata["validation_receipt_signature"]),
            expected,
        )

    def validation_receipt_subject(
        self,
        record: MemoryRecord,
    ) -> tuple[str, str, str, str | None] | None:
        if not self.is_authenticated_validation_receipt(
            record,
            require_subject_binding=True,
        ):
            return None
        payload = record.metadata["validation_receipt_payload"]
        if not isinstance(payload, dict):
            return None
        return (
            str(payload["subject_record_id"]),
            str(payload["subject_claim_digest"]),
            str(payload.get("session_id") or ""),
            str(payload["run_id"]) if payload.get("run_id") is not None else None,
        )

    def put(self, record: MemoryRecord) -> str:
        if self.enforce_stable_write_integrity and record.layer in _STABLE_LAYERS:
            raise ValueError(
                f"Direct {record.layer.value} memory writes are rejected by the stable-memory "
                "sink; use put_validated() with a resolved promotion envelope."
            )
        return self._put(record)

    def put_validated(
        self,
        record: MemoryRecord,
        *,
        authority: str,
        source_record_ids: Iterable[str],
        validation_evidence: object | None = None,
    ) -> str:
        """Persist a stable record only after validating its explicit envelope.

        Public tools never receive this capability directly.  Trusted runtime
        paths must name their authority and bind the write to durable source
        records that already exist in this memory system.
        """

        source_ids = tuple(
            dict.fromkeys(str(item).strip() for item in source_record_ids if str(item).strip())
        )
        if record.layer not in _STABLE_LAYERS:
            return self.put(record)
        if authority not in _STABLE_WRITE_AUTHORITIES:
            raise ValueError(f"Unknown stable-memory write authority: {authority}")
        runtime_validation_metadata = _runtime_validation_metadata(
            authority=authority,
            validation_evidence=validation_evidence,
        )
        source_records = [
            self.get_record(None, record_id, include_inactive=False) for record_id in source_ids
        ]
        if not source_ids or any(item is None for item in source_records):
            raise ValueError("Stable-memory promotion requires existing durable source_record_ids.")
        errors = _stable_record_integrity_errors(
            record,
            authority=authority,
            source_record_ids=source_ids,
            source_records=[item for item in source_records if item is not None],
            spec=self.specs[record.layer],
            runtime_validation_metadata=runtime_validation_metadata,
            authenticated_validation_source_ids={
                item.id
                for item in source_records
                if item is not None
                and self.is_authenticated_validation_receipt(
                    item,
                    require_subject_binding=True,
                )
            },
            validation_source_bindings={
                item.id: binding
                for item in source_records
                if item is not None
                and (binding := self.validation_receipt_subject(item)) is not None
            },
        )
        if errors:
            raise ValueError("Stable-memory promotion rejected: " + "; ".join(errors))
        envelope = {
            "version": _STABLE_WRITE_ENVELOPE_VERSION,
            "authority": authority,
            "target_layer": record.layer.value,
            "source_record_ids": list(source_ids),
            "validation_status": _stable_validation_status(record, authority),
            "evidence_resolved": True,
        }
        prepared = replace(
            record,
            metadata={
                **record.metadata,
                "source_record_ids": list(source_ids),
                "stable_write_envelope": envelope,
            },
        )
        return self._put(prepared)

    def _put(self, record: MemoryRecord) -> str:
        record = sanitize_memory_record(record)
        spec = self.specs[record.layer]
        record = _with_default_retention(record, spec)
        record, conflict_frame, conflicts = self._with_conflict_metadata(record)
        if record.confidence < spec.min_write_confidence:
            raise ValueError(
                f"Record confidence {record.confidence:.2f} is below {record.layer} write threshold "
                f"{spec.min_write_confidence:.2f}"
            )
        confirmed_twin = self._confirmed_record_matches_provisional(record)
        if confirmed_twin is not None:
            self._record_promotion(record, record_id=confirmed_twin.id)
            entry = _promotion_entry_from_record(record, record_id=confirmed_twin.id)
            if entry is not None:
                self.confirm_provisional(confirmed_twin.id, entry)
            return confirmed_twin.id
        record_id = self.backends[record.layer].put(record)
        self._note_write(record.layer)
        self._update_vector_sidecar(record)
        self._record_promotion(record, record_id=record_id)
        self._record_conflict_outcomes(record, conflicts)
        if conflict_frame is not None:
            conflict_frame.confidence = max(conflict_frame.confidence, spec.min_write_confidence)
            conflict_record = _with_default_retention(
                to_memory_record(conflict_frame), self.specs[conflict_frame.layer]
            )
            conflict_record.metadata.update(
                {
                    "source_record_ids": [record.id, *(item.id for item in conflicts)],
                    "validation_status": "derived_conflict_audit",
                }
            )
            conflict_record.evidence = [
                EvidenceRef(source="memory_record", locator=record_id)
                for record_id in conflict_record.metadata["source_record_ids"]
            ]
            self.backends[conflict_frame.layer].put(conflict_record)
            self._note_write(conflict_frame.layer)
            self._update_vector_sidecar(conflict_record)
        return record_id

    def upsert(self, record: MemoryRecord) -> str:
        if self.enforce_stable_write_integrity and record.layer in _STABLE_LAYERS:
            raise ValueError(
                f"Direct {record.layer.value} memory upserts are rejected by the stable-memory "
                "sink; use upsert_validated() with a resolved promotion envelope."
            )
        return self._upsert(record)

    def upsert_validated(
        self,
        record: MemoryRecord,
        *,
        authority: str,
        source_record_ids: Iterable[str],
        validation_evidence: object | None = None,
    ) -> str:
        source_ids = tuple(
            dict.fromkeys(str(item).strip() for item in source_record_ids if str(item).strip())
        )
        if record.layer not in _STABLE_LAYERS:
            return self.upsert(record)
        if authority not in _STABLE_WRITE_AUTHORITIES:
            raise ValueError(f"Unknown stable-memory write authority: {authority}")
        runtime_validation_metadata = _runtime_validation_metadata(
            authority=authority,
            validation_evidence=validation_evidence,
        )
        source_records = [
            self.get_record(None, record_id, include_inactive=False) for record_id in source_ids
        ]
        if not source_ids or any(item is None for item in source_records):
            raise ValueError("Stable-memory promotion requires existing durable source_record_ids.")
        errors = _stable_record_integrity_errors(
            record,
            authority=authority,
            source_record_ids=source_ids,
            source_records=[item for item in source_records if item is not None],
            spec=self.specs[record.layer],
            runtime_validation_metadata=runtime_validation_metadata,
            authenticated_validation_source_ids={
                item.id
                for item in source_records
                if item is not None
                and self.is_authenticated_validation_receipt(
                    item,
                    require_subject_binding=True,
                )
            },
            validation_source_bindings={
                item.id: binding
                for item in source_records
                if item is not None
                and (binding := self.validation_receipt_subject(item)) is not None
            },
        )
        if errors:
            raise ValueError("Stable-memory promotion rejected: " + "; ".join(errors))
        prepared = replace(
            record,
            metadata={
                **record.metadata,
                "source_record_ids": list(source_ids),
                "stable_write_envelope": {
                    "version": _STABLE_WRITE_ENVELOPE_VERSION,
                    "authority": authority,
                    "target_layer": record.layer.value,
                    "source_record_ids": list(source_ids),
                    "validation_status": _stable_validation_status(record, authority),
                    "evidence_resolved": True,
                },
            },
        )
        return self._upsert(prepared)

    def _upsert(self, record: MemoryRecord) -> str:
        record = sanitize_memory_record(record)
        spec = self.specs[record.layer]
        record = _with_default_retention(record, spec)
        record, conflict_frame, conflicts = self._with_conflict_metadata(record)
        if record.confidence < spec.min_write_confidence:
            raise ValueError(
                f"Record confidence {record.confidence:.2f} is below {record.layer} write threshold "
                f"{spec.min_write_confidence:.2f}"
            )
        confirmed_twin = self._confirmed_record_matches_provisional(record)
        if confirmed_twin is not None and confirmed_twin.id != record.id:
            self._record_promotion(record, record_id=confirmed_twin.id)
            entry = _promotion_entry_from_record(record, record_id=confirmed_twin.id)
            if entry is not None:
                self.confirm_provisional(confirmed_twin.id, entry)
            return confirmed_twin.id
        record_id = self.backends[record.layer].upsert(record)
        self._note_write(record.layer)
        self._update_vector_sidecar(record)
        self._record_promotion(record, record_id=record_id)
        self._record_conflict_outcomes(record, conflicts)
        if conflict_frame is not None:
            conflict_frame.confidence = max(conflict_frame.confidence, spec.min_write_confidence)
            conflict_record = _with_default_retention(
                to_memory_record(conflict_frame), self.specs[conflict_frame.layer]
            )
            conflict_record.metadata.update(
                {
                    "source_record_ids": [record.id, *(item.id for item in conflicts)],
                    "validation_status": "derived_conflict_audit",
                }
            )
            conflict_record.evidence = [
                EvidenceRef(source="memory_record", locator=record_id)
                for record_id in conflict_record.metadata["source_record_ids"]
            ]
            self.backends[conflict_frame.layer].upsert(conflict_record)
            self._note_write(conflict_frame.layer)
            self._update_vector_sidecar(conflict_record)
        return record_id

    def tombstone(
        self,
        layer: MemoryLayer,
        record_id: str,
        *,
        reason: str,
        superseded_by: str | None = None,
    ) -> bool:
        record = self.backends[layer].get_record(record_id, include_inactive=True)
        changed = self.backends[layer].tombstone(
            record_id, reason=reason, superseded_by=superseded_by
        )
        if changed:
            self._note_write(layer)
            self._tombstone_vector_sidecar(layer, record_id)
            promotion_id = None if record is None else _promotion_id(record)
            if promotion_id:
                if reason == "corrected":
                    self.record_promotion_outcome(
                        promotion_id,
                        "corrected",
                        evidence_record_id=superseded_by,
                        notes="correction frame superseded this promoted record",
                    )
                elif superseded_by:
                    self.record_promotion_outcome(
                        promotion_id,
                        "superseded",
                        evidence_record_id=superseded_by,
                        notes=f"record superseded during tombstone: {reason}",
                    )
                self.record_promotion_outcome(
                    promotion_id,
                    "tombstoned",
                    evidence_record_id=superseded_by,
                    notes=reason,
                )
        return changed

    def iter_records(
        self, layer: MemoryLayer | None = None, *, include_inactive: bool = False
    ) -> Iterable[MemoryRecord]:
        layers = (layer,) if layer is not None else tuple(self.backends)
        for selected in layers:
            yield from self.backends[selected].iter_records(include_inactive=include_inactive)

    def get_record(
        self,
        layer: MemoryLayer | None,
        record_id: str,
        *,
        include_inactive: bool = True,
    ) -> MemoryRecord | None:
        layers = (layer,) if layer is not None else tuple(self.backends)
        for selected in layers:
            record = self.backends[selected].get_record(
                record_id, include_inactive=include_inactive
            )
            if record is not None:
                return record
        return None

    def put_frame(self, frame: MV2ContextFrame) -> str:
        return self.put(to_memory_record(frame))

    def retrieve(self, query: RetrievalQuery) -> list[MemoryHit]:
        hits: list[MemoryHit] = []
        now = datetime.now(UTC)
        for layer in query.layers:
            spec = self.specs[layer]
            k = min(query.k_per_layer, spec.retrieval_k)
            mode = _resolved_search_mode(spec, query.mode)
            eligible_by_id = self._eligible_layer_hits(
                layer=layer,
                query=query,
                k=k,
                mode=mode,
                now=now,
            )
            hits.extend(
                sorted(
                    eligible_by_id.values(),
                    key=lambda hit: (hit.score, hit.record.importance),
                    reverse=True,
                )[:k]
            )
        ordered = sorted(hits, key=lambda hit: (hit.score, hit.record.importance), reverse=True)
        self._write_back_retrieval_hits(ordered)
        return ordered

    def _eligible_layer_hits(
        self,
        *,
        layer: MemoryLayer,
        query: RetrievalQuery,
        k: int,
        mode: str,
        now: datetime,
    ) -> dict[str, MemoryHit]:
        if query.include_inactive and query.include_retrieval_artifacts:
            direct_hits = self._find_layer_hits(
                layer=layer,
                query=query.query,
                k=k,
                mode=mode,
                min_relevancy=query.min_relevancy,
                include_inactive=True,
            )
            return _best_hits_by_record_id(direct_hits)

        eligible_by_id: dict[str, MemoryHit] = {}
        cursor: str | None = None
        seen_cursors: set[str] = set()
        examined = 0
        while examined < _RETRIEVAL_MAX_CANDIDATES_PER_LAYER:
            page_size = min(
                _RETRIEVAL_CANDIDATE_PAGE_SIZE,
                _RETRIEVAL_MAX_CANDIDATES_PER_LAYER - examined,
            )
            page = self._find_layer_hit_page(
                layer=layer,
                query=query.query,
                k=page_size,
                mode=mode,
                min_relevancy=query.min_relevancy,
                include_inactive=query.include_inactive,
                cursor=cursor,
            )
            # Charge the requested page size, not only converted hits. A
            # backend may omit inactive/corrupt candidates while still
            # returning a continuation cursor; those pages must consume the
            # same hard work budget.
            examined += page_size
            for hit in page.hits:
                if (
                    not query.include_retrieval_artifacts and _is_retrieval_artifact(hit.record)
                ) or (not query.include_inactive and memory_record_is_expired(hit.record, now=now)):
                    continue
                existing = eligible_by_id.get(hit.record.id)
                if existing is None or hit.score > existing.score:
                    eligible_by_id[hit.record.id] = hit
            if len(eligible_by_id) >= k or page.next_cursor is None:
                break
            if page.next_cursor in seen_cursors:
                break
            seen_cursors.add(page.next_cursor)
            cursor = page.next_cursor
        return eligible_by_id

    def record_promotion_outcome(
        self,
        promotion_id: str,
        outcome: str,
        *,
        evidence_record_id: str | None = None,
        notes: str = "",
    ) -> None:
        if self.ledger is None:
            return
        self.ledger.record_outcome(
            make_outcome(
                promotion_id,
                outcome,  # type: ignore[arg-type]
                evidence_record_id=evidence_record_id,
                notes=notes,
            )
        )

    def confirm_provisional(self, record_id: str, evidence: PromotionEntry) -> bool:
        record = self.get_record(None, record_id, include_inactive=True)
        if record is None or record.metadata.get("promotion_status") != "provisional":
            return False
        if record.layer in _STABLE_LAYERS and not _stable_record_has_valid_envelope(record):
            return False
        record.metadata["promotion_status"] = "confirmed"
        record.metadata["confirmed_at"] = datetime.now(UTC).isoformat()
        record.metadata["confirmation_promotion_id"] = evidence.promotion_id
        record.metadata["confirmation_evidence_record_id"] = evidence.record_id
        record.expires_at = None
        record.updated_at = datetime.now(UTC)
        self.backends[record.layer].upsert(record)
        self._note_write(record.layer)
        old_promotion_id = _promotion_id(record)
        if old_promotion_id:
            self.record_promotion_outcome(
                old_promotion_id,
                "useful",
                evidence_record_id=evidence.record_id,
                notes="provisional record confirmed by later full-threshold evidence",
            )
        return True

    def seal_all(self) -> None:
        self._seal_layers(tuple(self.backends))

    def _seal_dirty_layers(self) -> None:
        self._seal_layers(tuple(self._dirty_layers))

    def _seal_layers(self, layers: tuple[MemoryLayer, ...]) -> None:
        selected = set(layers)
        for layer, backend in self.backends.items():
            if layer in selected:
                backend.seal()
        self._writes_since_seal = 0
        self._dirty_layers.difference_update(selected)
        self._last_seal_monotonic = time.monotonic()

    def maybe_seal_all(
        self,
        *,
        write_threshold: int = 50,
        interval_seconds: float = 10.0,
        force: bool = False,
    ) -> bool:
        if not self._dirty_layers:
            return False
        if force or self._requires_eager_seal():
            self._seal_dirty_layers()
            return True
        durable_layers = {
            MemoryLayer.SEMANTIC,
            MemoryLayer.PROCEDURAL,
            MemoryLayer.SELF,
            MemoryLayer.POLICY,
        }
        if self._dirty_layers & durable_layers:
            self._seal_dirty_layers()
            return True
        elapsed = time.monotonic() - self._last_seal_monotonic
        if self._writes_since_seal >= max(write_threshold, 1) or elapsed >= max(
            interval_seconds, 0.001
        ):
            self._seal_dirty_layers()
            return True
        return False

    def verify_all(self) -> dict[MemoryLayer, bool]:
        return {layer: backend.verify() for layer, backend in self.backends.items()}

    def close_all(self) -> None:
        first_error: Exception | None = None
        try:
            self.maybe_seal_all(force=True)
        except Exception as exc:
            first_error = exc
        for backend in self.backends.values():
            try:
                backend.close()
            except Exception as exc:
                if first_error is None:
                    first_error = exc
        for sidecar in self.vector_sidecars.values():
            try:
                sidecar.close()
            except Exception as exc:
                if first_error is None:
                    first_error = exc
        if first_error is not None:
            raise first_error

    def iter_layers(self) -> Iterable[tuple[MemoryLayer, LayerSpec]]:
        return self.specs.items()

    def vector_index_status(self) -> dict[MemoryLayer, VectorSidecarStatus]:
        statuses: dict[MemoryLayer, VectorSidecarStatus] = {}
        for layer in self.specs:
            sidecar = self.vector_sidecars.get(layer)
            if sidecar is None:
                reason = (
                    "policy memory is lexical-only"
                    if layer == MemoryLayer.POLICY
                    else "vector sidecar not configured"
                )
                statuses[layer] = VectorSidecarStatus.disabled(layer, reason)
                continue
            statuses[layer] = sidecar.status(
                records=tuple(self.backends[layer].iter_records(include_inactive=True))
            )
        return statuses

    def rebuild_vector_indexes(
        self,
        layers: tuple[MemoryLayer, ...] | None = None,
    ) -> dict[MemoryLayer, VectorSidecarStatus]:
        selected_layers = layers or tuple(self.vector_sidecars)
        rebuilt: dict[MemoryLayer, VectorSidecarStatus] = {}
        for layer in selected_layers:
            sidecar = self.vector_sidecars.get(layer)
            if sidecar is None:
                rebuilt[layer] = VectorSidecarStatus.disabled(
                    layer, "vector sidecar not configured"
                )
                continue
            rebuilt[layer] = sidecar.rebuild(
                tuple(self.backends[layer].iter_records(include_inactive=True))
            )
        return rebuilt

    def _with_conflict_metadata(
        self, record: MemoryRecord
    ) -> tuple[MemoryRecord, MV2ContextFrame | None, list[MemoryRecord]]:
        if not _eligible_for_conflict_detection(record):
            return record, None, []
        conflicts = [
            existing
            for existing in self.iter_records(record.layer)
            if existing.id != record.id and _records_conflict(existing, record)
        ]
        if not conflicts:
            return record, None, []
        group_id = _conflict_group_id(record, conflicts)
        record.metadata["conflict_group_id"] = group_id
        member_ids = tuple(existing.id for existing in conflicts) + (record.id,)
        for existing in conflicts:
            if existing.metadata.get("conflict_group_id") == group_id:
                continue
            if (
                self.enforce_stable_write_integrity
                and existing.layer in _STABLE_LAYERS
                and not _stable_record_has_valid_envelope(existing)
            ):
                continue
            existing.metadata["conflict_group_id"] = group_id
            self.backends[existing.layer].upsert(existing)
        conflict_frame = make_conflict_set_frame(
            layer=MemoryLayer.EPISODIC,
            conflict_group_id=group_id,
            member_ids=member_ids,
            reason="deterministic conflict detection after memory write",
        )
        return record, conflict_frame, conflicts

    def _note_write(self, layer: MemoryLayer) -> None:
        self._writes_since_seal += 1
        self._dirty_layers.add(layer)

    def _update_vector_sidecar(self, record: MemoryRecord) -> None:
        sidecar = self.vector_sidecars.get(record.layer)
        if sidecar is not None:
            sidecar.upsert(record)

    def _tombstone_vector_sidecar(self, layer: MemoryLayer, record_id: str) -> None:
        sidecar = self.vector_sidecars.get(layer)
        if sidecar is not None:
            sidecar.tombstone(record_id)

    def _find_layer_hits(
        self,
        *,
        layer: MemoryLayer,
        query: str,
        k: int,
        mode: str,
        min_relevancy: float,
        include_inactive: bool,
    ) -> list[MemoryHit]:
        sidecar = self.vector_sidecars.get(layer)
        if mode in {"vec", "vector"}:
            return self._find_vector_sidecar_hits(
                layer=layer,
                query=query,
                k=k,
                min_relevancy=min_relevancy,
                include_inactive=include_inactive,
            )
        if mode == "hybrid" and sidecar is not None:
            lexical_hits = self.backends[layer].find(
                query=query,
                k=k,
                mode="lex",
                min_relevancy=min_relevancy,
                include_inactive=include_inactive,
            )
            vector_hits = self._find_vector_sidecar_hits(
                layer=layer,
                query=query,
                k=k,
                min_relevancy=min_relevancy,
                include_inactive=include_inactive,
            )
            return _fuse_memory_hits(lexical_hits, vector_hits, k=k)
        backend_mode = "lex" if mode == "hybrid" else mode
        return self.backends[layer].find(
            query=query,
            k=k,
            mode=backend_mode,
            min_relevancy=min_relevancy,
            include_inactive=include_inactive,
        )

    def _find_layer_hit_page(
        self,
        *,
        layer: MemoryLayer,
        query: str,
        k: int,
        mode: str,
        min_relevancy: float,
        include_inactive: bool,
        cursor: str | None,
    ) -> MemorySearchPage:
        sidecar = self.vector_sidecars.get(layer)
        if mode in {"vec", "vector"} and sidecar is None:
            return MemorySearchPage(hits=())
        uses_sidecar = sidecar is not None and mode in {"vec", "vector", "hybrid"}
        if not uses_sidecar:
            backend_mode = "lex" if mode == "hybrid" else mode
            return self.backends[layer].find_page(
                query=query,
                k=k,
                mode=backend_mode,
                min_relevancy=min_relevancy,
                include_inactive=include_inactive,
                cursor=cursor,
            )

        offset = _layer_page_offset(cursor)
        end = offset + k
        window = self._find_layer_hits(
            layer=layer,
            query=query,
            k=end + 1,
            mode=mode,
            min_relevancy=min_relevancy,
            include_inactive=include_inactive,
        )
        return MemorySearchPage(
            hits=tuple(window[offset:end]),
            next_cursor=f"layer-offset:{end}" if len(window) > end else None,
        )

    def _find_vector_sidecar_hits(
        self,
        *,
        layer: MemoryLayer,
        query: str,
        k: int,
        min_relevancy: float,
        include_inactive: bool,
    ) -> list[MemoryHit]:
        sidecar = self.vector_sidecars.get(layer)
        if sidecar is None:
            return []
        hits: list[MemoryHit] = []
        for vector_hit in sidecar.search(
            query, k=k, min_score=min_relevancy, include_inactive=include_inactive
        ):
            record = self.backends[layer].get_record(
                vector_hit.record_id, include_inactive=include_inactive
            )
            if record is None:
                continue
            hits.append(
                MemoryHit(
                    record=record,
                    score=vector_hit.score,
                    source_backend="vector_sidecar",
                    frame_id=record.id,
                    snippet=record.content[:220],
                )
            )
        return hits

    def _requires_eager_seal(self) -> bool:
        return any(
            backend.__class__.__name__ == "InMemoryBackend" for backend in self.backends.values()
        )

    def _record_promotion(self, record: MemoryRecord, *, record_id: str) -> None:
        if self.ledger is None:
            return
        entry = _promotion_entry_from_record(record, record_id=record_id)
        if entry is not None:
            self.ledger.record_promotion(entry)

    def _record_conflict_outcomes(
        self, record: MemoryRecord, conflicts: list[MemoryRecord]
    ) -> None:
        if not conflicts:
            return
        for existing in conflicts:
            promotion_id = _promotion_id(existing)
            if not promotion_id:
                continue
            self.record_promotion_outcome(
                promotion_id,
                "contradicted",
                evidence_record_id=record.id,
                notes="deterministic conflict detection after memory write",
            )

    def _write_back_retrieval_hits(self, hits: list[MemoryHit]) -> None:
        now = datetime.now(UTC)
        for hit in hits:
            record = hit.record
            previous = _metadata_datetime(record.metadata.get("last_retrieved_at"))
            if previous is not None and (now - previous).total_seconds() < 3600:
                continue
            record.metadata["last_retrieved_at"] = now.isoformat()
            record.updated_at = now
            if record.layer in _STABLE_LAYERS and not _stable_record_has_valid_envelope(record):
                continue
            self.backends[record.layer].upsert(record)
            self._note_write(record.layer)

    def _confirmed_record_matches_provisional(self, record: MemoryRecord) -> MemoryRecord | None:
        if record.layer in _STABLE_LAYERS and not _stable_record_has_valid_envelope(record):
            return None
        if record.metadata.get("promotion_status", "confirmed") != "confirmed":
            return None
        if record.metadata.get("active", True) is False:
            return None
        for existing in self.iter_records(record.layer):
            if existing.id == record.id:
                continue
            if existing.metadata.get("promotion_status") != "provisional":
                continue
            if existing.layer in _STABLE_LAYERS and not _stable_record_has_valid_envelope(existing):
                continue
            if _conceptually_same_record(existing, record):
                return existing
        return None


def load_layer_specs(path: Path) -> dict[MemoryLayer, LayerSpec]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("Layer spec config must be a JSON object")
    specs = dict(DEFAULT_LAYER_SPECS)
    for layer_name, payload in raw.items():
        layer = MemoryLayer(str(layer_name))
        if not isinstance(payload, dict):
            raise ValueError(f"Layer spec for {layer.value} must be an object")
        base = specs[layer]
        vector_payload = payload.get("vector")
        vector: dict[str, object] = vector_payload if isinstance(vector_payload, dict) else {}
        vector_enabled = bool(vector.get("enabled", payload.get("vector_search_enabled", False)))
        provider = _optional_str(
            vector.get("embedding_provider", payload.get("vector_embedding_provider"))
        )
        embedding_model = _optional_str(
            vector.get("embedding_model", payload.get("vector_embedding_model"))
        )
        index_path = _optional_str(vector.get("index_path", payload.get("vector_index_path")))
        local_vector_enabled = bool(vector_enabled and provider == "local" and index_path)
        search_mode = str(payload.get("search_mode", base.search_mode))
        if (
            layer == MemoryLayer.PROCEDURAL
            and local_vector_enabled
            and "search_mode" not in payload
        ):
            search_mode = "hybrid"
        if layer == MemoryLayer.POLICY or not local_vector_enabled:
            search_mode = (
                "lex"
                if layer == MemoryLayer.POLICY
                else ("lex" if search_mode in {"vec", "vector", "hybrid"} else search_mode)
            )
            local_vector_enabled = False
        provisional_threshold_raw = payload.get("provisional_threshold", base.provisional_threshold)
        specs[layer] = LayerSpec(
            layer=layer,
            description=str(payload.get("description", base.description)),
            mv2_file=str(payload.get("mv2_file", base.mv2_file)),
            update_cadence=str(payload.get("update_cadence", base.update_cadence)),
            retrieval_k=int(payload.get("retrieval_k", base.retrieval_k)),
            context_budget_chars=int(
                payload.get("context_budget_chars", base.context_budget_chars)
            ),
            min_write_confidence=float(
                payload.get("min_write_confidence", base.min_write_confidence)
            ),
            promotion_threshold=float(payload.get("promotion_threshold", base.promotion_threshold)),
            min_repeat_count_for_promotion=int(
                payload.get("min_repeat_count_for_promotion", base.min_repeat_count_for_promotion)
            ),
            retention_days=int(payload.get("retention_days", base.retention_days)),
            provisional_threshold=None
            if provisional_threshold_raw is None
            else float(provisional_threshold_raw),
            search_mode=search_mode,
            vector_search_enabled=local_vector_enabled,
            vector_embedding_provider=provider if local_vector_enabled else None,
            vector_embedding_model=embedding_model if local_vector_enabled else None,
            vector_index_path=index_path if local_vector_enabled else None,
            hybrid_search_enabled=local_vector_enabled and search_mode == "hybrid",
        )
    validate_layer_artifact_paths(specs)
    return specs


def _make_vector_sidecar(
    *,
    memory_dir: Path,
    mv2_path: Path,
    spec: LayerSpec,
    embedder: TextEmbedder | None,
) -> VectorSidecar | None:
    index_path = _configured_vector_index_path(memory_dir=memory_dir, spec=spec)
    if index_path is None:
        return None
    return VectorSidecar(
        path=index_path,
        layer=spec.layer,
        embedder=embedder or make_local_embedder(spec.vector_embedding_model),
        mv2_path=mv2_path,
        provider="local",
    )


def _configured_vector_index_path(*, memory_dir: Path, spec: LayerSpec) -> Path | None:
    if spec.layer == MemoryLayer.POLICY:
        return None
    if (
        not spec.vector_search_enabled
        or spec.vector_embedding_provider != "local"
        or not spec.vector_index_path
    ):
        return None
    index_name = _validated_artifact_name(
        spec.vector_index_path,
        label=f"{spec.layer.value} vector index_path",
    )
    return memory_dir / index_name


def _resolved_search_mode(spec: LayerSpec, requested_mode: str) -> str:
    mode = requested_mode if requested_mode != "auto" else spec.search_mode
    if spec.layer == MemoryLayer.POLICY:
        return "lex"
    if mode in {"vec", "vector", "hybrid"} and not spec.vector_search_enabled:
        return "lex"
    return mode


def _fuse_memory_hits(
    lexical_hits: list[MemoryHit], vector_hits: list[MemoryHit], *, k: int
) -> list[MemoryHit]:
    by_id: dict[str, MemoryHit] = {}
    for hit in lexical_hits:
        by_id[hit.record.id] = hit
    for hit in vector_hits:
        existing = by_id.get(hit.record.id)
        if existing is None or hit.score > existing.score:
            by_id[hit.record.id] = hit
    return sorted(by_id.values(), key=lambda hit: (hit.score, hit.record.importance), reverse=True)[
        :k
    ]


def _best_hits_by_record_id(hits: Iterable[MemoryHit]) -> dict[str, MemoryHit]:
    best: dict[str, MemoryHit] = {}
    for hit in hits:
        existing = best.get(hit.record.id)
        if existing is None or hit.score > existing.score:
            best[hit.record.id] = hit
    return best


def _layer_page_offset(cursor: str | None) -> int:
    if cursor is None:
        return 0
    prefix = "layer-offset:"
    if not cursor.startswith(prefix):
        raise ValueError("Invalid layered memory search cursor")
    try:
        offset = int(cursor.removeprefix(prefix))
    except ValueError as exc:
        raise ValueError("Invalid layered memory search cursor") from exc
    if offset < 0:
        raise ValueError("Invalid layered memory search cursor")
    return offset


def _with_default_retention(record: MemoryRecord, spec: LayerSpec) -> MemoryRecord:
    if record.layer in {MemoryLayer.WORKING, MemoryLayer.EPISODIC} and record.expires_at is None:
        record.expires_at = datetime.now(UTC) + timedelta(days=max(spec.retention_days, 0))
    return record


def _is_retrieval_artifact(record: MemoryRecord) -> bool:
    if record.metadata.get("retrieval_artifact") is True:
        return True
    source_uri = str(record.metadata.get("source_uri") or "")
    return source_uri.startswith(
        (
            "tool://context.expand/",
            "tool://context.pack/",
            "tool://memory.conflicts/",
            "tool://memory.export/",
            "tool://memory.inspect/",
            "tool://memory.ledger/",
            "tool://memory.search/",
        )
    )


def memory_record_is_expired(
    record: MemoryRecord,
    *,
    now: datetime | None = None,
) -> bool:
    """Return whether a record is expired, interpreting naive values as UTC."""

    if record.expires_at is None:
        return False
    expires_at = record.expires_at
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=UTC)
    compared_at = now or datetime.now(UTC)
    if compared_at.tzinfo is None:
        compared_at = compared_at.replace(tzinfo=UTC)
    return expires_at.astimezone(UTC) <= compared_at.astimezone(UTC)


def _stable_record_integrity_errors(
    record: MemoryRecord,
    *,
    authority: str,
    source_record_ids: tuple[str, ...],
    source_records: list[MemoryRecord],
    spec: LayerSpec,
    runtime_validation_metadata: dict[str, object] | None,
    authenticated_validation_source_ids: set[str],
    validation_source_bindings: dict[str, tuple[str, str, str, str | None]],
) -> list[str]:
    errors: list[str] = []
    if record.layer not in _STABLE_LAYERS:
        return errors
    if record.confidence < spec.min_write_confidence:
        errors.append("confidence_below_layer_threshold")
    if not record.evidence or any(
        not ref.source.strip() or not ref.locator.strip() for ref in record.evidence
    ):
        errors.append("nonempty_provenance_required")
    if len(source_records) != len(source_record_ids):
        errors.append("unresolved_source_record")
    evidence_locators = {ref.locator.strip() for ref in record.evidence}
    if not set(source_record_ids).issubset(evidence_locators):
        errors.append("source_records_not_bound_to_evidence_refs")

    if authority == "nested_learning":
        validation = record.metadata.get("validation_evidence")
        nested = record.metadata.get("nested_learning")
        decision = nested.get("decision") if isinstance(nested, dict) else None
        if not isinstance(validation, dict):
            errors.append("structured_validation_evidence_required")
        else:
            if runtime_validation_metadata is None or validation != runtime_validation_metadata:
                errors.append("runtime_validation_capability_required")
            if validation.get("legacy_raw_score") is not False:
                errors.append("legacy_raw_score_forbidden")
            if validation.get("resolved") is not True:
                errors.append("validation_evidence_unresolved")
            if validation.get("validation_status") not in {
                "runtime_validated",
                "human_confirmed",
                "operator_approved",
            }:
                errors.append("validation_status_unresolved")
            resolution_ids = validation.get("resolution_artifact_ids")
            if not isinstance(resolution_ids, list) or not resolution_ids:
                errors.append("resolution_artifact_ids_required")
            elif not set(str(item) for item in resolution_ids).issubset(set(source_record_ids)):
                errors.append("resolution_artifacts_not_bound_to_source_records")
            elif not set(str(item) for item in resolution_ids).issubset(
                authenticated_validation_source_ids
            ):
                errors.append("resolution_artifacts_not_authenticated")
            else:
                claim_digest = _stable_claim_digest(record)
                source_records_by_id = {item.id: item for item in source_records}
                target_session_id = str(record.metadata.get("session_id") or "")
                raw_target_run_id = record.metadata.get("run_id")
                target_run_id = (
                    str(raw_target_run_id) if raw_target_run_id is not None else None
                )
                for resolution_id in (str(item) for item in resolution_ids):
                    binding = validation_source_bindings.get(resolution_id)
                    subject_record = (
                        source_records_by_id.get(binding[0]) if binding is not None else None
                    )
                    if (
                        binding is None
                        or binding[0] not in source_record_ids
                        or binding[1] != claim_digest
                        or subject_record is None
                        or _stable_claim_digest(subject_record) != binding[1]
                    ):
                        errors.append("validation_receipt_subject_mismatch")
                        break
                    if (
                        (binding[3] is not None and binding[3] != target_run_id)
                        or (binding[3] is None and binding[2] != target_session_id)
                    ):
                        errors.append("validation_receipt_run_scope_mismatch")
                        break
        if (
            not isinstance(decision, dict)
            or decision.get("accepted") is not True
            or decision.get("target_layer") != record.layer.value
        ):
            errors.append("nested_learning_decision_invalid")
        repeat_count = record.metadata.get("repeat_count")
        if (
            not isinstance(repeat_count, int)
            or isinstance(repeat_count, bool)
            or repeat_count < spec.min_repeat_count_for_promotion
        ):
            errors.append("resolved_repeat_count_below_threshold")
        resolution_id_set = (
            {str(item) for item in validation.get("resolution_artifact_ids", [])}
            if isinstance(validation, dict)
            and isinstance(validation.get("resolution_artifact_ids"), list)
            else set()
        )
        if (
            record.layer in {MemoryLayer.PROCEDURAL, MemoryLayer.POLICY}
            and len(resolution_id_set) < spec.min_repeat_count_for_promotion
        ):
            errors.append("distinct_source_records_below_threshold")
        if record.layer == MemoryLayer.POLICY:
            bindings = record.metadata.get("resolved_artifact_bindings")
            if (
                not isinstance(validation, dict)
                or validation.get("validation_status") != "operator_approved"
            ):
                errors.append("policy_operator_approval_unresolved")
            if not isinstance(record.metadata.get("approval_provenance"), dict):
                errors.append("policy_approval_provenance_required")
            if (
                not isinstance(bindings, dict)
                or set(bindings) != resolution_id_set
                or any(
                    not isinstance(binding, dict)
                    or str(binding.get("evidence_bucket") or "")
                    not in {"test", "lint", "repair", "review"}
                    or bool(str(binding.get("source") or "").strip())
                    != bool(str(binding.get("locator") or "").strip())
                    or (
                        str(binding.get("evidence_bucket") or "") == "repair"
                        and str(binding.get("source") or "").strip()
                        != "repair.validate"
                    )
                    or (
                        str(binding.get("evidence_bucket") or "") == "review"
                        and str(binding.get("source") or "").strip()
                        != "repair.review"
                    )
                    or (
                        str(binding.get("evidence_bucket") or "") in {"test", "lint"}
                        and bool(str(binding.get("source") or "").strip())
                    )
                    for binding in bindings.values()
                )
            ):
                errors.append("policy_artifact_bindings_invalid")
    elif authority == "lesson_resolution":
        if (
            record.layer != MemoryLayer.PROCEDURAL
            or record.kind != MemoryKind.PROCEDURE
            or record.metadata.get("cognition_schema") != "lesson_card.v1"
            or record.metadata.get("validation_status") != "validated_once"
        ):
            errors.append("lesson_card_contract_invalid")
        if not _is_positive_int(record.metadata.get("success_count")):
            errors.append("lesson_success_required")
        if not _is_positive_int(record.metadata.get("failure_count")):
            errors.append("lesson_failure_required")
        if not any(
            source.layer == MemoryLayer.EPISODIC
            and source.kind == MemoryKind.FAILURE
            and source.metadata.get("validation_status") == "resolved"
            and source.id in source_record_ids
            for source in source_records
        ):
            errors.append("resolved_failure_episode_required")
        evidence_sources = {ref.source for ref in record.evidence}
        if not {"failure_episode", "validation"}.issubset(evidence_sources):
            errors.append("lesson_failure_and_validation_evidence_required")
    elif authority == "approved_correction":
        corrected_ids = record.metadata.get("corrects")
        if (
            record.kind != MemoryKind.CORRECTION
            or record.metadata.get("validation_status") != "correction_written"
            or not isinstance(corrected_ids, list)
            or set(str(item) for item in corrected_ids) != set(source_record_ids)
        ):
            errors.append("approved_correction_contract_invalid")
    return errors


def _runtime_validation_metadata(
    *,
    authority: str,
    validation_evidence: object | None,
) -> dict[str, object] | None:
    if authority != "nested_learning":
        return None
    # Imported lazily to avoid the module-level dependency cycle: the nested
    # learning kernel consumes layer specs while the sink verifies the opaque
    # in-process evidence-resolution capability carried by ValidationEvidence.
    from .nested_learning import ValidationEvidence, validation_evidence_is_resolved

    if not isinstance(
        validation_evidence, ValidationEvidence
    ) or not validation_evidence_is_resolved(validation_evidence):
        raise ValueError(
            "Stable-memory nested-learning writes require a resolved runtime validation capability."
        )
    return validation_evidence.to_metadata()


def _is_positive_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 1


def _stable_validation_status(record: MemoryRecord, authority: str) -> str:
    if authority == "nested_learning":
        validation = record.metadata.get("validation_evidence")
        if isinstance(validation, dict):
            return str(validation.get("validation_status") or "unresolved")
    return str(record.metadata.get("validation_status") or authority)


def _stable_record_has_valid_envelope(record: MemoryRecord) -> bool:
    if record.layer not in _STABLE_LAYERS:
        return True
    envelope = record.metadata.get("stable_write_envelope")
    if not isinstance(envelope, dict):
        return False
    source_ids = envelope.get("source_record_ids")
    if (
        envelope.get("version") != _STABLE_WRITE_ENVELOPE_VERSION
        or envelope.get("authority") not in _STABLE_WRITE_AUTHORITIES
        or envelope.get("target_layer") != record.layer.value
        or envelope.get("evidence_resolved") is not True
        or not isinstance(source_ids, list)
        or not source_ids
    ):
        return False
    evidence_locators = {ref.locator.strip() for ref in record.evidence}
    return set(str(item) for item in source_ids).issubset(evidence_locators)


def _eligible_for_conflict_detection(record: MemoryRecord) -> bool:
    if record.metadata.get("active", True) is False:
        return False
    if record.metadata.get("frame_type") in {"correction", "conflict_set"}:
        return False
    if record.layer not in {MemoryLayer.SEMANTIC, MemoryLayer.PROCEDURAL, MemoryLayer.SELF}:
        return False
    return record.kind in {MemoryKind.FACT, MemoryKind.PROCEDURE}


def _records_conflict(left: MemoryRecord, right: MemoryRecord) -> bool:
    if left.kind == MemoryKind.FACT and right.kind == MemoryKind.FACT:
        return _normalize_claim_key(left.title) == _normalize_claim_key(right.title) and _polarity(
            left.content
        ) != _polarity(right.content)
    if left.kind == MemoryKind.PROCEDURE and right.kind == MemoryKind.PROCEDURE:
        left_category = str(
            left.metadata.get("failure_category") or left.tags.get("failure_category") or ""
        )
        right_category = str(
            right.metadata.get("failure_category") or right.tags.get("failure_category") or ""
        )
        if left_category and left_category == right_category:
            return (
                SequenceMatcher(None, left.content, right.content).ratio() >= 0.85
                and left.content != right.content
            )
    return False


def _conflict_group_id(record: MemoryRecord, conflicts: list[MemoryRecord]) -> str:
    for existing in conflicts:
        group_id = existing.metadata.get("conflict_group_id")
        if group_id:
            return str(group_id)
    key = "|".join(sorted([record.id, *(item.id for item in conflicts)]))
    return f"conflict_{sha256(key.encode('utf-8')).hexdigest()[:16]}"


def _normalize_claim_key(title: str) -> str:
    return " ".join(re.findall(r"[a-zA-Z0-9_]+", title.lower()))[:80] or "untitled"


def _polarity(text: str) -> str:
    lowered = text.lower()
    negative_markers = (
        " not ",
        " never ",
        " no longer ",
        " incorrect",
        " false",
        " avoid ",
        " do not ",
    )
    return (
        "negative" if any(marker in f" {lowered} " for marker in negative_markers) else "positive"
    )


def _optional_str(value: object) -> str | None:
    if value is None:
        return None
    stripped = str(value).strip()
    return stripped or None


def _promotion_id(record: MemoryRecord) -> str | None:
    value = record.metadata.get("promotion_id")
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _promotion_entry_from_record(record: MemoryRecord, *, record_id: str) -> PromotionEntry | None:
    promotion_id = _promotion_id(record)
    nested = record.metadata.get("nested_learning")
    if not promotion_id or not isinstance(nested, dict):
        return None
    decision = nested.get("decision")
    flow = nested.get("context_flow")
    optimizer_trace = nested.get("optimizer_trace")
    if not isinstance(decision, dict) or not isinstance(flow, dict):
        return None
    source_layer_value = record.metadata.get("source_layer")
    if not source_layer_value:
        source_layers = flow.get("source_layers")
        if isinstance(source_layers, list) and source_layers:
            source_layer_value = source_layers[0]
    try:
        source_layer = MemoryLayer(str(source_layer_value or record.layer.value))
    except ValueError:
        source_layer = record.layer
    return PromotionEntry(
        promotion_id=promotion_id,
        record_id=record_id,
        source_layer=source_layer,
        target_layer=record.layer,
        decision_reason=str(decision.get("reason") or ""),
        validation_score=float(record.metadata.get("validation_score", 0.0)),
        repeat_count=int(record.metadata.get("repeat_count", 1)),
        explicit_instruction=bool(record.metadata.get("explicit_instruction", False)),
        optimizer_trace=dict(optimizer_trace) if isinstance(optimizer_trace, dict) else {},
        promoted_at=record.created_at.isoformat(),
    )


def _metadata_datetime(value: object) -> datetime | None:
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _conceptually_same_record(left: MemoryRecord, right: MemoryRecord) -> bool:
    left_category = str(
        left.metadata.get("failure_category") or left.tags.get("failure_category") or ""
    )
    right_category = str(
        right.metadata.get("failure_category") or right.tags.get("failure_category") or ""
    )
    if left_category or right_category:
        if left_category != right_category:
            return False
        return (
            SequenceMatcher(
                None, _normalize_content(left.content), _normalize_content(right.content)
            ).ratio()
            >= 0.60
        )
    if _normalize_claim_key(left.title) != _normalize_claim_key(right.title):
        return False
    return (
        SequenceMatcher(
            None, _normalize_content(left.content), _normalize_content(right.content)
        ).ratio()
        >= 0.65
    )


def _normalize_content(text: str) -> str:
    return " ".join(re.findall(r"[a-zA-Z0-9_]+", text.lower()))
