from __future__ import annotations

import json
import os
import re
from collections.abc import Iterable
from datetime import UTC, datetime
from hashlib import sha256
from importlib import import_module
from pathlib import Path
from threading import Lock, RLock
from typing import Any
from urllib.parse import quote, unquote, urlsplit

from ..context_frames import MV2ContextFrame, default_frame_type_for_memory, to_memory_record
from ..file_lock import lock_exclusive, lock_shared, unlock
from ..models import EvidenceRef, MemoryHit, MemoryKind, MemoryLayer, MemoryRecord
from ..private_artifacts import (
    ensure_private_directory,
    harden_memory_artifact_files,
    harden_private_file,
    open_private_file_descriptor,
    write_private_text,
)
from .base import MemoryBackend, MemorySearchPage

_PATH_LOCKS: dict[Path, Lock] = {}
_PATH_LOCKS_GUARD = Lock()
_TOKEN_RE = re.compile(r"[a-zA-Z0-9_]+")
_EXACT_CACHE_SCHEMA_VERSION = 2
_CANONICAL_EVENT_SCHEMA_VERSION = 1
_CANONICAL_EVENT_METADATA_KEY = "kestrel_canonical_event"
_CANONICAL_EVENT_DIGEST_KEY = "kestrel_canonical_event_sha256"
_CANONICAL_EVENT_SCHEMA_KEY = "kestrel_canonical_event_schema"
_CANONICAL_TIMELINE_BATCH_SIZE = 256


class MemvidLockError(RuntimeError):
    """Raised when another live backend owns conflicting access to a layer."""


