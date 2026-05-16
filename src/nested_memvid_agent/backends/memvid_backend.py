from __future__ import annotations

import json
import re
from importlib import import_module
from pathlib import Path
from typing import Any

from ..context_frames import MV2ContextFrame, default_frame_type_for_memory, to_memory_record
from ..models import MemoryHit, MemoryKind, MemoryLayer, MemoryRecord
from .base import MemoryBackend


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
        **kwargs: object,
    ) -> None:
        super().__init__(path, layer, **kwargs)
        self.enable_vec = enable_vec
        self.enable_lex = enable_lex
        self.read_only = read_only
        self.mem: Any | None = None

    def open(self) -> None:
        try:
            memvid_sdk = import_module("memvid_sdk")
        except ImportError as exc:
            raise RuntimeError(
                "memvid-sdk is not installed. Run `pip install memvid-sdk` or use InMemoryBackend."
            ) from exc
        create = memvid_sdk.create
        use = memvid_sdk.use

        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists():
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
                raise RuntimeError(f"Failed to open existing Memvid memory {self.path}: {exc}") from exc
        else:
            if self.read_only:
                raise FileNotFoundError(f"Cannot open missing memory read-only: {self.path}")
            try:
                self.mem = create(str(self.path), enable_vec=self.enable_vec, enable_lex=self.enable_lex)
            except Exception as exc:  # noqa: BLE001 - backend boundary maps SDK failures
                raise RuntimeError(f"Failed to create Memvid memory {self.path}: {exc}") from exc

    def put(self, record: MemoryRecord) -> str:
        mem = self._require_mem()
        if record.layer != self.layer:
            raise ValueError(f"Cannot write {record.layer} record to {self.layer} backend")
        metadata = record.to_metadata()
        metadata.update(_context_metadata_for_record(record))
        uri = f"mv2://{record.layer.value}/{record.kind.value}/{record.id}"
        result = mem.put(
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
        if isinstance(result, str):
            return result
        if isinstance(result, list) and result:
            return str(result[0])
        return record.id

    def put_frame(self, frame: MV2ContextFrame) -> str:
        """Store a structured context frame through the existing record path."""

        return self.put(to_memory_record(frame))

    def find(self, query: str, k: int = 8, mode: str = "auto", min_relevancy: float = 0.0) -> list[MemoryHit]:
        mem = self._require_mem()
        raw = mem.find(
            query,
            mode=mode,
            k=k,
            snippet_chars=700,
            adaptive=True,
            min_relevancy=min_relevancy,
            max_k=max(k, 8),
            adaptive_strategy="combined",
        )
        hits = raw.get("hits", raw) if isinstance(raw, dict) else raw
        converted: list[MemoryHit] = []
        for item in hits:
            if not isinstance(item, dict):
                continue
            embedded_metadata = _metadata_from_embedded_text(str(item.get("text") or item.get("snippet") or ""))
            raw_metadata = item.get("metadata") or item.get("extra_metadata") or {}
            metadata = {**embedded_metadata, **raw_metadata}
            record = _record_from_hit(item=item, metadata=metadata, layer=self.layer)
            raw_score = item.get("score", item.get("relevance", 0.0))
            frame_id = item.get("frame_id") or item.get("id") or metadata.get("frame_id") or metadata.get("id")
            converted.append(
                MemoryHit(
                    record=record,
                    score=float(raw_score) if raw_score is not None else 0.0,
                    source_backend="memvid",
                    frame_id=str(frame_id) if frame_id else None,
                    snippet=str(item.get("snippet") or item.get("text") or ""),
                )
            )
        return converted

    def find_frames(
        self,
        query: str,
        k: int = 8,
        layers: tuple[MemoryLayer, ...] | None = None,
        frame_types: tuple[str, ...] | None = None,
        mode: str = "auto",
    ) -> list[MemoryHit]:
        """Find frame-backed records while keeping the backend interface stable."""

        if layers is not None and self.layer not in layers:
            return []
        hits = self.find(query=query, k=k, mode=mode)
        if frame_types is None:
            return hits
        allowed = set(frame_types)
        return [hit for hit in hits if str(hit.record.metadata.get("frame_type", "raw_chunk")) in allowed]

    def seal(self) -> None:
        mem = self._require_mem()
        seal = getattr(mem, "seal", None)
        if callable(seal):
            seal()

    def verify(self) -> bool:
        mem = self._require_mem()
        verify = getattr(mem, "verify", None)
        if callable(verify):
            result = verify(str(self.path), deep=True)
            if isinstance(result, dict):
                overall_status = result.get("overall_status")
                if isinstance(overall_status, str):
                    return overall_status == "passed"
                checks = result.get("checks")
                if isinstance(checks, list):
                    return all(isinstance(item, dict) and item.get("status") == "passed" for item in checks)
                return bool(result.get("ok", result.get("valid", True)))
            return bool(result)
        return self.path.exists()

    def stats(self) -> dict[str, Any]:
        mem = self._require_mem()
        core = getattr(mem, "_core", None)
        stats = getattr(core, "stats", None)
        if callable(stats):
            result = stats()
            if isinstance(result, dict):
                return result
            return {"ok": True, "result": result}
        return {"ok": self.path.exists(), "path": str(self.path), "stats_available": False}

    def doctor(self, *, dry_run: bool = True) -> dict[str, Any]:
        mem = self._require_mem()
        doctor = getattr(mem, "doctor", None)
        if callable(doctor):
            result = doctor(str(self.path), dry_run=dry_run, quiet=True)
            if isinstance(result, dict):
                return result
            return {"ok": bool(result), "result": result}
        return {"ok": self.path.exists(), "path": str(self.path), "doctor_available": False}

    def close(self) -> None:
        if self.mem is not None:
            close = getattr(self.mem, "close", None)
            if callable(close):
                close()
        self.mem = None

    def _require_mem(self) -> Any:
        if self.mem is None:
            raise RuntimeError("MemvidBackend.open() must be called before use")
        return self.mem


def _record_from_hit(item: dict[str, Any], metadata: dict[str, Any], layer: MemoryLayer) -> MemoryRecord:
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
        id=str(metadata.get("id") or metadata.get("frame_id") or item.get("frame_id") or item.get("id") or "memvid_hit"),
        title=str(title),
        content=str(content),
        layer=layer,
        kind=kind,
        confidence=max(0.0, min(confidence, 1.0)),
        importance=max(0.0, min(importance, 1.0)),
        metadata=dict(metadata),
    )


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
        "conflict_group_id",
        "run_id",
        "session_id",
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
    frame_type = str(metadata.get("frame_type") or default_frame_type_for_memory(record.kind, record.layer))
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
