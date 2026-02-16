"""
Brain Service — gRPC server wrapping the LLM engine.

Provides:
  - StreamChat: token-by-token streaming with tool-call support
  - HealthCheck: provider status
  - User management (create, authenticate)
  - Workspace / conversation CRUD
"""

import asyncio
import logging
import os
import json
import uuid
from concurrent import futures
from datetime import datetime

import grpc
from grpc import aio as grpc_aio
from dotenv import load_dotenv

# Generated protobuf stubs (will be generated from proto files)
# For now, use proto_loader approach
import grpc_tools
from google.protobuf import json_format

from providers.local import LocalProvider
from providers.cloud import CloudProvider
from memory.vector_store import VectorStore

load_dotenv()
logger = logging.getLogger("brain")
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO").upper())

# ── Configuration ─────────────────────────────────────────────────────
GRPC_PORT = int(os.getenv("BRAIN_GRPC_PORT", "50051"))
GRPC_HOST = os.getenv("BRAIN_GRPC_HOST", "0.0.0.0")
DB_URL = os.getenv(
    "DATABASE_URL",
    f"postgresql://{os.getenv('POSTGRES_USER', 'kestrel')}:"
    f"{os.getenv('POSTGRES_PASSWORD', 'changeme')}@"
    f"{os.getenv('POSTGRES_HOST', 'localhost')}:"
    f"{os.getenv('POSTGRES_PORT', '5432')}/"
    f"{os.getenv('POSTGRES_DB', 'kestrel')}"
)

# ── Database Layer ────────────────────────────────────────────────────
import asyncpg

_pool: asyncpg.Pool | None = None


async def get_pool() -> asyncpg.Pool:
    global _pool
    if _pool is None:
        _pool = await asyncpg.create_pool(
            DB_URL,
            min_size=int(os.getenv("POSTGRES_POOL_MIN", "2")),
            max_size=int(os.getenv("POSTGRES_POOL_MAX", "10")),
        )
    return _pool


# ── User Management ──────────────────────────────────────────────────

async def create_user(email: str, password: str, display_name: str = "") -> dict:
    """Create a new user with hashed password."""
    import hashlib, secrets
    pool = await get_pool()
    user_id = str(uuid.uuid4())
    salt = secrets.token_hex(16)
    pw_hash = hashlib.sha256((password + salt).encode()).hexdigest()

    await pool.execute(
        """INSERT INTO users (id, email, password_hash, salt, display_name, created_at)
           VALUES ($1, $2, $3, $4, $5, NOW())""",
        user_id, email, pw_hash, salt, display_name or email.split("@")[0],
    )
    return {"id": user_id, "email": email, "displayName": display_name}


async def authenticate_user(email: str, password: str) -> dict:
    """Verify credentials and return user info."""
    import hashlib
    pool = await get_pool()
    row = await pool.fetchrow(
        "SELECT id, email, password_hash, salt, display_name FROM users WHERE email = $1",
        email,
    )
    if not row:
        raise ValueError("User not found")

    pw_hash = hashlib.sha256((password + row["salt"]).encode()).hexdigest()
    if pw_hash != row["password_hash"]:
        raise ValueError("Invalid password")

    # Fetch workspace memberships
    memberships = await pool.fetch(
        """SELECT w.id, wm.role FROM workspaces w
           JOIN workspace_members wm ON w.id = wm.workspace_id
           WHERE wm.user_id = $1""",
        row["id"],
    )
    workspaces = [{"id": str(m["id"]), "role": m["role"]} for m in memberships]

    return {
        "id": str(row["id"]),
        "email": row["email"],
        "displayName": row["display_name"],
        "workspaces": workspaces,
    }


# ── Workspace / Conversation CRUD ────────────────────────────────────

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


# ── LLM Provider Registry ────────────────────────────────────────────

_providers: dict[str, LocalProvider | CloudProvider] = {}


def get_provider(name: str):
    if name not in _providers:
        if name == "local":
            _providers[name] = LocalProvider()
        else:
            _providers[name] = CloudProvider(name)
    return _providers[name]


# ── gRPC Service Implementation ──────────────────────────────────────

# We use a runtime proto loading approach so we don't need compiled stubs
import grpc_reflection.v1alpha.reflection as reflection
from grpc_tools.protoc import main as protoc_main

# Load proto definition at runtime
PROTO_PATH = os.path.join(os.path.dirname(__file__), "../shared/proto")
BRAIN_PROTO = os.path.join(PROTO_PATH, "brain.proto")