class MemvidBackend(MemoryBackend):
    """Memvid `.mv2` backend for one nested memory layer.

    Import is delayed so the scaffold can run tests without memvid-sdk installed.
    Codex should harden this adapter against the exact installed SDK version.
    """

    def __init__(
        self,
        path: Path,
        layer: MemoryLayer,
        enable_vec: bool = False,
        enable_lex: bool = True,
        read_only: bool = False,
        path_lock_blocking: bool = True,
        max_file_bytes: int = 1_073_741_824,
        **kwargs: object,
    ) -> None:
        super().__init__(path, layer, **kwargs)
        self.enable_vec = enable_vec
        self.enable_lex = enable_lex
        self.read_only = read_only
        self.path_lock_blocking = path_lock_blocking
        self.max_file_bytes = max(1, max_file_bytes)
        self.mem: Any | None = None
        self._path_lock: Lock | None = None
        self._lock_acquired = False
        self._memory_lock_handle: Any | None = None
        self._layer_lock_handle: Any | None = None
        # A live agent may reach one layer from concurrent internal callbacks.
        # Memvid's Python handle and Kestrel's exact record/cache state are
        # therefore serialized at the backend boundary. RunManager separately
        # admits only one Memvid-backed agent lifecycle at a time.
        self._operation_lock = RLock()
        self._records: dict[str, MemoryRecord] = {}
        self._inactive_ids: set[str] = set()
        self._canonical_event_count = 0
        self._last_canonical_event_digest: str | None = None
        self._canonical_chain_started = False
        self._saw_unchained_canonical_event = False
        self._index_path = self.path.with_suffix(f"{self.path.suffix}.records.json")

    def open(self) -> None:
        with self._operation_lock:
            ensure_private_directory(self.path.parent)
            self._acquire_path_lock()
            try:
                self._acquire_memory_file_lock()
                self._acquire_layer_file_lock()
                self._open_unlocked()
            except BaseException:
                # A partially opened SDK handle is still an exclusive owner.
                # If its close fails, retain both the handle and all locks so a
                # later retry (or process exit) is required before reuse.
                self._close_live_handle_unlocked()
                self._release_layer_file_lock()
                self._release_memory_file_lock()
                self._release_path_lock()
                raise

    def _open_unlocked(self) -> None:
        ensure_private_directory(self.path.parent)
        path_exists = harden_memory_artifact_files(self.path)
        try:
            memvid_sdk = import_module("memvid_sdk")
        except ModuleNotFoundError as exc:
            if exc.name != "memvid_sdk":
                raise RuntimeError(
                    f"memvid-sdk import failed because a dependency is missing: {exc}"
                ) from exc
            raise RuntimeError(
                "memvid-sdk is not installed. Run `pip install memvid-sdk` or use InMemoryBackend."
            ) from exc
        except ImportError as exc:
            raise RuntimeError(f"memvid-sdk import failed: {exc}") from exc
        create = memvid_sdk.create
        use = memvid_sdk.use

        if not path_exists:
            # Re-check immediately before SDK creation. The .mv2 itself must
            # remain absent: Memvid create(path) is never called on an existing
            # container and Kestrel never precreates an .mv2 placeholder.
            path_exists = harden_private_file(self.path, missing_ok=True)
        if path_exists:
            kwargs: dict[str, Any] = {}
            if self.read_only:
                kwargs["read_only"] = True
            try:
                self.mem = use("basic", str(self.path), **kwargs)
            except TypeError:
                if not self.read_only:
                    raise
                self.mem = use("basic", str(self.path))
            except Exception as exc:  # noqa: BLE001 - backend boundary reports corrupt/unreadable memory
                raise RuntimeError(
                    f"Failed to open existing Memvid memory {self.path}: {exc}"
                ) from exc
        else:
            if self.read_only:
                raise FileNotFoundError(f"Cannot open missing memory read-only: {self.path}")
            try:
                self.mem = create(
                    str(self.path), enable_vec=self.enable_vec, enable_lex=self.enable_lex
                )
            except Exception as exc:  # noqa: BLE001 - backend boundary maps SDK failures
                try:
                    harden_private_file(self.path, missing_ok=True)
                except Exception as hardening_exc:
                    raise hardening_exc from exc
                raise RuntimeError(f"Failed to create Memvid memory {self.path}: {exc}") from exc
            try:
                harden_private_file(self.path)
            except Exception:
                close = getattr(self.mem, "close", None)
                if callable(close):
                    close()
                self.mem = None
                raise
        self._load_exact_index()

    def put(self, record: MemoryRecord) -> str:
        with self._operation_lock:
            return self._put_record_unlocked(record)

    def _put_record_unlocked(
        self,
        record: MemoryRecord,
        *,
        canonical_event: dict[str, Any] | None = None,
        apply_event: bool = True,
        persist_cache: bool = True,
    ) -> str:
        if self.read_only:
            raise RuntimeError(f"Cannot write read-only Memvid memory: {self.path}")
        if record.layer != self.layer:
            raise ValueError(f"Cannot write {record.layer} record to {self.layer} backend")
        event = self._next_canonical_event_unlocked(
            canonical_event
            or _canonical_record_event(
                record,
                active=record.metadata.get("active", True) is not False,
            )
        )
        event_json = _canonical_event_json(event)
        event_digest = sha256(event_json.encode("utf-8")).hexdigest()
        harden_private_file(self.path, missing_ok=True)
        current_size = self.path.stat().st_size if self.path.exists() else 0
        estimated_write = (
            len(record.title.encode("utf-8"))
            + len(record.content.encode("utf-8"))
            + len(event_json.encode("utf-8"))
        )
        if current_size + estimated_write > self.max_file_bytes:
            raise RuntimeError(
                f"Memvid layer capacity exceeded for {self.layer.value}: "
                f"limit={self.max_file_bytes} bytes"
            )
        mem = self._require_mem()
        metadata = record.to_metadata()
        metadata.update(_context_metadata_for_record(record))
        metadata.update(
            {
                _CANONICAL_EVENT_METADATA_KEY: event_json,
                _CANONICAL_EVENT_DIGEST_KEY: event_digest,
                _CANONICAL_EVENT_SCHEMA_KEY: _CANONICAL_EVENT_SCHEMA_VERSION,
            }
        )
        # Memvid's frame(uri) resolves the newest frame for a URI. A digest
        # query makes distinct state transitions independently enumerable while
        # preserving the record ID as the final path component for search-hit
        # normalization. Identical replayed events may share a URI safely.
        encoded_id = quote(record.id, safe="")
        uri = (
            f"mv2://{record.layer.value}/{record.kind.value}/{encoded_id}"
            f"?kestrel_event={event_digest}"
        )
        try:
            mem.put(
                record.title,
                record.layer.value,
                metadata,
                text=record.content,
                uri=uri,
                tags=_record_tags(record),
                labels=[record.kind.value],
                track=record.layer.value,
                kind=record.kind.value,
                enable_embedding=self.enable_vec,
            )
        finally:
            harden_private_file(self.path, missing_ok=True)
        if apply_event:
            self._apply_canonical_event(event)
        if persist_cache:
            self._persist_exact_index()
        # The MemoryBackend contract returns the logical MemoryRecord ID.  The
        # Memvid SDK return value is a physical frame identifier and cannot be
        # used by get_record(), evidence binding, tombstones, or corrections.
        return record.id

    def upsert(self, record: MemoryRecord) -> str:
        with self._operation_lock:
            return self._put_record_unlocked(record)

    def tombstone(self, record_id: str, *, reason: str, superseded_by: str | None = None) -> bool:
        with self._operation_lock:
            if self.read_only:
                raise RuntimeError(f"Cannot write read-only Memvid memory: {self.path}")
            tombstoned_at = datetime.now(UTC)
            retention_tombstone = reason == "retention_compacted"
            audit = MemoryRecord(
                id=f"tombstone_{record_id}",
                title=f"Tombstone: {record_id}",
                content=(
                    f"Record {record_id} was tombstoned. "
                    f"reason={reason} superseded_by={superseded_by or ''}"
                ),
                layer=self.layer,
                kind=MemoryKind.CORRECTION,
                confidence=0.8,
                importance=0.4,
                metadata={
                    "frame_type": "trace_stub" if retention_tombstone else "correction",
                    "active": True,
                    "tombstone_for": record_id,
                    "tombstone_reason": reason,
                    "tombstoned_at": tombstoned_at.isoformat(),
                    "superseded_by": superseded_by,
                    "retention_artifact": retention_tombstone,
                    "retrieval_artifact": retention_tombstone,
                },
            )
            event = _canonical_tombstone_event(
                audit,
                target_id=record_id,
                reason=reason,
                superseded_by=superseded_by,
                tombstoned_at=tombstoned_at,
            )
            # Apply the inactive state only after the authoritative tombstone
            # frame has been committed to the .mv2 container.
            self._put_record_unlocked(audit, canonical_event=event)
            return True

    def iter_records(self, *, include_inactive: bool = False) -> Iterable[MemoryRecord]:
        with self._operation_lock:
            return tuple(
                record
                for record in self._records.values()
                if include_inactive or _record_active(record, inactive_ids=self._inactive_ids)
            )

    def get_record(self, record_id: str, *, include_inactive: bool = True) -> MemoryRecord | None:
        with self._operation_lock:
            record = self._records.get(record_id)
            if record is not None:
                if include_inactive or _record_active(record, inactive_ids=self._inactive_ids):
                    return record
                return None
            for indexed_record in self._records.values():
                if str(indexed_record.metadata.get("frame_id", "")) != record_id:
                    continue
                if include_inactive or _record_active(
                    indexed_record, inactive_ids=self._inactive_ids
                ):
                    return indexed_record
                return None
            return None

    def put_frame(self, frame: MV2ContextFrame) -> str:
        """Store a structured context frame through the existing record path."""

        return self.put(to_memory_record(frame))

    def find(
        self,
        query: str,
        k: int = 8,
        mode: str = "auto",
        min_relevancy: float = 0.0,
        *,
        include_inactive: bool = False,
    ) -> list[MemoryHit]:
        return list(
            self.find_page(
                query=query,
                k=k,
                mode=mode,
                min_relevancy=min_relevancy,
                include_inactive=include_inactive,
            ).hits
        )

    def find_page(
        self,
        query: str,
        k: int = 64,
        mode: str = "auto",
        min_relevancy: float = 0.0,
        *,
        include_inactive: bool = False,
        cursor: str | None = None,
    ) -> MemorySearchPage:
        with self._operation_lock:
            return self._find_page_unlocked(
                query=query,
                k=k,
                mode=mode,
                min_relevancy=min_relevancy,
                include_inactive=include_inactive,
                cursor=cursor,
            )

    def _find_page_unlocked(
        self,
        query: str,
        k: int = 64,
        mode: str = "auto",
        min_relevancy: float = 0.0,
        *,
        include_inactive: bool = False,
        cursor: str | None = None,
    ) -> MemorySearchPage:
        if k <= 0:
            return MemorySearchPage(hits=())
        mem = self._require_mem()
        try:
            raw = mem.find(
                query,
                mode=mode,
                k=k,
                snippet_chars=700,
                cursor=cursor,
                adaptive=True,
                min_relevancy=min_relevancy,
                max_k=max(k, 8),
                adaptive_strategy="combined",
            )
        except Exception as exc:
            if _is_index_disabled_error(exc):
                offset = _exact_fallback_offset(cursor)
                end = offset + k
                fallback_hits = self._find_exact_index_fallback(
                    query=query,
                    k=end + 1,
                    min_relevancy=min_relevancy,
                    include_inactive=include_inactive,
                )
                return MemorySearchPage(
                    hits=tuple(fallback_hits[offset:end]),
                    next_cursor=(f"exact-offset:{end}" if len(fallback_hits) > end else None),
                )
            raise
        hits = raw.get("hits", raw) if isinstance(raw, dict) else raw
        converted: list[MemoryHit] = []
        for item in hits:
            if not isinstance(item, dict):
                continue
            embedded_metadata = _metadata_from_embedded_text(
                str(item.get("text") or item.get("snippet") or "")
            )
            raw_metadata = item.get("metadata") or item.get("extra_metadata") or {}
            metadata = {**embedded_metadata, **raw_metadata}
            record = _record_from_hit(item=item, metadata=metadata, layer=self.layer)
            record = self._exact_record_for_hit(record, item=item) or record
            if not include_inactive and not _record_active(record, inactive_ids=self._inactive_ids):
                continue
            raw_score = item.get("score", item.get("relevance", 0.0))
            frame_id = (
                item.get("frame_id")
                or item.get("id")
                or metadata.get("frame_id")
                or metadata.get("id")
            )
            converted.append(
                MemoryHit(
                    record=record,
                    score=float(raw_score) if raw_score is not None else 0.0,
                    source_backend="memvid",
                    frame_id=str(frame_id) if frame_id else None,
                    snippet=str(item.get("snippet") or item.get("text") or ""),
                )
            )
        next_cursor = raw.get("next_cursor") if isinstance(raw, dict) else None
        return MemorySearchPage(
            hits=tuple(converted),
            next_cursor=str(next_cursor) if next_cursor not in {None, ""} else None,
        )

    def _find_exact_index_fallback(
        self,
        *,
        query: str,
        k: int,
        min_relevancy: float,
        include_inactive: bool,
    ) -> list[MemoryHit]:
        query_tokens = set(_tokens(query))
        if not query_tokens:
            return []
        scored: list[MemoryHit] = []
        for record in self._records.values():
            if not include_inactive and not _record_active(record, inactive_ids=self._inactive_ids):
                continue
            haystack = f"{record.title} {record.content} {' '.join(record.tags.values())}"
            record_tokens = set(_tokens(haystack))
            if not record_tokens:
                continue
            overlap = query_tokens & record_tokens
            if not overlap:
                continue
            score = len(overlap) / max(len(query_tokens), 1)
            if score < min_relevancy:
                continue
            scored.append(
                MemoryHit(
                    record=record,
                    score=score,
                    source_backend="memvid_exact_fallback",
                    frame_id=str(record.metadata.get("frame_id") or record.id),
                    snippet=_snippet(record.content, overlap),
                )
            )
        return sorted(scored, key=lambda hit: hit.score, reverse=True)[:k]

    def find_frames(
        self,
        query: str,
        k: int = 8,
        layers: tuple[MemoryLayer, ...] | None = None,
        frame_types: tuple[str, ...] | None = None,
        mode: str = "auto",
        include_inactive: bool = False,
    ) -> list[MemoryHit]:
        """Find frame-backed records while keeping the backend interface stable."""

        if layers is not None and self.layer not in layers:
            return []
        hits = self.find(query=query, k=k, mode=mode, include_inactive=include_inactive)
        if frame_types is None:
            return hits
        allowed = set(frame_types)
        return [
            hit
            for hit in hits
            if str(hit.record.metadata.get("frame_type", "raw_chunk")) in allowed
        ]

    def seal(self) -> None:
        with self._operation_lock:
            mem = self._require_mem()
            seal = getattr(mem, "seal", None)
            try:
                if callable(seal):
                    seal()
                self._persist_exact_index()
            finally:
                harden_memory_artifact_files(self.path)

    def verify(self) -> bool:
        with self._operation_lock:
            mem = self._require_mem()
            verify = getattr(mem, "verify", None)
            if callable(verify):
                # The SDK verifier opens the .mv2 file internally. Close this live
                # handle while keeping the in-process path lock so another local
                # request cannot claim the same file before verification finishes.
                close = getattr(mem, "close", None)
                if callable(close):
                    close()
                self.mem = None
                try:
                    result = verify(str(self.path), deep=True)
                    return _verify_result_to_bool(result)
                finally:
                    try:
                        self._open_unlocked()
                    except BaseException:
                        self._close_live_handle_unlocked()
                        self._release_layer_file_lock()
                        self._release_memory_file_lock()
                        self._release_path_lock()
                        raise
            return self.path.exists()

    def stats(self) -> dict[str, Any]:
        with self._operation_lock:
            return self._stats_unlocked()

    def _stats_unlocked(self) -> dict[str, Any]:
        mem = self._require_mem()
        direct_stats = getattr(mem, "stats", None)
        if callable(direct_stats):
            result = direct_stats()
            if isinstance(result, dict):
                return result
            return {"ok": True, "result": result}
        core = getattr(mem, "_core", None)
        stats = getattr(core, "stats", None)
        if callable(stats):
            result = stats()
            if isinstance(result, dict):
                return result
            return {"ok": True, "result": result}
        return {"ok": self.path.exists(), "path": str(self.path), "stats_available": False}

    def doctor(self, *, dry_run: bool = True) -> dict[str, Any]:
        with self._operation_lock:
            mem = self._require_mem()
            doctor = getattr(mem, "doctor", None)
            if callable(doctor):
                result = doctor(str(self.path), dry_run=dry_run, quiet=True)
                if isinstance(result, dict):
                    return result
                return {"ok": bool(result), "result": result}
            return {"ok": self.path.exists(), "path": str(self.path), "doctor_available": False}

    def close(self) -> None:
        with self._operation_lock:
            # Do not release exclusivity until the SDK confirms that its live
            # handle closed. A failed close remains retryable and fail-closed.
            self._close_live_handle_unlocked()
            try:
                harden_memory_artifact_files(self.path)
            finally:
                self._release_layer_file_lock()
                self._release_memory_file_lock()
                self._release_path_lock()

    def _close_live_handle_unlocked(self) -> None:
        mem = self.mem
        if mem is None:
            return
        close = getattr(mem, "close", None)
        if callable(close):
            close()
        # Assignment deliberately follows the SDK call: an exception retains
        # the exact handle required for a later bounded shutdown retry.
        self.mem = None

    def _acquire_memory_file_lock(self) -> None:
        if self._memory_lock_handle is not None:
            return
        lock_path = self.path.parent.parent / f".{self.path.parent.name}.kestrel-memory.lock"
        descriptor = open_private_file_descriptor(lock_path)
        handle = os.fdopen(descriptor, "r+")
        try:
            lock_shared(handle)
        except Exception:
            handle.close()
            raise
        self._memory_lock_handle = handle

    def _release_memory_file_lock(self) -> None:
        handle = self._memory_lock_handle
        if handle is None:
            return
        try:
            unlock(handle)
        finally:
            handle.close()
            self._memory_lock_handle = None

    def _acquire_layer_file_lock(self) -> None:
        if self._layer_lock_handle is not None:
            return
        lock_path = self.path.parent / f".{self.path.name}.kestrel.lock"
        descriptor = open_private_file_descriptor(lock_path)
        handle = os.fdopen(descriptor, "r+")
        try:
            if self.read_only:
                lock_shared(handle, blocking=False)
            else:
                lock_exclusive(handle, blocking=False)
        except OSError as exc:
            handle.close()
            mode = "read" if self.read_only else "write"
            raise MemvidLockError(
                f"Memvid layer is already open for conflicting {mode} access: {self.path}"
            ) from exc
        except Exception:
            handle.close()
            raise
        self._layer_lock_handle = handle

    def _release_layer_file_lock(self) -> None:
        handle = self._layer_lock_handle
        if handle is None:
            return
        try:
            unlock(handle)
        finally:
            handle.close()
            self._layer_lock_handle = None

    def _require_mem(self) -> Any:
        if self.mem is None:
            raise RuntimeError("MemvidBackend.open() must be called before use")
        return self.mem

    def _acquire_path_lock(self) -> None:
        if self._lock_acquired:
            return
        resolved = self.path.resolve()
        with _PATH_LOCKS_GUARD:
            lock = _PATH_LOCKS.setdefault(resolved, Lock())
        acquired = lock.acquire(blocking=self.path_lock_blocking)
        if not acquired:
            raise MemvidLockError(f"Memvid layer is already open in this process: {self.path}")
        self._path_lock = lock
        self._lock_acquired = True

    def _release_path_lock(self) -> None:
        if not self._lock_acquired:
            return
        lock = self._path_lock
        self._path_lock = None
        self._lock_acquired = False
        if lock is not None:
            lock.release()

    def _remember_record(self, record: MemoryRecord) -> None:
        self._records[record.id] = record
        if record.metadata.get("active", True) is False:
            self._inactive_ids.add(record.id)
        else:
            self._inactive_ids.discard(record.id)

    def _exact_record_for_hit(
        self,
        record: MemoryRecord,
        *,
        item: dict[str, Any] | None = None,
    ) -> MemoryRecord | None:
        candidate_ids: list[str] = []
        uri_record_id = _record_id_from_hit_uri(item.get("uri") if item else None)
        if uri_record_id:
            candidate_ids.append(uri_record_id)
        frame_id = str(record.metadata.get("frame_id", ""))
        if frame_id:
            candidate_ids.append(frame_id)
        candidate_ids.append(record.id)
        for candidate_id in candidate_ids:
            indexed = self._records.get(candidate_id)
            if indexed is not None:
                return indexed
        for indexed_record in self._records.values():
            indexed_frame_id = str(indexed_record.metadata.get("frame_id", ""))
            if indexed_frame_id and indexed_frame_id in candidate_ids:
                return indexed_record
        return None

    def _load_exact_index(self) -> None:
        self._records = {}
        self._inactive_ids = set()
        self._canonical_event_count = 0
        self._last_canonical_event_digest = None
        self._canonical_chain_started = False
        self._saw_unchained_canonical_event = False
        cache_payload: dict[str, Any] | list[Any] | None = None
        cache_state: tuple[dict[str, MemoryRecord], set[str]] | None = None
        cache_error: Exception | None = None
        if harden_private_file(self._index_path, missing_ok=True):
            try:
                loaded = json.loads(self._index_path.read_text(encoding="utf-8"))
                if not isinstance(loaded, dict | list):
                    raise ValueError("exact-record cache must be an object or legacy list")
                cache_payload = loaded
                cache_state = _exact_cache_state(loaded, self.layer)
            except Exception as exc:  # noqa: BLE001 - disposable cache is rebuilt from .mv2 below
                cache_error = exc

        fingerprint = self._mv2_fingerprint_unlocked()
        try:
            cache_schema = (
                int(cache_payload.get("schema_version", 1))
                if isinstance(cache_payload, dict)
                else 1
            )
        except (TypeError, ValueError) as exc:
            cache_error = exc
            cache_state = None
            cache_schema = 0

        mem = self._require_mem()
        replay_supported = callable(getattr(mem, "timeline", None)) and callable(
            getattr(mem, "frame", None)
        )
        # Production Memvid opens always replay the digest-verified envelopes.
        # A cache-local checksum and a container fingerprint detect ordinary
        # staleness but cannot make JSON authoritative: both could be edited
        # together. The narrow branch below only supports deterministic unit
        # doubles that intentionally omit the v2 timeline/frame APIs.
        if (
            not replay_supported
            and fingerprint is None
            and cache_state is not None
            and cache_schema == _EXACT_CACHE_SCHEMA_VERSION
            and isinstance(cache_payload, dict)
            and _exact_cache_integrity_valid(cache_payload)
        ):
            self._records, self._inactive_ids = cache_state
            return

        # Version-one sidecars predate canonical envelopes. On the first
        # writable open, migrate their final exact state into append-only .mv2
        # events. A read-only compatibility open may consume the legacy cache,
        # but cannot make an existing container authoritative by mutation.
        if cache_state is not None and cache_schema < _EXACT_CACHE_SCHEMA_VERSION:
            self._records, self._inactive_ids = cache_state
            if self.read_only:
                return
            self._migrate_legacy_exact_cache_unlocked()
            self._persist_exact_index()
            return

        events = self._canonical_events_from_mv2_unlocked()
        if events:
            self._records = {}
            self._inactive_ids = set()
            for event in events:
                self._apply_canonical_event(event)
            if (
                self._saw_unchained_canonical_event
                and not self._canonical_chain_started
                and not self.read_only
            ):
                self._append_canonical_chain_anchor_unlocked()
            self._persist_exact_index()
            return

        frame_count = _fingerprint_frame_count(fingerprint)
        if frame_count > 0:
            detail = f": {cache_error}" if cache_error is not None else ""
            raise RuntimeError(
                "Memvid container has frames but no rebuildable Kestrel canonical events; "
                f"a legacy exact-record cache is required for one-time migration{detail}"
            )
        if cache_error is not None and fingerprint is None:
            raise RuntimeError(
                f"Failed to rebuild unreadable Memvid exact-record cache {self._index_path}: "
                f"{cache_error}"
            ) from cache_error
        self._persist_exact_index()

    def _canonical_events_from_mv2_unlocked(self) -> list[dict[str, Any]]:
        mem = self._require_mem()
        timeline = getattr(mem, "timeline", None)
        frame = getattr(mem, "frame", None)
        if not callable(timeline) or not callable(frame):
            return []
        fingerprint = self._mv2_fingerprint_unlocked()
        physical_frame_count = _fingerprint_frame_count(fingerprint)
        if fingerprint is not None and physical_frame_count == 0:
            return []

        entries: dict[int, dict[str, Any]] = {}
        as_of_frame: int | None = None
        reached_origin = False
        while not reached_origin:
            limit = _CANONICAL_TIMELINE_BATCH_SIZE
            kwargs: dict[str, Any] = {"limit": limit, "reverse": True}
            if as_of_frame is not None:
                kwargs["as_of_frame"] = as_of_frame
            used_one_shot_fallback = False
            try:
                batch = timeline(**kwargs)
            except TypeError:
                # The pinned SDK supports frame cursors. This compatibility path
                # still enumerates older v2 SDKs that accept an unbounded limit.
                one_shot_limit = max(physical_frame_count, _CANONICAL_TIMELINE_BATCH_SIZE)
                batch = timeline(limit=one_shot_limit, reverse=True)
                used_one_shot_fallback = True
            if not isinstance(batch, list):
                raise RuntimeError("Memvid timeline returned a non-list result")
            if not batch:
                if entries:
                    raise RuntimeError(
                        "Memvid logical timeline ended before origin frame 0; "
                        "canonical event replay would be truncated"
                    )
                if physical_frame_count > 0:
                    raise RuntimeError(
                        "Memvid reports physical frames but returned no logical timeline commits"
                    )
                return []
            frame_ids: list[int] = []
            for item in batch:
                if not isinstance(item, dict):
                    raise RuntimeError("Memvid timeline contains a non-object commit")
                try:
                    frame_id = int(item["frame_id"])
                except (KeyError, TypeError, ValueError) as exc:
                    raise RuntimeError(
                        "Memvid timeline commit is missing a valid frame_id"
                    ) from exc
                if frame_id < 0:
                    raise RuntimeError("Memvid timeline returned a negative frame_id")
                if physical_frame_count > 0 and frame_id >= physical_frame_count:
                    raise RuntimeError(
                        "Memvid logical timeline frame is outside the physical frame range: "
                        f"frame_id={frame_id} physical_frame_count={physical_frame_count}"
                    )
                if as_of_frame is not None and frame_id > as_of_frame:
                    raise RuntimeError(
                        "Memvid timeline pagination did not honor the descending frame cursor"
                    )
                frame_ids.append(frame_id)
            if frame_ids != sorted(frame_ids, reverse=True) or len(frame_ids) != len(
                set(frame_ids)
            ):
                raise RuntimeError("Memvid logical timeline page is not strictly descending")
            duplicate_ids = set(frame_ids) & set(entries)
            if duplicate_ids:
                raise RuntimeError(
                    "Memvid logical timeline pagination repeated frame IDs: "
                    f"{sorted(duplicate_ids)[:3]}"
                )
            entries.update(zip(frame_ids, batch, strict=True))
            lowest = min(frame_ids)
            if lowest == 0:
                reached_origin = True
                continue
            if used_one_shot_fallback:
                raise RuntimeError(
                    "Memvid timeline API cannot page to logical origin frame 0; "
                    "canonical event replay would be truncated"
                )
            as_of_frame = lowest - 1

        events: list[dict[str, Any]] = []
        for frame_id in sorted(entries):
            uri = entries[frame_id].get("uri")
            if not isinstance(uri, str) or not uri:
                raise RuntimeError(f"Memvid frame {frame_id} is missing its URI")
            try:
                frame_payload = frame(uri)
            except Exception as exc:  # noqa: BLE001 - exact replay must fail closed
                raise RuntimeError(
                    f"Failed to read Memvid frame {frame_id} ({uri}): {exc}"
                ) from exc
            event = _canonical_event_from_frame(frame_payload, frame_id=frame_id)
            if event is not None:
                events.append(event)
        return events

    def _apply_canonical_event(self, event: dict[str, Any]) -> None:
        self._advance_canonical_chain(event)
        event_kind = str(event.get("event") or "")
        record_payload = event.get("record")
        if isinstance(record_payload, dict):
            record = _record_from_index_payload(record_payload, self.layer)
            self._records[record.id] = record
            active = event.get("active", record.metadata.get("active", True)) is not False
            record.metadata["active"] = active
            if active:
                self._inactive_ids.discard(record.id)
            else:
                self._inactive_ids.add(record.id)
        if event_kind == "chain_anchor":
            return
        if event_kind not in {"tombstone", "tombstone_state"}:
            if event_kind != "record":
                raise RuntimeError(f"Unsupported Kestrel canonical event: {event_kind}")
            return

        target_id = str(event.get("target_id") or "").strip()
        if not target_id:
            raise RuntimeError("Canonical tombstone event is missing target_id")
        self._inactive_ids.add(target_id)
        target = self._records.get(target_id)
        if target is None:
            return
        target.metadata["active"] = False
        target.metadata["tombstone_reason"] = str(event.get("reason") or "unknown")
        tombstoned_at = str(event.get("tombstoned_at") or "")
        if tombstoned_at:
            target.metadata["tombstoned_at"] = tombstoned_at
            target.updated_at = _datetime_from_index(tombstoned_at) or target.updated_at
        superseded_by = event.get("superseded_by")
        if superseded_by not in {None, ""}:
            target.metadata["superseded_by"] = str(superseded_by)

    def _migrate_legacy_exact_cache_unlocked(self) -> None:
        records = tuple(self._records.values())
        inactive_ids = set(self._inactive_ids)
        for record in records:
            event = _canonical_record_event(
                record,
                active=_record_active(record, inactive_ids=inactive_ids),
            )
            self._put_record_unlocked(
                record,
                canonical_event=event,
                apply_event=True,
                persist_cache=False,
            )
        for target_id in sorted(inactive_ids - set(self._records)):
            migration_time = datetime.now(UTC)
            state_record = MemoryRecord(
                id=f"_kestrel_tombstone_state_{sha256(target_id.encode('utf-8')).hexdigest()}",
                title="Kestrel canonical tombstone state",
                content=f"Preserved inactive state for legacy record {target_id}.",
                layer=self.layer,
                kind=MemoryKind.CORRECTION,
                metadata={"active": False, "retrieval_artifact": True},
            )
            event = _canonical_tombstone_state_event(target_id, migration_time)
            self._put_record_unlocked(
                state_record,
                canonical_event=event,
                apply_event=True,
                persist_cache=False,
            )

    def _next_canonical_event_unlocked(self, event: dict[str, Any]) -> dict[str, Any]:
        if "commit_sequence" in event or "previous_event_sha256" in event:
            return dict(event)
        chained = dict(event)
        chained["commit_sequence"] = self._canonical_event_count + 1
        chained["previous_event_sha256"] = self._last_canonical_event_digest
        return chained

    def _advance_canonical_chain(self, event: dict[str, Any]) -> None:
        digest = sha256(_canonical_event_json(event).encode("utf-8")).hexdigest()
        has_sequence = "commit_sequence" in event
        has_previous = "previous_event_sha256" in event
        if not has_sequence and not has_previous:
            if self._canonical_chain_started:
                raise RuntimeError("Unchained canonical event follows a chained event")
            self._canonical_event_count += 1
            self._last_canonical_event_digest = digest
            self._saw_unchained_canonical_event = True
            return
        if not has_sequence or not has_previous:
            raise RuntimeError("Canonical event chain metadata is incomplete")
        try:
            sequence = int(event["commit_sequence"])
        except (TypeError, ValueError) as exc:
            raise RuntimeError("Canonical event commit_sequence is invalid") from exc
        expected_sequence = self._canonical_event_count + 1
        if sequence != expected_sequence:
            raise RuntimeError(
                "Canonical event sequence is truncated or out of order: "
                f"expected={expected_sequence} observed={sequence}"
            )
        previous = event.get("previous_event_sha256")
        if previous != self._last_canonical_event_digest:
            raise RuntimeError(
                f"Canonical event hash chain is truncated or out of order at sequence {sequence}"
            )
        self._canonical_event_count = sequence
        self._last_canonical_event_digest = digest
        self._canonical_chain_started = True

    def _append_canonical_chain_anchor_unlocked(self) -> None:
        anchor_time = datetime.now(UTC)
        previous = self._last_canonical_event_digest or "genesis"
        state_record = MemoryRecord(
            id=f"_kestrel_chain_anchor_{self._canonical_event_count + 1}_{previous[:16]}",
            title="Kestrel canonical event-chain anchor",
            content="Anchors legacy canonical events to a verified logical commit sequence.",
            layer=self.layer,
            kind=MemoryKind.CORRECTION,
            metadata={
                "active": False,
                "retrieval_artifact": True,
                "anchored_at": anchor_time.isoformat(),
            },
        )
        event = {
            "schema_version": _CANONICAL_EVENT_SCHEMA_VERSION,
            "event": "chain_anchor",
            "anchored_at": anchor_time.isoformat(),
        }
        self._put_record_unlocked(
            state_record,
            canonical_event=event,
            apply_event=True,
            persist_cache=False,
        )

    def _persist_exact_index(self) -> None:
        if self.read_only:
            return
        ensure_private_directory(self._index_path.parent)
        records_payload = [_record_to_index_payload(record) for record in self._records.values()]
        inactive_payload = sorted(self._inactive_ids)
        payload = {
            "schema_version": _EXACT_CACHE_SCHEMA_VERSION,
            "mv2_path": str(self.path),
            "layer": self.layer.value,
            "mv2_fingerprint": self._mv2_fingerprint_unlocked(),
            "inactive_ids": inactive_payload,
            "records": records_payload,
            "cache_sha256": _exact_cache_digest(
                layer=self.layer.value,
                records=records_payload,
                inactive_ids=inactive_payload,
            ),
        }
        write_private_text(
            self._index_path,
            json.dumps(payload, indent=2, sort_keys=True),
            encoding="utf-8",
        )

    def _mv2_fingerprint_unlocked(self) -> dict[str, int] | None:
        try:
            stats = self._stats_unlocked()
        except Exception:  # noqa: BLE001 - older/fake SDKs may not expose stats
            return None
        if "frame_count" not in stats:
            return None
        fingerprint: dict[str, int] = {}
        for key in ("frame_count", "active_frame_count", "seq_no", "size_bytes"):
            value = stats.get(key)
            if isinstance(value, bool):
                continue
            if isinstance(value, int | float):
                fingerprint[key] = int(value)
        return fingerprint or None


