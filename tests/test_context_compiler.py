from __future__ import annotations

from pathlib import Path

from nested_memvid_agent.backends.in_memory import InMemoryBackend
from nested_memvid_agent.context_compiler import ContextCompiler, ContextCompilerConfig
from nested_memvid_agent.layers import LayeredMemorySystem
from nested_memvid_agent.models import MemoryKind, MemoryLayer, MemoryRecord


def test_context_compiler_groups_by_layer(tmp_path: Path) -> None:
    memory = LayeredMemorySystem.from_backend_factory(
        tmp_path,
        InMemoryBackend,
        enforce_stable_write_integrity=False,
    )
    memory.put(
        MemoryRecord(
            title="Auth profile fact",
            content="Provider-specific auth profiles live inside agent auth profile files.",
            layer=MemoryLayer.SEMANTIC,
            kind=MemoryKind.FACT,
            confidence=0.9,
        )
    )
    compiler = ContextCompiler(memory, config=ContextCompilerConfig(total_budget_chars=6000))
    compiled = compiler.compile("Fix auth profile lookup")
    assert "SEMANTIC MEMORY" in compiled.prompt
    assert "Provider-specific auth profiles" in compiled.prompt
    assert compiled.hits


def test_context_compiler_respects_total_budget(tmp_path: Path) -> None:
    memory = LayeredMemorySystem.from_backend_factory(tmp_path, InMemoryBackend)
    memory.put(
        MemoryRecord(
            title="Long task memory",
            content="auth " * 3000,
            layer=MemoryLayer.WORKING,
            confidence=0.4,
        )
    )
    compiled = ContextCompiler(memory, config=ContextCompilerConfig(total_budget_chars=500)).compile("auth")
    assert compiled.total_chars <= 540
    assert "TRUNCATED_BY_CONTEXT_COMPILER" in compiled.prompt


def test_context_compiler_uses_pack_token_budget_before_char_ceiling(tmp_path: Path) -> None:
    memory = LayeredMemorySystem.from_backend_factory(
        tmp_path,
        InMemoryBackend,
        enforce_stable_write_integrity=False,
    )
    memory.put(
        MemoryRecord(
            title="Long pack memory",
            content="packbudget " + ("long content " * 1000),
            layer=MemoryLayer.SEMANTIC,
            kind=MemoryKind.FACT,
            confidence=0.9,
            metadata={"frame_type": "section_summary"},
        )
    )

    compiled = ContextCompiler(
        memory,
        config=ContextCompilerConfig(total_budget_chars=20_000, context_pack_token_budget=180),
    ).compile("packbudget")

    assert "TRUNCATED_BY_CONTEXT_PACKER" in compiled.prompt
    assert "TRUNCATED_BY_CONTEXT_COMPILER" not in compiled.prompt
