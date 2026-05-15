from __future__ import annotations

from pathlib import Path

from .backends.in_memory import InMemoryBackend
from .backends.memvid_backend import MemvidBackend
from .context_compiler import ContextCompiler
from .event_log import AgentEvent, JsonlEventLog
from .layers import LayeredMemorySystem
from .models import EvidenceRef, MemoryKind, MemoryLayer, MemoryRecord


def build_memory_system(backend: str, memory_dir: Path) -> LayeredMemorySystem:
    if backend == "memory":
        return LayeredMemorySystem.from_backend_factory(memory_dir, InMemoryBackend)
    if backend == "memvid":
        return LayeredMemorySystem.from_backend_factory(memory_dir, MemvidBackend)
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