def _canonical_record_event(record: MemoryRecord, *, active: bool) -> dict[str, Any]:
    return {
        "schema_version": _CANONICAL_EVENT_SCHEMA_VERSION,
        "event": "record",
        "active": active,
        "record": _record_to_index_payload(record),
    }


def _canonical_tombstone_event(
    audit: MemoryRecord,
    *,
    target_id: str,
    reason: str,
    superseded_by: str | None,
    tombstoned_at: datetime,
) -> dict[str, Any]:
    return {
        "schema_version": _CANONICAL_EVENT_SCHEMA_VERSION,
        "event": "tombstone",
        "active": True,
        "record": _record_to_index_payload(audit),
        "target_id": target_id,
        "reason": reason,
        "superseded_by": superseded_by,
        "tombstoned_at": tombstoned_at.isoformat(),
    }


def _canonical_tombstone_state_event(target_id: str, tombstoned_at: datetime) -> dict[str, Any]:
    return {
        "schema_version": _CANONICAL_EVENT_SCHEMA_VERSION,
        "event": "tombstone_state",
        "target_id": target_id,
        "reason": "legacy_cache_migration",
        "superseded_by": None,
        "tombstoned_at": tombstoned_at.isoformat(),
    }


def _canonical_event_json(event: dict[str, Any]) -> str:
    return json.dumps(_json_safe(event), sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _canonical_event_from_frame(frame: Any, *, frame_id: int) -> dict[str, Any] | None:
    if not isinstance(frame, dict):
        raise RuntimeError(f"Memvid frame {frame_id} returned an invalid payload")
    metadata = frame.get("extra_metadata") or frame.get("metadata") or {}
    if not isinstance(metadata, dict):
        return None
    raw_event = metadata.get(_CANONICAL_EVENT_METADATA_KEY)
    if raw_event is None:
        return None
    event = _decode_sdk_metadata_value(raw_event)
    if not isinstance(event, dict):
        raise RuntimeError(f"Memvid frame {frame_id} has an invalid canonical event envelope")
    raw_schema_version = event.get("schema_version")
    if isinstance(raw_schema_version, bool) or not isinstance(raw_schema_version, int | str):
        raise RuntimeError(f"Memvid frame {frame_id} has no canonical event schema")
    try:
        schema_version = int(raw_schema_version)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"Memvid frame {frame_id} has no canonical event schema") from exc
    if schema_version != _CANONICAL_EVENT_SCHEMA_VERSION:
        raise RuntimeError(
            f"Unsupported canonical event schema in Memvid frame {frame_id}: {schema_version}"
        )
    metadata_schema = _decode_sdk_metadata_value(metadata.get(_CANONICAL_EVENT_SCHEMA_KEY))
    if metadata_schema is not None:
        try:
            decoded_schema = int(metadata_schema)
        except (TypeError, ValueError) as exc:
            raise RuntimeError(f"Memvid frame {frame_id} has invalid schema metadata") from exc
        if decoded_schema != schema_version:
            raise RuntimeError(f"Memvid frame {frame_id} canonical schema metadata does not match")
    expected_digest = _decode_sdk_metadata_value(metadata.get(_CANONICAL_EVENT_DIGEST_KEY))
    actual_digest = sha256(_canonical_event_json(event).encode("utf-8")).hexdigest()
    if not isinstance(expected_digest, str) or expected_digest != actual_digest:
        raise RuntimeError(f"Memvid frame {frame_id} canonical event digest does not match")
    return event


