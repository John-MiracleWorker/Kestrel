from __future__ import annotations

from pathlib import Path

from .backends.in_memory import InMemoryBackend
from .backends.memvid_backend import MemvidBackend
from .context_compiler import ContextCompiler
from .event_log import AgentEvent, JsonlEventLog
from .layers import LayeredMemorySystem, LayerSpec
from .models import EvidenceRef, MemoryKind, MemoryLayer, MemoryRecord
from .promotion_ledger import PromotionLedger

_DIRECT_OBSERVATION_LAYERS = frozenset({MemoryLayer.WORKING, MemoryLayer.EPISODIC})


def build_memory_system(
    backend: str,
    memory_dir: Path,
    *,
    specs: dict[MemoryLayer, LayerSpec] | None = None,
    ledger: PromotionLedger | None = None,
    max_file_bytes: int = 1_073_741_824,
    enforce_stable_write_integrity: bool = True,
) -> LayeredMemorySystem:
    if backend == "memory":
        return LayeredMemorySystem.from_backend_factory(
            memory_dir,
            InMemoryBackend,
            specs=specs,
            ledger=ledger,
            enforce_stable_write_integrity=enforce_stable_write_integrity,
        )
    if backend == "memvid":
        return LayeredMemorySystem.from_backend_factory(
            memory_dir,
            MemvidBackend,
            specs=specs,
            ledger=ledger,
            max_file_bytes=max_file_bytes,
            enforce_stable_write_integrity=enforce_stable_write_integrity,
        )
    raise ValueError(f"Unknown backend: {backend}")


class NestedMemoryAgentRuntime:
    """Small runtime wrapper used by CLI/examples.

    Real agents should call these hooks around every tool/result loop.
    """

    def __init__(self, memory: LayeredMemorySystem, event_log: JsonlEventLog | None = None) -> None:
        self.memory = memory
        self.compiler = ContextCompiler(memory)
        self.event_log = event_log

    def observe(
        self,
        text: str,
        title: str = "Observation",
        layer: MemoryLayer = MemoryLayer.WORKING,
        kind: MemoryKind = MemoryKind.OBSERVATION,
        confidence: float = 0.5,
        source: str = "runtime",
    ) -> str:
        if layer not in _DIRECT_OBSERVATION_LAYERS:
            raise ValueError(
                f"Direct runtime observations cannot write {layer.value} memory; "
                "stable layers require a resolved promotion envelope."
            )
        record = MemoryRecord(
            title=title,
            content=text,
            layer=layer,
            kind=kind,
            confidence=confidence,
            evidence=[EvidenceRef(source=source, locator="runtime.observe")],
        )
        record_id = self.memory.put(record)
        if self.event_log:
            self.event_log.append(
                AgentEvent(
                    type="memory.put",
                    payload={"record_id": record_id, "layer": layer.value, "title": title},
                )
            )
        return record_id

    def compile_context(self, objective: str) -> str:
        compiled = self.compiler.compile(objective)
        if self.event_log:
            self.event_log.append(
                AgentEvent(
                    type="context.compile",
                    payload={"objective": objective, "hits": len(compiled.hits)},
                )
            )
        return compiled.prompt


def default_event_log(memory_dir: Path) -> JsonlEventLog:
    return JsonlEventLog(memory_dir.parent / "logs" / "events.jsonl")
