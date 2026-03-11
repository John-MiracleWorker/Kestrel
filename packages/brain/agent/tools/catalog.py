from __future__ import annotations

from agent.tool_catalog import ToolCatalogIndex
from agent.types import RiskLevel, ToolDefinition


SEARCH_TOOL_CATALOG_TOOL = ToolDefinition(
    name="search_tool_catalog",
    description=(
        "Search Kestrel's live tool catalog for tool names, descriptions, use cases, "
        "availability, and risk without loading full tool schemas."
    ),
    parameters={
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "What capability, tool, or use case you need.",
            },
            "limit": {
                "type": "integer",
                "description": "Maximum number of matches to return.",
                "default": 8,
            },
            "include_unavailable": {
                "type": "boolean",
                "description": "Whether to include tools that exist but are currently unavailable.",
                "default": True,
            },
        },
        "required": ["query"],
    },
    risk_level=RiskLevel.LOW,
    category="control",
    use_cases=("discover tools", "inspect tool use cases", "understand tool availability"),
)


def register_catalog_tools(registry) -> None:
    async def search_tool_catalog_handler(
        query: str,
        limit: int = 8,
        include_unavailable: bool = True,
        **kwargs,
    ) -> dict:
        catalog = registry.catalog()
        matches = catalog.search(
            query,
            limit=max(1, min(int(limit or 8), 20)),
            include_unavailable=bool(include_unavailable),
        )
        return {
            "query": query,
            "count": len(matches),
            "catalog_markdown_path": catalog.markdown_path,
            "catalog_json_path": catalog.json_path,
            "matches": [entry.to_dict() for entry in matches],
        }

    registry.register(SEARCH_TOOL_CATALOG_TOOL, search_tool_catalog_handler)