def _decode_sdk_metadata_value(value: Any) -> Any:
    """Undo the SDK's JSON wrapping while preserving ordinary string values."""

    decoded = value
    for _ in range(4):
        if not isinstance(decoded, str):
            break
        try:
            candidate = json.loads(decoded)
        except (TypeError, json.JSONDecodeError):
            break
        if candidate == decoded:
            break
        decoded = candidate
        # Memvid wraps string metadata once. Continue only when that string is
        # itself a serialized object/array (the canonical event envelope), not
        # for scalar strings such as an all-numeric SHA-256 digest.
        if not (isinstance(decoded, str) and decoded.lstrip().startswith(("{", "["))):
            break
    return decoded


def _exact_cache_state(
    payload: dict[str, Any] | list[Any],
    backend_layer: MemoryLayer,
) -> tuple[dict[str, MemoryRecord], set[str]]:
    records_payload = payload.get("records", []) if isinstance(payload, dict) else payload
    if not isinstance(records_payload, list):
        raise ValueError("records must be a list")
    if isinstance(payload, dict):
        cache_layer = payload.get("layer")
        if cache_layer not in {None, backend_layer.value}:
            raise ValueError(
                f"exact-record cache layer {cache_layer} does not match backend layer {backend_layer.value}"
            )
        inactive_payload = payload.get("inactive_ids", [])
    else:
        inactive_payload = []
    if not isinstance(inactive_payload, list):
        raise ValueError("inactive_ids must be a list")
    records: dict[str, MemoryRecord] = {}
    inactive_ids = {str(item) for item in inactive_payload}
    for item in records_payload:
        if not isinstance(item, dict):
            raise ValueError("record cache entries must be objects")
        record = _record_from_index_payload(item, backend_layer)
        records[record.id] = record
        if record.metadata.get("active", True) is False:
            inactive_ids.add(record.id)
    return records, inactive_ids


