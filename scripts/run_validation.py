#!/usr/bin/env python3
from __future__ import annotations

import argparse
from pathlib import Path

from nested_memvid_agent.models import MemoryLayer
from nested_memvid_agent.orchestrator import build_memory_system
from nested_memvid_agent.validation import GoldenQuestion, RetrievalValidator, summarize_validation

GOLDEN = [
    GoldenQuestion(
        name="policy promotion guardrail",
        query="when can working memory become policy",
        expected_terms=("validation", "evidence"),
        layers=(MemoryLayer.POLICY, MemoryLayer.PROCEDURAL),
    ),
    GoldenQuestion(
        name="memvid mv2 model",
        query="what file format should layers use",
        expected_terms=(".mv2", "portable"),
        layers=(MemoryLayer.SEMANTIC,),
    ),
]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--backend", choices=["memory", "memvid"], default="memory")
    parser.add_argument("--memory-dir", type=Path, default=Path("./memory"))
    args = parser.parse_args()

    memory = build_memory_system(args.backend, args.memory_dir)
    try:
        results = RetrievalValidator(memory).run(GOLDEN)
        print(summarize_validation(results))
        if not all(result.passed for result in results):
            raise SystemExit(1)
    finally:
        memory.close_all()


if __name__ == "__main__":
    main()
