"""
Workspace and conversation CRUD operations.
"""
import uuid
import logging

from db import get_pool

logger = logging.getLogger("brain.crud")


async def list_workspaces(user_id: str) -> list:
    pool = await get_pool()
    rows = await pool.fetch(
        """SELECT w.id, w.name, w.created_at, wm.role
           FROM workspaces w
           JOIN workspace_members wm ON w.id = wm.workspace_id
           WHERE wm.user_id = $1
           ORDER BY w.created_at DESC""",
        user_id,
    )
    return [
        {"id": str(r["id"]), "name": r["name"], "role": r["role"],
         "createdAt": r["created_at"].isoformat()}
        for r in rows
    ]


async def create_workspace(user_id: str, name: str) -> dict:
    pool = await get_pool()
    ws_id = str(uuid.uuid4())
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                "INSERT INTO workspaces (id, name, created_at) VALUES ($1, $2, NOW())",
                ws_id, name,
            )
            await conn.execute(
                """INSERT INTO workspace_members (workspace_id, user_id, role, joined_at)
                   VALUES ($1, $2, 'owner', NOW())""",
                ws_id, user_id,
            )
    return {"id": ws_id, "name": name, "role": "owner"}


async def list_conversations(user_id: str, workspace_id: str) -> list:
    pool = await get_pool()
    rows = await pool.fetch(
        """SELECT id, title, created_at, updated_at
           FROM conversations
           WHERE workspace_id = $1
           ORDER BY updated_at DESC LIMIT 50""",
        workspace_id,
    )
    return [
        {"id": str(r["id"]), "title": r["title"],
         "createdAt": r["created_at"].isoformat(),
         "updatedAt": r["updated_at"].isoformat()}
        for r in rows
    ]


async def create_conversation(user_id: str, workspace_id: str) -> dict:
    pool = await get_pool()
    conv_id = str(uuid.uuid4())
    await pool.execute(
        """INSERT INTO conversations (id, workspace_id, title, created_at, updated_at)
           VALUES ($1, $2, 'New Conversation', NOW(), NOW())""",
        conv_id, workspace_id,
    )
    return {"id": conv_id, "title": "New Conversation"}


async def get_messages(user_id: str, workspace_id: str, conversation_id: str) -> list:
    pool = await get_pool()
    rows = await pool.fetch(
        """SELECT id, role, content, created_at
           FROM messages
           WHERE conversation_id = $1
           ORDER BY created_at ASC""",
        conversation_id,
    )
    return [
        {"id": str(r["id"]), "role": r["role"], "content": r["content"],
         "createdAt": r["created_at"].isoformat()}
        for r in rows
    ]


async def delete_conversation(user_id: str, workspace_id: str, conversation_id: str) -> bool:
    pool = await get_pool()
    result = await pool.execute(
        "DELETE FROM conversations WHERE id = $1 AND workspace_id = $2",
        conversation_id, workspace_id
    )
    return result != "DELETE 0"


async def update_conversation_title(user_id: str, workspace_id: str, conversation_id: str, title: str) -> dict:
    pool = await get_pool()
    row = await pool.fetchrow(
        """UPDATE conversations SET title = $1, updated_at = NOW()
           WHERE id = $2 AND workspace_id = $3
           RETURNING id, title, created_at, updated_at""",
        title, conversation_id, workspace_id
    )
    if not row:
        raise ValueError("Conversation not found")

    return {
        "id": str(row["id"]),
        "title": row["title"],
        "createdAt": row["created_at"].isoformat(),
        "updatedAt": row["updated_at"].isoformat()
    }


async def ensure_conversation(
    conversation_id: str,
    workspace_id: str,
    channel: str = "web",
    title: str = "New Conversation",
) -> None:
    """Create the conversation row if it doesn't already exist.

    External channels (Telegram, Discord, etc.) generate deterministic
    conversation IDs but never create rows in the conversations table.
    This ensures the FK from messages → conversations is satisfied.
    """
    pool = await get_pool()
    await pool.execute(
        """INSERT INTO conversations (id, workspace_id, title, channel, created_at, updated_at)
           VALUES ($1, $2, $3, $4, NOW(), NOW())
           ON CONFLICT (id) DO NOTHING""",
        conversation_id, workspace_id, title, channel,
    )


async def save_message(conversation_id: str, role: str, content: str) -> str:
    pool = await get_pool()
    msg_id = str(uuid.uuid4())
    await pool.execute(
        """INSERT INTO messages (id, conversation_id, role, content, created_at)
           VALUES ($1, $2, $3, $4, NOW())""",
        msg_id, conversation_id, role, content,
    )
    await pool.execute(
        "UPDATE conversations SET updated_at = NOW() WHERE id = $1",
        conversation_id,
    )
    return msg_id