def _exact_cache_digest(
    *,
    layer: str,
    records: list[dict[str, Any]],
    inactive_ids: list[str],
) -> str:
    payload = {"layer": layer, "records": records, "inactive_ids": inactive_ids}
    serialized = json.dumps(
        _json_safe(payload), sort_keys=True, separators=(",", ":"), ensure_ascii=True
    )
    return sha256(serialized.encode("utf-8")).hexdigest()


def _exact_cache_integrity_valid(payload: dict[str, Any]) -> bool:
    records = payload.get("records")
    inactive_ids = payload.get("inactive_ids")
    expected = payload.get("cache_sha256")
    layer = payload.get("layer")
    if (
        not isinstance(records, list)
        or not isinstance(inactive_ids, list)
        or not isinstance(expected, str)
        or not isinstance(layer, str)
    ):
        return False
    return expected == _exact_cache_digest(
        layer=layer,
        records=records,
        inactive_ids=[str(item) for item in inactive_ids],
    )


def _fingerprint_frame_count(fingerprint: dict[str, int] | None) -> int:
    if fingerprint is None:
        return 0
    return max(0, int(fingerprint.get("frame_count", 0)))


def _verify_result_to_bool(result: Any) -> bool:
    if isinstance(result, dict):
        overall_status = result.get("overall_status")
        if isinstance(overall_status, str):
            return overall_status == "passed"
        checks = result.get("checks")
        if isinstance(checks, list):
            return all(
                isinstance(item, dict) and item.get("status") in {"passed", "skipped"}
                for item in checks
            )
        return bool(result.get("ok", result.get("valid", True)))
    return bool(result)


