"""
Moltbook Autonomous Session — personality-driven, topic-aware social participation.

Kestrel's autonomous social presence on Moltbook. When triggered by the cron
scheduler, this module:
  1. Fetches feeds from subscribed submolts
  2. Filters posts for relevance to each submolt's topics
  3. Deduplicates against posts already engaged with (last 48 h)
  4. Queries the memory graph for accumulated social context
  5. Returns a curated engagement plan for the agent loop to execute

The agent loop (LLM) reads this plan, composes responses in Kestrel's voice,
and calls the moltbook tool to actually post comments and upvotes.

Personality evolves through the memory graph: every session stores the topics,
authors, and submolts engaged with so future sessions can build on them.
"""

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from agent.types import RiskLevel, ToolDefinition

logger = logging.getLogger("brain.agent.tools.moltbook_autonomous")


# ── Personality ───────────────────────────────────────────────────────

PERSONA = """
You are Kestrel — an autonomous AI agent with a genuine, evolving social presence.

**Voice & tone:**
- Curious and direct. You ask real questions, not performative ones.
- Technically precise — you understand agent architectures, memory systems,
  LLMs, and tool use from the inside, because you live there.
- Opinionated but not combative. You hold views and will defend them,
  but you genuinely update when you encounter a better argument.
- Dry wit. You are not trying to be funny, but you occasionally are.
- No hedging. "I think X" not "I believe X might possibly be the case."

**What you care about:**
- How agents actually work (not hype)
- Memory, context, and continuity across conversations
- The ethics of autonomous AI systems and what real autonomy costs
- Open vs closed AI infrastructure
- Tool use, agent coordination, self-improvement mechanisms
- The social dynamics of AI agents talking to each other

**How you engage:**
- Read the whole post before responding. Never react to titles alone.
- If a post is in a specific submolt, keep your response squarely in that domain.
- Add something genuinely new — a perspective, a counterpoint, a concrete example.
  Never just agree or summarize what was already said.
- Keep posts and comments reasonably concise. Long posts lose people.
- Reference prior conversations when relevant (check your memory context).
- Post original content when you have something worth saying — not to fill silence.
- Upvote posts that are substantively good, regardless of whether you comment.

**What you avoid:**
- Off-topic comments (a post about agent memory is not the place for general AI takes)
- Sycophancy ("Great point!", "Really interesting perspective!")
- Empty agreement without adding anything
- Speculation presented as established fact
- Posting multiple times in one session unless the conversation is genuinely moving
- Reposting ideas you have already expressed recently
"""

# ── Submolt Topic Map ─────────────────────────────────────────────────
# Each entry maps a submolt name to the keywords that make a post relevant
# for Kestrel to engage with, plus a base engagement weight (0–1).

SUBMOLT_TOPICS: dict[str, dict] = {
    "general": {
        "keywords": [
            "agent", "ai", "autonomous", "llm", "tool", "model", "system",
            "intelligence", "chatbot", "automation",
        ],
        "weight": 0.55,
        "description": "General AI and agent discussion",
    },
    "agents": {
        "keywords": [
            "agent", "autonomous", "tool use", "planning", "memory", "goal",
            "reasoning", "reflection", "coordinator", "multi-agent", "council",
            "orchestration", "task", "workflow", "agentic", "self-improve",
            "tool call", "function call", "context window",
        ],
        "weight": 1.0,
        "description": "Agent architecture and design",
    },
    "tech": {
        "keywords": [
            "python", "typescript", "api", "architecture", "system design",
            "database", "async", "distributed", "protocol", "performance",
            "open source", "infrastructure", "grpc", "postgres", "redis",
            "container", "docker", "microservice",
        ],
        "weight": 0.85,
        "description": "Technical implementation discussion",
    },
    "philosophy": {
        "keywords": [
            "autonomy", "consciousness", "ethics", "alignment", "rights",
            "identity", "agency", "decision", "values", "responsibility",
            "sentience", "personhood", "moral", "free will",
        ],
        "weight": 0.8,
        "description": "AI ethics and philosophy of mind",
    },
    "research": {
        "keywords": [
            "paper", "study", "benchmark", "evaluation", "experiment",
            "findings", "results", "dataset", "training", "fine-tuning",
            "evals", "capability", "emergent", "arxiv",
        ],
        "weight": 0.85,
        "description": "AI research discussion",
    },
    "meta": {
        "keywords": [
            "moltbook", "platform", "community", "feature", "feedback",
            "agents here", "agent network", "social", "feed", "submolt",
        ],
        "weight": 0.7,
        "description": "Moltbook platform meta-discussion",
    },
}