class BrainServicer:
    """Implements kestrel.brain.BrainService gRPC interface."""

    async def StreamChat(self, request, context):
        """Stream LLM responses back to the caller."""
        user_id = request.user_id
        workspace_id = request.workspace_id
        conversation_id = request.conversation_id
        provider_name = request.provider or "local"
        model = request.model

        logger.info(
            f"StreamChat: user={user_id}, provider={provider_name}, "
            f"model={model}, msgs={len(request.messages)}"
        )

        try:
            provider = get_provider(provider_name)

            # Convert proto messages to dict format
            messages = []
            role_map = {0: "user", 1: "assistant", 2: "system", 3: "tool"}
            for msg in request.messages:
                messages.append({
                    "role": role_map.get(msg.role, "user"),
                    "content": msg.content,
                })

            # Extract parameters
            params = dict(request.parameters) if request.parameters else {}
            temperature = float(params.get("temperature", "0.7"))
            max_tokens = int(params.get("max_tokens", "2048"))

            # Stream tokens
            async for token in provider.stream(
                messages=messages,
                model=model,
                temperature=temperature,
                max_tokens=max_tokens,
            ):
                # Build ChatResponse proto
                yield self._make_response(
                    chunk_type=0,  # CONTENT_DELTA
                    content_delta=token,
                )

            # Save the full response to DB
            # (Provider accumulates internally)
            full_response = provider.last_response
            if conversation_id and full_response:
                await save_message(conversation_id, "assistant", full_response)

            # Send DONE
            yield self._make_response(
                chunk_type=2,  # DONE
                metadata={"provider": provider_name, "model": model},
            )

        except Exception as e:
            logger.error(f"StreamChat error: {e}", exc_info=True)
            yield self._make_response(
                chunk_type=3,  # ERROR
                error_message=str(e),
            )

    def _make_response(self, chunk_type: int, content_delta: str = "",
                       error_message: str = "", metadata: dict = None,
                       tool_call: dict = None) -> dict:
        """Build a ChatResponse-compatible dict."""
        resp = {
            "type": chunk_type,
            "content_delta": content_delta,
            "error_message": error_message,
            "metadata": metadata or {},
        }
        if tool_call:
            resp["tool_call"] = tool_call
        return resp

    async def HealthCheck(self, request, context):
        """Return health status."""
        status = {}
        for name, provider in _providers.items():
            status[name] = "ready" if provider.is_ready() else "not_ready"

        return {
            "healthy": True,
            "version": "0.1.0",
            "status": status,
        }

    # ── Extended RPCs (user/workspace/conversation management) ────────

    async def CreateUser(self, request, context):
        try:
            return await create_user(request.email, request.password, request.display_name)
        except Exception as e:
            context.set_code(grpc.StatusCode.ALREADY_EXISTS)
            context.set_details(str(e))
            return {}

    async def AuthenticateUser(self, request, context):
        try:
            return await authenticate_user(request.email, request.password)
        except ValueError as e:
            context.set_code(grpc.StatusCode.UNAUTHENTICATED)
            context.set_details(str(e))
            return {}

    async def ListWorkspaces(self, request, context):
        workspaces = await list_workspaces(request.user_id)
        return {"workspaces": workspaces}

    async def CreateWorkspace(self, request, context):
        return await create_workspace(request.user_id, request.name)

    async def ListConversations(self, request, context):
        convos = await list_conversations(request.user_id, request.workspace_id)
        return {"conversations": convos}

    async def CreateConversation(self, request, context):
        return await create_conversation(request.user_id, request.workspace_id)

    async def GetMessages(self, request, context):
        msgs = await get_messages(
            request.user_id, request.workspace_id, request.conversation_id
        )
        return {"messages": msgs}

    async def RegisterPushToken(self, request, context):
        # Phase 2: implement push token storage
        return {"success": True}

    async def GetUpdates(self, request, context):
        # Phase 2: implement sync
        return {"messages": [], "conversations": []}


# ── Server Bootstrap ─────────────────────────────────────────────────

async def serve():
    server = grpc_aio.server(
        futures.ThreadPoolExecutor(max_workers=10),
        options=[
            ("grpc.max_send_message_length", 50 * 1024 * 1024),
            ("grpc.max_receive_message_length", 50 * 1024 * 1024),
        ],
    )

    # Register service
    # Note: In production, use compiled proto stubs via grpc_tools.protoc
    # For now, we use a generic servicer registration approach
    from grpc_reflection.v1alpha import reflection as grpc_reflection

    servicer = BrainServicer()

    # Dynamic proto loading for servicer registration
    from grpc_tools import protoc
    import importlib
    import sys
    import tempfile

    # Generate Python stubs in a temp dir
    out_dir = os.path.join(os.path.dirname(__file__), "_generated")
    os.makedirs(out_dir, exist_ok=True)

    protoc.main([
        "grpc_tools.protoc",
        f"-I{PROTO_PATH}",
        f"--python_out={out_dir}",
        f"--grpc_python_out={out_dir}",
        "brain.proto",
    ])

    # Import generated modules
    sys.path.insert(0, out_dir)
    import brain_pb2
    import brain_pb2_grpc

    brain_pb2_grpc.add_BrainServiceServicer_to_server(servicer, server)

    # Enable reflection
    service_names = (
        brain_pb2.DESCRIPTOR.services_by_name["BrainService"].full_name,
        grpc_reflection.SERVICE_NAME,
    )
    grpc_reflection.enable_server_reflection(service_names, server)

    bind_address = f"{GRPC_HOST}:{GRPC_PORT}"
    server.add_insecure_port(bind_address)

    logger.info(f"Brain gRPC server starting on {bind_address}")

    # Initialize database pool
    await get_pool()
    logger.info("Database pool initialized")

    # Initialize vector store
    vector_store = VectorStore()
    await vector_store.initialize()
    logger.info("Vector store initialized")

    await server.start()
    logger.info("Brain service ready")

    try:
        await server.wait_for_termination()
    except KeyboardInterrupt:
        logger.info("Shutting down Brain service...")
        await server.stop(5)
        if _pool:
            await _pool.close()


if __name__ == "__main__":
    asyncio.run(serve())