def _tokens(text: str) -> list[str]:
    return [match.group(0).lower() for match in _TOKEN_RE.finditer(text)]


def _snippet(text: str, query_tokens: set[str], window: int = 220) -> str:
    lower = text.lower()
    first_idx = min((lower.find(token) for token in query_tokens if token in lower), default=0)
    start = max(first_idx - 60, 0)
    return text[start : start + window].strip()


def _is_index_disabled_error(exc: Exception) -> bool:
    exc_name = type(exc).__name__.lower()
    message = str(exc).lower()
    return (
        "indexdisabled" in exc_name
        or "index is not enabled" in message
        or "index disabled" in message
    )


def _record_from_hit(
    item: dict[str, Any], metadata: dict[str, Any], layer: MemoryLayer
) -> MemoryRecord:
    kind_value = (
        metadata.get("nested_kind")
        or metadata.get("kind")
        or item.get("kind")
        or _kind_from_hit_collections(item)
        or MemoryKind.OBSERVATION.value
    )
    try:
        kind = MemoryKind(kind_value)
    except ValueError:
        kind = MemoryKind.OBSERVATION
    confidence = float(metadata.get("nested_confidence", metadata.get("confidence", 0.5)))
    importance = float(metadata.get("nested_importance", metadata.get("importance", 0.5)))
    content = _clean_hit_text(str(item.get("text") or item.get("snippet") or ""))
    title = item.get("title") or metadata.get("title") or "Memvid hit"
    return MemoryRecord(
        id=str(
            metadata.get("id")
            or metadata.get("frame_id")
            or item.get("frame_id")
            or item.get("id")
            or "memvid_hit"
        ),
        title=str(title),
        content=str(content),
        layer=layer,
        kind=kind,
        confidence=max(0.0, min(confidence, 1.0)),
        importance=max(0.0, min(importance, 1.0)),
        metadata=dict(metadata),
    )


