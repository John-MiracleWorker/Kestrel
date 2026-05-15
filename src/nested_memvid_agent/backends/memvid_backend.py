from __future__ import annotations

from importlib import import_module
from pathlib import Path
from typing import Any

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
            self.mem = use("basic", str(self.path), **kwargs)
        else:
            if self.read_only:
                raise FileNotFoundError(f"Cannot open missing memory read-only: {self.path}")
            self.mem = create(str(self.path), enable_vec=self.enable_vec, enable_lex=self.enable_lex)

    def put(self, record: MemoryRecord) -> str:
        mem = self._require_mem()
        if record.layer != self.layer:
            raise ValueError(f"Cannot write {record.layer} record to {self.layer} backend")
        metadata = record.to_metadata()
        metadata.update(
            {
                "nested_layer": record.layer.value,
                "nested_kind": record.kind.value,
                "nested_confidence": record.confidence,
                "nested_importance": record.importance,
            }
        )
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
            metadata = item.get("metadata") or item.get("extra_metadata") or {}
            record = _record_from_hit(item=item, metadata=metadata, layer=self.layer)
            raw_score = item.get("score", item.get("relevance", 0.0))
            converted.append(
                MemoryHit(
                    record=record,
                    score=float(raw_score) if raw_score is not None else 0.0,
                    source_backend="memvid",
                    frame_id=str(item.get("frame_id") or item.get("id") or ""),
                    snippet=str(item.get("snippet") or item.get("text") or ""),
                )
            )
        return converted

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
    content = item.get("text") or item.get("snippet") or ""
    title = item.get("title") or metadata.get("title") or "Memvid hit"
    return MemoryRecord(
        id=str(metadata.get("id") or item.get("frame_id") or item.get("id") or "memvid_hit"),
        title=str(title),
        content=str(content),
        layer=layer,
        kind=kind,
        confidence=max(0.0, min(confidence, 1.0)),
        importance=max(0.0, min(importance, 1.0)),
        metadata=dict(metadata),
    )


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
    return sorted(tags)
