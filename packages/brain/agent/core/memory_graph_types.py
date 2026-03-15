from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class EntityType(str, Enum):
    PERSON = "person"
    PROJECT = "project"
    FILE = "file"
    FUNCTION = "function"
    CONCEPT = "concept"
    DECISION = "decision"
    TOOL = "tool"
    ERROR = "error"
    PREFERENCE = "preference"
    GOAL = "goal"
    OUTCOME = "outcome"


class RelationType(str, Enum):
    MENTIONED_IN = "mentioned_in"
    DECIDED_BY = "decided_by"
    DEPENDS_ON = "depends_on"
    RELATED_TO = "related_to"
    CAUSED_BY = "caused_by"
    RESOLVED_BY = "resolved_by"
    CREATED_BY = "created_by"
    MODIFIED_BY = "modified_by"
    PREFERS = "prefers"
    CONFLICTS_WITH = "conflicts_with"
    SUCCEEDED_BY = "succeeded_by"
    PART_OF = "part_of"


@dataclass
class EntityNode:
    """A node in the memory graph."""

    id: str
    entity_type: EntityType
    name: str
    description: str = ""
    properties: dict[str, Any] = field(default_factory=dict)
    weight: float = 1.0
    mention_count: int = 1
    first_seen: str = ""
    last_seen: str = ""
    source_conversation_id: str = ""
    workspace_id: str = ""

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "type": self.entity_type.value,
            "name": self.name,
            "description": self.description[:300],
            "weight": round(self.weight, 3),
            "mention_count": self.mention_count,
            "last_seen": self.last_seen,
        }


@dataclass
class RelationEdge:
    """A directed edge between two nodes."""

    id: str
    source_id: str
    target_id: str
    relation_type: RelationType
    strength: float = 1.0
    context: str = ""
    conversation_id: str = ""
    created_at: str = ""

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "source_id": self.source_id,
            "target_id": self.target_id,
            "relation": self.relation_type.value,
            "strength": round(self.strength, 3),
            "context": self.context[:200],
        }
