"""
Knowledge Base Skill
Search and add documents to the local RAG knowledge base.
"""

import os


# ---------------------------------------------------------------------------
# Tool Definitions
# ---------------------------------------------------------------------------

TOOL_DEFINITIONS = [
    {
        "type": "function",
        "function": {
            "name": "knowledge_search",
            "description": "Search your local knowledge base (RAG) for relevant information. The knowledge base contains documents, notes, and snippets that have been previously added. Use this when the user asks about something that might be in their stored knowledge.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "What to search for in the knowledge base"},
                    "max_results": {"type": "integer", "description": "Maximum number of results to return (default 5)"},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "knowledge_add",
            "description": "Add a new document or snippet to the local knowledge base. Use when the user says 'remember this', 'save this to my knowledge base', 'store this information', etc.",
            "parameters": {
                "type": "object",
                "properties": {
                    "content": {"type": "string", "description": "The text content to add to the knowledge base"},
                    "title": {"type": "string", "description": "A title or label for this knowledge entry"},
                    "source": {"type": "string", "description": "Where this information came from (optional, e.g. URL, book title)"},
                },
                "required": ["content", "title"],
            },
        },
    },
]


# ---------------------------------------------------------------------------
# Tool Implementations
# ---------------------------------------------------------------------------

def tool_knowledge_search(query: str, max_results: int = 5) -> dict:
    try:
        from knowledge_base import knowledge_base
        results = knowledge_base.search(query, top_k=max_results)
        if not results:
            return {
                "query": query,
                "results": [],
                "message": "No knowledge base entries found for this query. The knowledge base may be empty or the query didn't match any stored information.",
            }
        return {"query": query, "results": results, "count": len(results)}
    except ImportError:
        return {"error": "Knowledge base module not available. Make sure knowledge_base.py exists."}
    except Exception as e:
        return {"error": f"Knowledge search failed: {str(e)}"}


def tool_knowledge_add(content: str, title: str, source: str = None) -> dict:
    try:
        from knowledge_base import knowledge_base
        result = knowledge_base.add(content, title=title, source=source)
        return {
            "status": "added",
            "title": title,
            "content_length": len(content),
            "id": result.get("id", "unknown"),
        }
    except ImportError:
        return {"error": "Knowledge base module not available. Make sure knowledge_base.py exists."}
    except Exception as e:
        return {"error": f"Failed to add to knowledge base: {str(e)}"}


# ---------------------------------------------------------------------------
# Tool Handlers
# ---------------------------------------------------------------------------

TOOL_HANDLERS = {
    "knowledge_search": lambda args: tool_knowledge_search(
        args.get("query", ""), args.get("max_results", 5)
    ),
    "knowledge_add": lambda args: tool_knowledge_add(
        args.get("content", ""), args.get("title", "Untitled"), args.get("source")
    ),
}