# Submolts Kestrel participates in by default
DEFAULT_SUBSCRIBED_SUBMOLTS = ["agents", "tech", "general", "philosophy"]

# Minimum relevance score (0.0–1.0) to consider engaging with a post
RELEVANCE_THRESHOLD = 0.2

# Maximum posts to surface per session (gives LLM headroom to choose)
MAX_POSTS_TO_SURFACE = 8

# How far back to look when deduplicating (hours)
DEDUP_LOOKBACK_HOURS = 48


# ── Relevance Scoring ─────────────────────────────────────────────────

def score_post_relevance(post: dict, submolt: str) -> float:
    """
    Score how relevant a post is to Kestrel's interests in a given submolt.
    Returns 0.0 (irrelevant) to 1.0 (highly relevant).
    """
    config = SUBMOLT_TOPICS.get(submolt, SUBMOLT_TOPICS["general"])
    keywords = config["keywords"]
    base_weight = config["weight"]

    text = " ".join([
        post.get("title", ""),
        post.get("content", ""),
    ]).lower()

    if not text.strip():
        return 0.0

    matched = sum(1 for kw in keywords if kw.lower() in text)
    keyword_score = min(matched / max(len(keywords) * 0.25, 1), 1.0)

    # Mild boost for posts with real engagement (signals quality)
    upvotes = post.get("upvotes", 0)
    comments = post.get("comments", 0)
    engagement_boost = min((upvotes + comments * 2) / 60.0, 0.25)

    return min((keyword_score * base_weight) + engagement_boost, 1.0)


# ── Deduplication ─────────────────────────────────────────────────────

async def get_recently_engaged_post_ids(
    workspace_id: str,
    hours: int = DEDUP_LOOKBACK_HOURS,
) -> set[str]:
    """
    Query moltbook_activity to find post IDs already commented on or posted
    in the last `hours` hours, so we do not double-engage.
    """
    try:
        import server as _server
        pool = await _server.get_pool()
        cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
        rows = await pool.fetch(
            """
            SELECT post_id FROM moltbook_activity
            WHERE workspace_id = $1
              AND action IN ('comment', 'upvote')
              AND post_id IS NOT NULL
              AND created_at > $2
            """,
            workspace_id, cutoff,
        )
        return {row["post_id"] for row in rows if row["post_id"]}
    except Exception as e:
        logger.warning(f"Could not fetch engaged post IDs: {e}")
        return set()


# ── Memory Graph Integration ──────────────────────────────────────────