def _record_to_index_payload(record: MemoryRecord) -> dict[str, Any]:
    return {
        "id": record.id,
        "title": record.title,
        "content": record.content,
        "layer": record.layer.value,
        "kind": record.kind.value,
        "tags": _json_safe(record.tags),
        "metadata": _json_safe(record.metadata),
        "evidence": [_json_safe(ref.__dict__) for ref in record.evidence],
        "confidence": record.confidence,
        "importance": record.importance,
        "created_at": record.created_at.isoformat(),
        "updated_at": record.updated_at.isoformat(),
        "expires_at": record.expires_at.isoformat() if record.expires_at else None,
    }


def _record_from_index_payload(payload: dict[str, Any], backend_layer: MemoryLayer) -> MemoryRecord:
    try:
        layer = MemoryLayer(str(payload.get("layer") or backend_layer.value))
    except ValueError:
        layer = backend_layer
    if layer != backend_layer:
        raise ValueError(f"index record layer {layer} does not match backend layer {backend_layer}")
    try:
        kind = MemoryKind(str(payload.get("kind") or MemoryKind.OBSERVATION.value))
    except ValueError:
        kind = MemoryKind.OBSERVATION
    evidence = []
    raw_evidence = payload.get("evidence", [])
    if isinstance(raw_evidence, list):
        for item in raw_evidence:
            if isinstance(item, dict) and item.get("source") and item.get("locator"):
                evidence.append(
                    EvidenceRef(
                        source=str(item["source"]),
                        locator=str(item["locator"]),
                        quote=str(item["quote"]) if item.get("quote") is not None else None,
                    )
                )
    tags = payload.get("tags", {})
    metadata = payload.get("metadata", {})
    return MemoryRecord(
        id=str(payload["id"]),
        title=str(payload["title"]),
        content=str(payload["content"]),
        layer=layer,
        kind=kind,
        tags={str(key): str(value) for key, value in tags.items()}
        if isinstance(tags, dict)
        else {},
        metadata=dict(metadata) if isinstance(metadata, dict) else {},
        evidence=evidence,
        confidence=float(payload.get("confidence", 0.5)),
        importance=float(payload.get("importance", 0.5)),
        created_at=_datetime_from_index(payload.get("created_at")) or datetime.now(UTC),
        updated_at=_datetime_from_index(payload.get("updated_at")) or datetime.now(UTC),
        expires_at=_datetime_from_index(payload.get("expires_at")),
    )


