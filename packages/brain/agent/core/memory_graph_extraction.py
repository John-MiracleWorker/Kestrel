from __future__ import annotations

import json
import logging
import os

logger = logging.getLogger("brain.agent.memory_graph")


def _extract_max_tokens() -> int:
    """Token cap for LLM entity extraction (small structured JSON output)."""

    raw = os.getenv("MEMORY_GRAPH_EXTRACT_MAX_TOKENS", "768")
    try:
        value = int(raw)
    except ValueError:
        value = 768
    return max(128, min(value, 4096))


_EXTRACTION_PROMPT = """\
Extract structured entities and relationships from this conversation turn.

Entity types: file, person, project, tool, decision, error, concept
Relationship types: depends_on, related_to, caused_by, resolved_by, uses, part_of, decided

Return ONLY a JSON object with this exact structure (no markdown, no explanation):
{{"entities": [{{"type": "...", "name": "...", "description": "..."}}], "relations": [{{"source": "name1", "target": "name2", "relation": "..."}}]}}

Rules:
- Extract 1-{max_entities} entities maximum, only genuinely important ones
- Names should be concise (1-4 words)
- Skip generic words like "The", "System", "Data"
- File entities should be actual filenames (e.g. "server.py")
- Person entities should be actual names or roles
- Decision entities describe choices made (e.g. "Use gRPC over REST")
- Error entities describe bugs or failures
- Only create relations between entities you extracted

USER: {user_message}

ASSISTANT: {assistant_response}"""


def _compute_max_entities(user_message: str, assistant_response: str) -> int:
    """Dynamically compute entity extraction limit based on conversation richness."""

    combined_length = len(user_message) + len(assistant_response)
    if combined_length < 200:
        return 3
    if combined_length < 800:
        return 5
    if combined_length < 2000:
        return 8
    return 12


async def extract_entities_llm(
    provider,
    model: str,
    api_key: str,
    user_message: str,
    assistant_response: str,
) -> tuple[list[dict], list[dict]]:
    """
    Use a lightweight LLM call to extract structured entities and relations
    from a conversation turn.

    Returns (entities, relations) suitable for MemoryGraph.extract_and_store().
    """

    user_msg = user_message[:800]
    asst_msg = assistant_response[:1200]

    max_entities = _compute_max_entities(user_msg, asst_msg)
    prompt = _EXTRACTION_PROMPT.format(
        max_entities=max_entities,
        user_message=user_msg,
        assistant_response=asst_msg,
    )

    try:
        chunks = []
        async for token in provider.stream(
            messages=[{"role": "user", "content": prompt}],
            model=model,
            temperature=0.1,
            max_tokens=_extract_max_tokens(),
            api_key=api_key,
        ):
            if isinstance(token, str):
                chunks.append(token)

        raw = "".join(chunks).strip()
        logger.debug("LLM raw response (%s chars): %s", len(raw), raw[:200])

        if not raw:
            logger.warning("LLM entity extraction returned empty response")
            return [], []

        if "```" in raw:
            import re as _re

            fence_match = _re.search(r"```(?:json)?\s*\n?(.*?)```", raw, _re.DOTALL)
            if fence_match:
                raw = fence_match.group(1).strip()
            elif raw.startswith("```"):
                raw = raw.split("\n", 1)[-1]
                if raw.endswith("```"):
                    raw = raw[:-3]
                raw = raw.strip()

        if not raw.startswith("{"):
            json_start = raw.find("{")
            if json_start >= 0:
                raw = raw[json_start:]

        data = json.loads(raw)
        entities = data.get("entities", [])
        relations = data.get("relations", [])

        valid_types = {"file", "person", "project", "tool", "decision", "error", "concept"}
        entities = [
            entity
            for entity in entities
            if isinstance(entity, dict) and entity.get("type") in valid_types and entity.get("name")
        ]

        entity_names = {entity["name"] for entity in entities}
        relations = [
            relation
            for relation in relations
            if isinstance(relation, dict)
            and relation.get("source") in entity_names
            and relation.get("target") in entity_names
        ]

        logger.info("LLM extracted %s entities, %s relations", len(entities), len(relations))
        return entities, relations

    except json.JSONDecodeError as error:
        logger.warning("LLM entity extraction JSON parse failed: %s", error)
        logger.debug("Raw response was: %s", raw[:300] if raw else "(empty)")
        return [], []
    except Exception as error:
        logger.warning("LLM entity extraction failed: %s", error)
        return [], []