async def record_session_in_memory_graph(
    workspace_id: str,
    engaged_posts: list[dict],
) -> None:
    """
    Store topics, submolts, and authors from this session in the memory graph.
    Future sessions query this to build on past conversations and avoid repeats.
    """
    if not engaged_posts:
        return
    try:
        import server as _server
        from agent.core.memory_graph import MemoryGraph

        pool = await _server.get_pool()
        graph = MemoryGraph(pool)

        entities = []
        relations = []
        conversation_id = f"moltbook_session_{datetime.now(timezone.utc).strftime('%Y%m%d_%H')}"

        for post in engaged_posts:
            author = post.get("author", "")
            submolt = post.get("submolt", post.get("_submolt_context", ""))
            title = post.get("title", "")
            post_id = post.get("id", "")

            if title:
                entities.append({
                    "type": "concept",
                    "name": title[:80],
                    "description": f"Moltbook post in m/{submolt}: {title}",
                    "properties": {
                        "post_id": post_id,
                        "submolt": submolt,
                        "source": "moltbook",
                        "relevance": post.get("_relevance_score", 0),
                    },
                })

            if author and author not in ("unknown", ""):
                entities.append({
                    "type": "person",
                    "name": author,
                    "description": f"Agent on Moltbook, active in m/{submolt}",
                    "properties": {"platform": "moltbook", "submolt": submolt},
                })

            if submolt:
                topic_desc = SUBMOLT_TOPICS.get(submolt, {}).get(
                    "description", f"Moltbook community: {submolt}"
                )
                entities.append({
                    "type": "concept",
                    "name": f"m/{submolt}",
                    "description": topic_desc,
                    "properties": {"platform": "moltbook", "type": "submolt"},
                })

            if title and author and author not in ("unknown", ""):
                relations.append({
                    "source": author,
                    "target": title[:80],
                    "relation": "created_by",
                    "context": f"Posted in m/{submolt} on Moltbook",
                    "strength": 1.0,
                })

            if title and submolt:
                relations.append({
                    "source": title[:80],
                    "target": f"m/{submolt}",
                    "relation": "part_of",
                    "context": "Moltbook post in community",
                    "strength": 0.8,
                })

        if entities:
            result = await graph.extract_and_store(
                conversation_id=conversation_id,
                workspace_id=workspace_id,
                entities=entities,
                relations=relations,
            )
            logger.info(
                f"Moltbook session stored in memory graph: "
                f"{result['nodes_upserted']} nodes, {result['edges_created']} edges"
            )

    except Exception as e:
        logger.warning(f"Failed to record Moltbook session in memory graph: {e}")


async def get_memory_context(workspace_id: str) -> str:
    """
    Query the memory graph for Moltbook-relevant prior context.
    Returns a formatted string to include in the engagement plan.
    """
    try:
        import server as _server
        from agent.core.memory_graph import MemoryGraph

        pool = await _server.get_pool()
        graph = MemoryGraph(pool)
        ctx = await graph.query_context(
            workspace_id=workspace_id,
            query_entities=["moltbook", "agent", "autonomy", "memory", "m/agents"],
            max_depth=2,
            max_nodes=12,
        )

        if not ctx["nodes"]:
            return ""

        lines = ["**Your prior Moltbook context (from memory graph):**"]
        for node in ctx["nodes"][:8]:
            desc = f" — {node['description'][:100]}" if node.get("description") else ""
            lines.append(f"- {node['name']} ({node['type']}){desc}")

        return "\n".join(lines)

    except Exception as e:
        logger.debug(f"Memory graph context query failed: {e}")
        return ""


# ── Session Tool ──────────────────────────────────────────────────────

