from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass

from .context_packer import ContextPacker, ContextPackRequest
from .layers import DEFAULT_LAYER_SPECS, LayeredMemorySystem, LayerSpec
from .models import CompiledContext, MemoryHit, MemoryLayer


@dataclass(frozen=True)
class ContextCompilerConfig:
    total_budget_chars: int = 18_000
    context_pack_token_budget: int = 6000
    expand_raw: bool = False
    include_evidence: bool = True
    include_scores: bool = True
    max_hits_per_layer: int = 8


class ContextCompiler:
    """Compiles nested memories into the small prompt handed to an LLM."""

    def __init__(
        self,
        memory: LayeredMemorySystem,
        specs: dict[MemoryLayer, LayerSpec] | None = None,
        config: ContextCompilerConfig | None = None,
    ) -> None:
        self.memory = memory
        self.specs = specs or DEFAULT_LAYER_SPECS
        self.config = config or ContextCompilerConfig()
        self.packer = ContextPacker(memory)

    def compile(
        self,
        objective: str,
        query: str | None = None,
        *,
        excluded_record_ids: frozenset[str] = frozenset(),
        include_objective: bool = True,
        include_telemetry: bool = True,
    ) -> CompiledContext:
        packed = self.packer.pack(
            ContextPackRequest(
                objective=objective,
                query=query,
                token_budget=max(self.config.context_pack_token_budget, 1),
                allowed_layers=tuple(MemoryLayer),
                expand_raw=self.config.expand_raw,
                include_objective=include_objective,
                include_telemetry=include_telemetry,
                k_per_layer=self.config.max_hits_per_layer,
                excluded_record_ids=excluded_record_ids,
            )
        )
        selected = list(packed.hits)
        prompt = packed.prompt
        if len(prompt) > self.config.total_budget_chars:
            prompt = prompt[: self.config.total_budget_chars] + "\n\n[TRUNCATED_BY_CONTEXT_COMPILER]"
        return CompiledContext(
            objective=objective,
            prompt=prompt,
            hits=tuple(selected),
            total_chars=len(prompt),
            budget_chars=self.config.total_budget_chars,
            warnings=packed.conflict_warnings,
        )

    def _select_hits(self, hits: list[MemoryHit]) -> list[MemoryHit]:
        by_layer: dict[MemoryLayer, list[MemoryHit]] = defaultdict(list)
        for hit in hits:
            by_layer[hit.record.layer].append(hit)

        selected: list[MemoryHit] = []
        for layer in MemoryLayer:
            layer_hits = sorted(
                by_layer.get(layer, []),
                key=lambda hit: (hit.score, hit.record.importance, hit.record.confidence),
                reverse=True,
            )
            budget = self.specs[layer].context_budget_chars
            used = 0
            count = 0
            for hit in layer_hits:
                if count >= self.config.max_hits_per_layer:
                    break
                text = self._hit_text(hit)
                if used + len(text) > budget and count > 0:
                    break
                selected.append(hit)
                used += len(text)
                count += 1
        return selected

    def _render(self, objective: str, hits: list[MemoryHit]) -> str:
        lines = [
            "# COMPILED NESTED MEMORY CONTEXT",
            "",
            "## Current objective",
            objective.strip(),
            "",
            "## Operating rule",
            "Use these memories as evidence, not as unquestionable truth. Prefer validated, high-confidence, high-layer memories over noisy working memory. When memory conflicts, cite the conflict and ask for validation or inspect primary evidence.",
            "",
        ]
        for layer in MemoryLayer:
            layer_hits = [hit for hit in hits if hit.record.layer == layer]
            if not layer_hits:
                continue
            spec = self.specs[layer]
            lines.extend([f"## {layer.value.upper()} MEMORY", spec.description, ""])
            for idx, hit in enumerate(layer_hits, start=1):
                lines.append(self._format_hit(idx, hit))
                lines.append("")
        lines.extend(
            [
                "## Next-step instruction",
                "Answer or act using only the context that is relevant to the objective. If a required fact is absent, retrieve or inspect before inventing. Save new lessons only after validation.",
            ]
        )
        return "\n".join(lines).strip()

    def _format_hit(self, idx: int, hit: MemoryHit) -> str:
        record = hit.record
        header = f"{idx}. {record.title}"
        meta = []
        if self.config.include_scores:
            meta.append(f"score={hit.score:.3f}")
            meta.append(f"confidence={record.confidence:.2f}")
            meta.append(f"importance={record.importance:.2f}")
        meta.append(f"kind={record.kind.value}")
        evidence = ""
        if self.config.include_evidence and record.evidence:
            refs = "; ".join(f"{ref.source}:{ref.locator}" for ref in record.evidence)
            evidence = f"\n   evidence: {refs}"
        snippet = hit.snippet or record.content
        return f"{header} ({', '.join(meta)})\n   {snippet}{evidence}"

    @staticmethod
    def _hit_text(hit: MemoryHit) -> str:
        return f"{hit.record.title}\n{hit.snippet or hit.record.content}"
