#!/usr/bin/env python3
from __future__ import annotations

import argparse
from pathlib import Path

from nested_memvid_agent.models import MemoryKind, MemoryLayer, MemoryRecord
from nested_memvid_agent.orchestrator import build_memory_system

SEED_RECORDS = [
    MemoryRecord(
        layer=MemoryLayer.POLICY,
        kind=MemoryKind.POLICY,
        title="Memory promotion rule",
        content="Do not promote working memory into policy memory unless the lesson has repeated validation and explicit evidence.",
        confidence=0.98,
        importance=0.95,
    ),
    MemoryRecord(
        layer=MemoryLayer.PROCEDURAL,
        kind=MemoryKind.PROCEDURE,
        title="Context compiler discipline",
        content="Before calling an LLM, compile objective, task state, relevant memories, source evidence, and next action candidates. Do not dump entire histories by default.",
        confidence=0.9,
        importance=0.85,
    ),
    MemoryRecord(
        layer=MemoryLayer.SEMANTIC,
        kind=MemoryKind.FACT,
        title="Memvid file model",
        content="Memvid v2 uses .mv2 files as portable memory capsules. Each nested memory layer can use its own .mv2 file.",
        confidence=0.9,
        importance=0.8,
    ),
]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--backend", choices=["memory", "memvid"], default="memory")
    parser.add_argument("--memory-dir", type=Path, default=Path("./memory"))
    args = parser.parse_args()

    memory = build_memory_system(args.backend, args.memory_dir)
    try:
        for record in SEED_RECORDS:
            memory.put(record)
        memory.seal_all()
        print(f"Bootstrapped {len(SEED_RECORDS)} records into {args.memory_dir} using {args.backend}")
        print(memory.verify_all())
    finally:
        memory.close_all()


if __name__ == "__main__":
    main()