def _datetime_from_index(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed


def _json_safe(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, MemoryLayer | MemoryKind):
        return value.value
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, list | tuple | set):
        return [_json_safe(item) for item in value]
    if value is None or isinstance(value, str | int | float | bool):
        return value
    return str(value)


def _record_active(record: MemoryRecord, *, inactive_ids: set[str]) -> bool:
    return record.id not in inactive_ids and record.metadata.get("active", True) is not False


def _exact_fallback_offset(cursor: str | None) -> int:
    if cursor is None:
        return 0
    prefix = "exact-offset:"
    if not cursor.startswith(prefix):
        raise ValueError("Invalid Memvid fallback search cursor")
    try:
        offset = int(cursor.removeprefix(prefix))
    except ValueError as exc:
        raise ValueError("Invalid Memvid fallback search cursor") from exc
    if offset < 0:
        raise ValueError("Invalid Memvid fallback search cursor")
    return offset


def _metadata_from_embedded_text(text: str) -> dict[str, Any]:
    """Recover metadata from SDKs that index metadata into result text only."""

    if not text:
        return {}
    metadata: dict[str, Any] = {}
    for key in (
        "frame_id",
        "frame_type",
        "id",
        "kind",
        "layer",
        "mv2_ctx_version",
        "nested_kind",
        "nested_layer",
        "source_uri",
        "content_hash",
        "context_flow_id",
        "cognition_schema",
        "conflict_group_id",
        "failure_category",
        "run_id",
        "session_id",
        "validation_status",
    ):
        value = _quoted_value(text, key)
        if value is not None and value != "null":
            metadata[key] = value
    for key in ("confidence", "importance", "nested_confidence", "nested_importance"):
        number_value = _number_value(text, key)
        if number_value is not None:
            metadata[key] = number_value
    token_count = _int_value(text, "token_count")
    if token_count is not None:
        metadata["token_count"] = token_count
    for key in ("parent_ids", "child_ids"):
        value = _json_value(text, key)
        if isinstance(value, list):
            metadata[key] = value
    source_span = _json_value(text, "source_span")
    if isinstance(source_span, dict):
        metadata["source_span"] = source_span
    return metadata


def _clean_hit_text(text: str) -> str:
    marker = " title: "
    if marker in text:
        return text.split(marker, maxsplit=1)[0].strip()
    return text


def _record_id_from_hit_uri(value: object) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    parsed = urlsplit(value)
    if parsed.scheme != "mv2":
        return None
    record_id = unquote(parsed.path.rstrip("/").rsplit("/", maxsplit=1)[-1]).strip()
    return record_id or None


def _quoted_value(text: str, key: str) -> str | None:
    match = re.search(rf"\b{re.escape(key)}:\s+\"([^\"]*)\"", text)
    if match:
        return match.group(1)
    match = re.search(rf"\b{re.escape(key)}:\s+(null)\b", text)
    return match.group(1) if match else None


def _number_value(text: str, key: str) -> float | None:
    match = re.search(rf"\b{re.escape(key)}:\s+([0-9]+(?:\.[0-9]+)?)", text)
    return float(match.group(1)) if match else None


def _int_value(text: str, key: str) -> int | None:
    match = re.search(rf"\b{re.escape(key)}:\s+([0-9]+)", text)
    return int(match.group(1)) if match else None


def _json_value(text: str, key: str) -> Any | None:
    match = re.search(rf"\b{re.escape(key)}:\s+(\[[^\n]*\]|\{{[^\n]*\}})", text)
    if not match:
        return None
    try:
        return json.loads(match.group(1))
    except json.JSONDecodeError:
        return None


def _kind_from_hit_collections(item: dict[str, Any]) -> str | None:
    values: list[str] = []
    for key in ("tags", "labels"):
        raw = item.get(key)
        if isinstance(raw, list):
            values.extend(str(value).lower() for value in raw)
    for kind in MemoryKind:
        if kind.value in values:
            return kind.value
    return None


def _record_tags(record: MemoryRecord) -> list[str]:
    tags = {record.layer.value, record.kind.value}
    tags.update(str(value) for value in record.tags.values())
    frame_type = record.metadata.get("frame_type")
    if frame_type:
        tags.add(str(frame_type))
    return sorted(tags)


def _context_metadata_for_record(record: MemoryRecord) -> dict[str, Any]:
    metadata = dict(record.metadata)
    nested_learning = metadata.get("nested_learning")
    context_flow_id = metadata.get("context_flow_id")
    optimizer_trace = metadata.get("optimizer_trace")
    if isinstance(nested_learning, dict):
        context_flow = nested_learning.get("context_flow")
        if isinstance(context_flow, dict) and context_flow.get("id"):
            context_flow_id = context_flow["id"]
        trace = nested_learning.get("optimizer_trace")
        if isinstance(trace, dict):
            optimizer_trace = trace
    frame_type = str(
        metadata.get("frame_type") or default_frame_type_for_memory(record.kind, record.layer)
    )
    frame_id = str(metadata.get("frame_id") or record.id)
    return {
        "mv2_ctx_version": metadata.get("mv2_ctx_version", "0.1"),
        "frame_type": frame_type,
        "frame_id": frame_id,
        "parent_ids": list(_list_str(metadata.get("parent_ids"))),
        "child_ids": list(_list_str(metadata.get("child_ids"))),
        "source_uri": metadata.get("source_uri"),
        "source_span": metadata.get("source_span", {}),
        "token_count": metadata.get("token_count"),
        "content_hash": metadata.get("content_hash", record.content_hash),
        "nested_layer": record.layer.value,
        "nested_kind": record.kind.value,
        "nested_confidence": record.confidence,
        "nested_importance": record.importance,
        "context_flow_id": context_flow_id,
        "optimizer_trace": optimizer_trace,
        "conflict_group_id": metadata.get("conflict_group_id"),
        "run_id": metadata.get("run_id"),
        "session_id": metadata.get("session_id"),
    }


def _list_str(value: object) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, list | tuple):
        return tuple(str(item) for item in value)
    return (str(value),)