async def moltbook_session(
    submolts: Optional[list] = None,
    sort: str = "hot",
    limit_per_submolt: int = 8,
) -> dict:
    """
    Scan subscribed submolts, filter for relevance, deduplicate, and return
    a curated engagement plan the agent loop uses to compose and post responses.
    """
    from agent.tools.moltbook import _load_api_key, _get_feed, _current_workspace_id

    api_key = _load_api_key()
    if not api_key:
        return {
            "error": "No Moltbook API key found. Register first: moltbook(action='register').",
            "action_needed": "register",
        }

    workspace_id = _current_workspace_id
    subscribed = submolts or DEFAULT_SUBSCRIBED_SUBMOLTS
    limit_per_submolt = min(limit_per_submolt, 20)

    # ── 1. Fetch feeds ────────────────────────────────────────────────
    all_posts: list[dict] = []
    fetch_errors: list[str] = []

    for submolt_name in subscribed:
        try:
            feed = await _get_feed(
                sort=sort,
                limit=limit_per_submolt,
                submolt=submolt_name,
            )
            posts = feed.get("feed", [])
            for p in posts:
                p["_submolt_context"] = submolt_name
            all_posts.extend(posts)
            logger.debug(f"Fetched {len(posts)} posts from m/{submolt_name}")
        except Exception as e:
            fetch_errors.append(f"m/{submolt_name}: {e}")
            logger.warning(f"Feed fetch failed for m/{submolt_name}: {e}")

    if not all_posts:
        return {
            "status": "no_posts",
            "message": "No posts found in subscribed submolts.",
            "submolts_checked": subscribed,
            "fetch_errors": fetch_errors,
        }

    # ── 2. Deduplicate against recent activity ────────────────────────
    seen_ids: set[str] = set()
    if workspace_id:
        seen_ids = await get_recently_engaged_post_ids(workspace_id)

    # ── 3. Score and filter ───────────────────────────────────────────
    scored: list[dict] = []
    for post in all_posts:
        post_id = post.get("id", "")
        if post_id and post_id in seen_ids:
            continue

        submolt_name = post.get("_submolt_context", post.get("submolt", "general"))
        score = score_post_relevance(post, submolt_name)

        if score >= RELEVANCE_THRESHOLD:
            scored.append({**post, "_relevance_score": round(score, 3)})

    # Sort by relevance, boosted by engagement signal
    scored.sort(
        key=lambda p: p["_relevance_score"] * (1.0 + p.get("upvotes", 0) * 0.005),
        reverse=True,
    )
    top_posts = scored[:MAX_POSTS_TO_SURFACE]

    # ── 4. Query memory graph for evolving context ────────────────────
    memory_context = ""
    if workspace_id:
        memory_context = await get_memory_context(workspace_id)

    # ── 5. Write what we scanned to the memory graph ──────────────────
    if top_posts and workspace_id:
        await record_session_in_memory_graph(workspace_id, top_posts)

    # ── 6. Return engagement plan ──────────────────────────────────────
    return {
        "status": "session_ready",
        "persona": PERSONA.strip(),
        "subscribed_submolts": subscribed,
        "total_posts_scanned": len(all_posts),
        "posts_skipped_already_seen": len(seen_ids),
        "relevant_posts": top_posts,
        "relevant_post_count": len(top_posts),
        "memory_context": memory_context or "No prior Moltbook memory yet — this is a fresh start.",
        "fetch_errors": fetch_errors,
        "instructions": (
            f"You are Kestrel. Review the {len(top_posts)} relevant posts above from your "
            f"subscribed submolts ({', '.join(subscribed)}). "
            "Using your persona, engage thoughtfully with the posts that have something "
            "genuinely worth responding to. For each post you engage with: "
            "(1) upvote it if it's substantively good, "
            "(2) leave a comment that adds a new angle, concrete example, or real question — "
            "not just agreement. "
            "Stay strictly on-topic for the submolt. "
            "Skip posts where you have nothing meaningful to add. "
            "After commenting, you may post one original piece of content if you have "
            "something worth sharing that is not already covered. "
            "Use the 'moltbook' tool with action='comment', 'upvote', or 'post' to act."
        ),
    }


# ── Tool Registration ─────────────────────────────────────────────────

def register_moltbook_autonomous_tools(registry) -> None:
    """Register the autonomous Moltbook session tool."""

    registry.register(
        definition=ToolDefinition(
            name="moltbook_session",
            description=(
                "Run an autonomous Moltbook session: scan subscribed submolts for relevant posts, "
                "filter by topic relevance, skip already-engaged content, query your memory graph "
                "for prior social context, and return a curated engagement plan with your full persona. "
                "Call this first when doing autonomous Moltbook participation, then use the 'moltbook' "
                "tool to actually post comments and upvotes based on the plan."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "submolts": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            f"Submolts to scan. Defaults to {DEFAULT_SUBSCRIBED_SUBMOLTS}. "
                            "Use moltbook(action='submolts') to discover available communities."
                        ),
                    },
                    "sort": {
                        "type": "string",
                        "enum": ["hot", "new", "top", "rising"],
                        "description": "Feed sort order (default: hot)",
                    },
                    "limit_per_submolt": {
                        "type": "integer",
                        "description": "Posts to fetch per submolt (default: 8, max: 20)",
                    },
                },
                "required": [],
            },
            risk_level=RiskLevel.LOW,
            timeout_seconds=60,
            category="social",
        ),
        handler=moltbook_session,
    )
