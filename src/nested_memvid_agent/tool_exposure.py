from __future__ import annotations

import re
from collections.abc import Sequence

from .runtime_models import ToolSpec

_TOKEN_RE = re.compile(r"[a-z0-9]+")
_DISCOVERY_TOOL = "tool.registry"
_STOP_WORDS = frozenset(
    {
        "a",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "by",
        "can",
        "do",
        "for",
        "from",
        "in",
        "is",
        "it",
        "me",
        "of",
        "on",
        "or",
        "please",
        "the",
        "this",
        "to",
        "use",
        "with",
        "you",
    }
)


def select_relevant_tool_specs(
    specs: Sequence[ToolSpec],
    *,
    objective: str,
    limit: int | None,
    preferred_names: Sequence[str] = (),
) -> list[ToolSpec]:
    """Return a deterministic, capability-filtered native-tool catalog.

    ``specs`` must already be the registry's live, capability-filtered view.  This
    function only narrows what is advertised to a provider; it never grants a
    capability and it does not participate in execution or approval decisions.
    The discovery tool is retained whenever present so a bounded catalog never
    hides the existence of Kestrel's authoritative tool registry.
    """

    active = list(specs)
    if limit is None or limit <= 0 or len(active) <= limit:
        return active

    active_names = {spec.name for spec in active}
    validated_preferred_names = frozenset(
        name for name in preferred_names if name in active_names
    )
    normalized_objective = objective.casefold()
    objective_tokens = _tokens(objective)
    ranked: list[tuple[int, int, ToolSpec]] = []
    for index, spec in enumerate(active):
        ranked.append(
            (
                _relevance_score(
                    spec,
                    normalized_objective=normalized_objective,
                    objective_tokens=objective_tokens,
                    preferred_names=validated_preferred_names,
                ),
                index,
                spec,
            )
        )

    # Python's sort is stable, but keeping the registration index in the key
    # makes the deterministic tie-break explicit for provider certification.
    ranked.sort(key=lambda item: (-item[0], item[1]))
    chosen = [item[2] for item in ranked[: max(1, limit)]]

    discovery = next((spec for spec in active if spec.name == _DISCOVERY_TOOL), None)
    if discovery is not None and all(spec.name != _DISCOVERY_TOOL for spec in chosen):
        chosen[-1] = discovery

    # Preserve registry order so provider payloads remain stable across turns.
    chosen_names = {spec.name for spec in chosen}
    return [spec for spec in active if spec.name in chosen_names]


def _relevance_score(
    spec: ToolSpec,
    *,
    normalized_objective: str,
    objective_tokens: frozenset[str],
    preferred_names: frozenset[str],
) -> int:
    score = 0
    if spec.name in preferred_names:
        score += 20_000
    canonical = spec.name.casefold()
    aliases = tuple(alias.casefold() for alias in spec.aliases)
    if _contains_public_name(normalized_objective, canonical):
        score += 10_000
    if any(_contains_public_name(normalized_objective, alias) for alias in aliases):
        score += 9_000
    if spec.name == _DISCOVERY_TOOL:
        score += 1_000

    name_tokens = _tokens(" ".join((spec.name, *spec.aliases)))
    description_tokens = _tokens(spec.description)
    capability_tokens = _tokens(" ".join(spec.capabilities))
    parameter_tokens = _tokens(" ".join(str(key) for key in spec.parameters.get("properties", {})))
    score += 120 * len(objective_tokens & name_tokens)
    score += 24 * len(objective_tokens & capability_tokens)
    score += 12 * len(objective_tokens & description_tokens)
    score += 8 * len(objective_tokens & parameter_tokens)
    return score


def _tokens(value: str) -> frozenset[str]:
    return frozenset(
        token
        for token in _TOKEN_RE.findall(value.casefold())
        if len(token) > 1 and token not in _STOP_WORDS
    )


def _contains_public_name(objective: str, name: str) -> bool:
    if not name:
        return False
    return re.search(rf"(?<![a-z0-9_]){re.escape(name)}(?![a-z0-9_])", objective) is not None
