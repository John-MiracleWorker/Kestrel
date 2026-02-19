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
from typing import Optional, Union
from dotenv import load_dotenv

# Generated protobuf stubs (will be generated from proto files)
# For now, use proto_loader approach
import grpc_tools
from google.protobuf import json_format

from providers.local import LocalProvider
from providers.cloud import CloudProvider
from memory.vector_store import VectorStore
from memory.retrieval import RetrievalPipeline
from memory.embeddings import EmbeddingPipeline
from provider_config import ProviderConfig

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
import redis.asyncio as redis

_pool: Optional[asyncpg.Pool] = None
_redis_pool: Optional[redis.Redis] = None


async def get_pool() -> asyncpg.Pool:
    global _pool
    if _pool is None:
        _pool = await asyncpg.create_pool(
            DB_URL,
            min_size=int(os.getenv("POSTGRES_POOL_MIN", "2")),
            max_size=int(os.getenv("POSTGRES_POOL_MAX", "10")),
        )
    return _pool

async def get_redis() -> redis.Redis:
    global _redis_pool
    if _redis_pool is None:
        _redis_pool = redis.from_url(
            f"redis://{os.getenv('REDIS_HOST', 'localhost')}:{os.getenv('REDIS_PORT', '6379')}"
        )
    return _redis_pool


# ── User Management ──────────────────────────────────────────────────

async def create_user(email: str, password: str, display_name: str = "") -> dict:
    """Create a new user with hashed password."""
    import hashlib, secrets
    pool = await get_pool()
    user_id = str(uuid.uuid4())
    salt = secrets.token_hex(16)
    pw_hash = hashlib.sha256((password + salt).encode()).hexdigest()

    final_display_name = display_name or email.split("@")[0]
    await pool.execute(
        """INSERT INTO users (id, email, password_hash, salt, display_name, created_at)
           VALUES ($1, $2, $3, $4, $5, NOW())""",
        user_id, email, pw_hash, salt, final_display_name,
    )
    return {"id": user_id, "email": email, "displayName": final_display_name}


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


async def delete_conversation(user_id: str, workspace_id: str, conversation_id: str) -> bool:
    pool = await get_pool()
    # Verify ownership/membership before deleting? 
    # For now, we trust the workspace_id check implicitly via the query
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

_providers: dict[str, Union[LocalProvider, CloudProvider]] = {}
_retrieval: Optional[RetrievalPipeline] = None
_embedding_pipeline: Optional[EmbeddingPipeline] = None

# ── Agent Runtime ────────────────────────────────────────────────────

_agent_loop = None
_agent_persistence = None
_running_tasks: dict[str, object] = {}


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

# Dynamic proto loading
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


import brain_pb2_grpc

# ── Provider Config ────────────────────────────────────────────────
async def list_provider_configs(workspace_id):
    query = """
        SELECT * FROM workspace_provider_config
        WHERE workspace_id = $1
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        return await conn.fetch(query, workspace_id)

async def set_provider_config(workspace_id, provider, config):
    # upsert
    query = """
        INSERT INTO workspace_provider_config (
            workspace_id, provider, model, api_key_encrypted, 
            temperature, max_tokens, system_prompt, rag_enabled, 
            rag_top_k, rag_min_similarity, is_default, settings
        ) VALUES (
            $1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12
        )
        ON CONFLICT (workspace_id, provider) DO UPDATE SET
            model = EXCLUDED.model,
            api_key_encrypted = COALESCE(EXCLUDED.api_key_encrypted, workspace_provider_config.api_key_encrypted),
            temperature = EXCLUDED.temperature,
            max_tokens = EXCLUDED.max_tokens,
            system_prompt = EXCLUDED.system_prompt,
            rag_enabled = EXCLUDED.rag_enabled,
            rag_top_k = EXCLUDED.rag_top_k,
            rag_min_similarity = EXCLUDED.rag_min_similarity,
            is_default = EXCLUDED.is_default,
            settings = EXCLUDED.settings,
            updated_at = NOW()
        RETURNING *
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            if config.get('is_default', False):
                await conn.execute(
                    "UPDATE workspace_provider_config SET is_default = FALSE WHERE workspace_id = $1",
                    workspace_id
                )
            
            return await conn.fetchrow(query, 
                workspace_id, provider, config.get('model'), config.get('api_key_encrypted'),
                config.get('temperature', 0.7), config.get('max_tokens', 2048),
                config.get('system_prompt'), config.get('rag_enabled', True),
                config.get('rag_top_k', 5), config.get('rag_min_similarity', 0.3),
                config.get('is_default', False), json.dumps(config.get('settings', {}))
            )

async def delete_provider_config(workspace_id, provider):
    query = "DELETE FROM workspace_provider_config WHERE workspace_id = $1 AND provider = $2"
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(query, workspace_id, provider)


class BrainServicer:
    """Implements kestrel.brain.BrainService gRPC interface."""

    async def ListModels(self, request, context):
        """List available models for a provider."""
        try:
            # 1. Get provider instance
            if request.provider == "local":
                # TODO: query local models
                models = [
                    {"id": "llama-3-8b-instruct", "name": "Llama 3 8B (Local)", "context_window": "8k"},
                    {"id": "mistral-7b-instruct", "name": "Mistral 7B (Local)", "context_window": "32k"},
                ]
                pb_models = [
                    brain_pb2.Model(id=m["id"], name=m["name"], context_window=m["context_window"])
                    for m in models
                ]
                return brain_pb2.ListModelsResponse(models=pb_models)
            
            provider = get_provider(request.provider)
            if not isinstance(provider, CloudProvider):
                    logger.error(f"Provider {request.provider} is not a CloudProvider")
                    return brain_pb2.ListModelsResponse(models=[])

            # 2. Resolve API Key
            api_key = request.api_key
            if not api_key and request.workspace_id:
                # Try to load from workspace config
                try:
                    pool = await get_pool()
                    ws_config = await ProviderConfig(pool).get_config(request.workspace_id)
                    # Check if this config is for the requested provider?
                    # ProviderConfig.get_config returns *resolved* config (merged with default)
                    # But we specifically want the key for the requested provider if it matches
                    # Actually get_config returns configuration for the *active* provider?
                    # No, let's look at provider_config.py...
                    # It seems get_config fetches "effective" config.
                    
                    # Better approach: Fetch specifically for this provider
                    # We can use list_provider_configs helper or check Redis
                    # Let's check list_provider_configs in server.py
                    configs = await list_provider_configs(request.workspace_id)
                    # configs is a list of records
                    for c in configs:
                        if c["provider"] == request.provider:
                            # Found config for this provider
                            encrypted = c.get("api_key_encrypted")
                            if encrypted and encrypted.startswith("provider_key:"):
                                r = await get_redis()
                                real_key = await r.get(encrypted)
                                if real_key:
                                    api_key = real_key.decode("utf-8")
                            elif encrypted:
                                api_key = encrypted # Assuming config stores plain if not prefixed (or handled elsewhere)
                            break
                except Exception as e:
                    logger.warning(f"Failed to resolve workspace key for ListModels: {e}")

            # 3. Fetch models
            model_list = await provider.list_models(api_key=api_key)
            
            models = []
            for m in model_list:
                models.append({
                    "id": m["id"],
                    "name": m["name"],
                    "context_window": m.get("context_window", "")
                })

            # 4. Convert to proto
            pb_models = [
                brain_pb2.Model(
                    id=m["id"],
                    name=m["name"],
                    context_window=m["context_window"]
                ) for m in models
            ]
            return brain_pb2.ListModelsResponse(models=pb_models)

        except Exception as e:
            logger.error(f"ListModels error: {e}", exc_info=True)
            return brain_pb2.ListModelsResponse(models=[])

    async def StreamChat(self, request, context):
        """Stream LLM responses back to the caller."""
        user_id = request.user_id
        workspace_id = request.workspace_id
        conversation_id = request.conversation_id

        logger.info(
            f"StreamChat: user={user_id}, workspace={workspace_id}, "
            f"msgs={len(request.messages)}"
        )

        try:
            # ── 1. Load workspace provider config ───────────────────
            pool = await get_pool()
            ws_config = await ProviderConfig(pool).get_config(workspace_id)
            
            # Resolve API Key from Redis if it's a reference
            api_key = ws_config.get("api_key", "")
            if api_key and api_key.startswith("provider_key:"):
                try:
                    r = await get_redis()
                    real_key = await r.get(api_key)
                    if real_key:
                        api_key = real_key.decode("utf-8")
                        logger.info(f"Resolved API key for {workspace_id} from Redis")
                    else:
                        logger.warning(f"API key reference {api_key} not found in Redis")
                        api_key = ""
                except Exception as e:
                    logger.error(f"Redis error resolving API key: {e}")
                    api_key = ""
            
            # DEBUG: Check if API key is present
            api_key_status = "PRESENT" if api_key else "MISSING"
            logger.info(f"Loaded config for {workspace_id}: provider={ws_config.get('provider')}, api_key={api_key_status}")

            # Request can override workspace defaults
            provider_name = request.provider or ws_config["provider"]
            model = request.model or ws_config["model"]

            provider = get_provider(provider_name)

            # Convert proto messages to dict format
            messages = []
            role_map = {0: "user", 1: "assistant", 2: "system", 3: "tool"}
            for msg in request.messages:
                messages.append({
                    "role": role_map.get(msg.role, "user"),
                    "content": msg.content,
                })

            # Extract parameters (request overrides → workspace config → defaults)
            params = dict(request.parameters) if request.parameters else {}
            temperature = float(params.get("temperature", str(ws_config["temperature"])))
            max_tokens = int(params.get("max_tokens", str(ws_config["max_tokens"])))

            # ── 2. RAG context injection ────────────────────────────
            if ws_config["rag_enabled"] and _retrieval:
                user_msg = next(
                    (m["content"] for m in reversed(messages) if m["role"] == "user"),
                    "",
                )
                if user_msg:
                    base_prompt = ws_config.get("system_prompt", "")
                    augmented = await _retrieval.build_augmented_prompt(
                        workspace_id=workspace_id,
                        user_message=user_msg,
                        system_prompt=base_prompt,
                        top_k=ws_config["rag_top_k"],
                        min_similarity=ws_config["rag_min_similarity"],
                    )
                    if augmented:
                        # Inject or replace system message
                        if messages and messages[0]["role"] == "system":
                            messages[0]["content"] = augmented
                        else:
                            messages.insert(0, {"role": "system", "content": augmented})

            elif ws_config.get("system_prompt"):
                # No RAG but workspace has a system prompt
                if messages and messages[0]["role"] == "system":
                    messages[0]["content"] = ws_config["system_prompt"]
                else:
                    messages.insert(0, {"role": "system", "content": ws_config["system_prompt"]})

            logger.info(f"Using provider={provider_name}, model={model}")

            # ── 3. Save user message before streaming ───────────────
            if conversation_id:
                user_content = next(
                    (m["content"] for m in reversed(messages) if m["role"] == "user"),
                    "",
                )
                if user_content:
                    await save_message(conversation_id, "user", user_content)

            # ── 4. Stream tokens ────────────────────────────────────
            async for token in provider.stream(
                messages=messages,
                model=model,
                temperature=temperature,
                max_tokens=max_tokens,
                api_key=api_key,
            ):
                yield self._make_response(
                    chunk_type=0,  # CONTENT_DELTA
                    content_delta=token,
                )

            # ── 5. Save response + auto-embed ──────────────────────
            full_response = provider.last_response
            if conversation_id and full_response:
                await save_message(conversation_id, "assistant", full_response)

                # Auto-embed the Q&A pair for future RAG
                if ws_config["rag_enabled"] and _embedding_pipeline:
                    user_msg = next(
                        (m["content"] for m in reversed(messages) if m["role"] == "user"),
                        "",
                    )
                    await _embedding_pipeline.embed_conversation_turn(
                        workspace_id=workspace_id,
                        conversation_id=conversation_id,
                        user_message=user_msg,
                        assistant_response=full_response,
                    )

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
                       tool_call: dict = None):
        """Build a ChatResponse object."""
        # Use the generated protobuf class
        logger.debug(f"Making response chunk {chunk_type}")
        resp = brain_pb2.ChatResponse(
            type=chunk_type,
            content_delta=content_delta,
            error_message=error_message,
            metadata=metadata or {},
        )
        if tool_call:
            resp.tool_call.id = tool_call.get("id", "")
            resp.tool_call.name = tool_call.get("name", "")
            resp.tool_call.arguments = tool_call.get("arguments", "")
        
        # DEBUG: Verify type
        # logger.info(f"Response type: {type(resp)}")
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
            data = await create_user(request.email, request.password, request.display_name)
            return brain_pb2.UserResponse(
                id=data["id"],
                email=data["email"],
                display_name=data["displayName"]
            )
        except Exception as e:
            context.set_code(grpc.StatusCode.ALREADY_EXISTS)
            context.set_details(str(e))
            return brain_pb2.UserResponse()

    async def AuthenticateUser(self, request, context):
        try:
            data = await authenticate_user(request.email, request.password)
            workspaces = [
                brain_pb2.WorkspaceMembership(id=w["id"], role=w["role"])
                for w in data["workspaces"]
            ]
            return brain_pb2.AuthenticateUserResponse(
                id=data["id"],
                email=data["email"],
                display_name=data["displayName"],
                workspaces=workspaces
            )
        except ValueError as e:
            context.set_code(grpc.StatusCode.UNAUTHENTICATED)
            context.set_details(str(e))
            return brain_pb2.AuthenticateUserResponse()

    async def ListWorkspaces(self, request, context):
        raw_workspaces = await list_workspaces(request.user_id)
        workspaces = [
            brain_pb2.WorkspaceResponse(
                id=w["id"],
                name=w["name"],
                role=w["role"],
                created_at=w["createdAt"]
            ) for w in raw_workspaces
        ]
        return brain_pb2.ListWorkspacesResponse(workspaces=workspaces)

    async def CreateWorkspace(self, request, context):
        data = await create_workspace(request.user_id, request.name)
        return brain_pb2.WorkspaceResponse(
            id=data["id"],
            name=data["name"],
            role=data["role"]
        )

    async def ListConversations(self, request, context):
        raw_convos = await list_conversations(request.user_id, request.workspace_id)
        conversations = [
            brain_pb2.ConversationResponse(
                id=c["id"],
                title=c["title"],
                created_at=c["createdAt"],
                updated_at=c["updatedAt"]
            ) for c in raw_convos
        ]
        return brain_pb2.ListConversationsResponse(conversations=conversations)

    async def CreateConversation(self, request, context):
        data = await create_conversation(request.user_id, request.workspace_id)
        return brain_pb2.ConversationResponse(
            id=data["id"],
            title=data["title"]
        )

    async def GetMessages(self, request, context):
        try:
            raw_msgs = await get_messages(
                request.user_id, request.workspace_id, request.conversation_id
            )
            messages = [
                brain_pb2.MessageResponse(
                    id=m["id"],
                    role=m["role"],
                    content=m["content"],
                    created_at=m["createdAt"]
                ) for m in raw_msgs
            ]
            return brain_pb2.GetMessagesResponse(messages=messages)
        except Exception as e:
            # Handle invalid UUIDs or DB errors gracefully
            logger.error(f"GetMessages error: {e}")
            context.set_code(grpc.StatusCode.INVALID_ARGUMENT)
            context.set_details(str(e))
            return brain_pb2.GetMessagesResponse()

    async def DeleteConversation(self, request, context):
        try:
            success = await delete_conversation(
                request.user_id, request.workspace_id, request.conversation_id
            )
            return brain_pb2.DeleteConversationResponse(success=success)
        except Exception as e:
            logger.error(f"DeleteConversation error: {e}")
            context.set_code(grpc.StatusCode.INTERNAL)
            return brain_pb2.DeleteConversationResponse(success=False)

    async def UpdateConversation(self, request, context):
        try:
            data = await update_conversation_title(
                request.user_id, request.workspace_id, request.conversation_id, request.title
            )
            return brain_pb2.ConversationResponse(
                id=data["id"],
                title=data["title"],
                created_at=data["createdAt"],
                updated_at=data["updatedAt"]
            )
        except ValueError:
            context.set_code(grpc.StatusCode.NOT_FOUND)
            return brain_pb2.ConversationResponse()
        except Exception as e:
            logger.error(f"UpdateConversation error: {e}")
            context.set_code(grpc.StatusCode.INTERNAL)
            return brain_pb2.ConversationResponse()

    async def GenerateTitle(self, request, context):
        """Generate a title for the conversation using the LLM."""
        try:
            # 1. Fetch messages
            messages = await get_messages(
                request.user_id, request.workspace_id, request.conversation_id
            )
            if not messages:
                return brain_pb2.GenerateTitleResponse(title="New Conversation")

            # 2. Construct prompt
            conversation_text = ""
            for m in messages[:6]: # Use first few messages
                conversation_text += f"{m['role']}: {m['content']}\n"
            
            prompt = (
                "Summarize the following conversation into a short, concise title (max 6 words). "
                "Do not use quotes. Just the title.\n\n"
                f"{conversation_text}"
            )

            # 3. Resolve provider — use workspace config, fall back to first user message
            try:
                pool = await get_pool()
                ws_config = await ProviderConfig(pool).get_config(request.workspace_id)
                provider_name = ws_config.get("provider", "local")
                api_key = ws_config.get("api_key", "")
                # Resolve Redis key reference
                if api_key and api_key.startswith("provider_key:"):
                    r = await get_redis()
                    real_key = await r.get(api_key)
                    api_key = real_key.decode("utf-8") if real_key else ""
                provider = get_provider(provider_name)
            except Exception:
                provider_name = "local"
                api_key = ""
                provider = get_provider("local")

            # Allow "smart" title generation:
            response_chunks = []
            try:
                async for token in provider.stream(
                    messages=[{"role": "user", "content": prompt}],
                    model="",
                    temperature=0.3,
                    max_tokens=20,
                    api_key=api_key,
                ):
                    response_chunks.append(token)
            except Exception as stream_err:
                logger.warning(f"Title generation stream failed: {stream_err}")

            # If LLM failed or returned nothing, derive title from first user message
            if not response_chunks:
                first_user = next((m["content"] for m in messages if m["role"] == "user"), "")
                generated_title = first_user[:50].strip() if first_user else "New Conversation"
            else:
                generated_title = "".join(response_chunks).strip().strip('"')

            # Clamp to 80 chars
            generated_title = generated_title[:80] if generated_title else "New Conversation"
            
            # Update the title in DB
            await update_conversation_title(
                request.user_id, request.workspace_id, request.conversation_id, generated_title
            )

            return brain_pb2.GenerateTitleResponse(title=generated_title)

        except Exception as e:
            logger.error(f"GenerateTitle error: {e}")
            # Fallback
            return brain_pb2.GenerateTitleResponse(title="New Conversation")

    async def RegisterPushToken(self, request, context):
        # Phase 2: implement push token storage
        return brain_pb2.RegisterPushTokenResponse(success=True)

    async def GetUpdates(self, request, context):
        # Phase 2: implement sync
        return brain_pb2.GetUpdatesResponse(messages=[], conversations=[])

    # ── Autonomous Agent RPCs ────────────────────────────────────

    async def StartTask(self, request, context):
        """Start an autonomous agent task and stream events."""
        import json
        from agent.types import (
            AgentTask,
            GuardrailConfig as GCfg,
            RiskLevel,
            TaskStatus,
        )

        user_id = request.user_id
        workspace_id = request.workspace_id
        goal = request.goal

        # Build guardrail config from request (or defaults)
        config = GCfg()
        if request.guardrails:
            g = request.guardrails
            if g.max_iterations > 0:
                config.max_iterations = g.max_iterations
            if g.max_tool_calls > 0:
                config.max_tool_calls = g.max_tool_calls
            if g.max_tokens > 0:
                config.max_tokens = g.max_tokens
            if g.max_wall_time_seconds > 0:
                config.max_wall_time_seconds = g.max_wall_time_seconds
            if g.auto_approve_risk:
                config.auto_approve_risk = RiskLevel(g.auto_approve_risk)
            if g.blocked_patterns:
                config.blocked_patterns = list(g.blocked_patterns)
            if g.require_approval_tools:
                config.require_approval_tools = list(g.require_approval_tools)

        # Create the task
        task = AgentTask(
            user_id=user_id,
            workspace_id=workspace_id,
            goal=goal,
            conversation_id=request.conversation_id or None,
            config=config,
        )

        # Save to DB
        await _agent_persistence.save_task(task)
        logger.info(f"Agent task started: {task.id} — {goal}")

        # Store the running task handle
        _running_tasks[task.id] = task

        # Run the agent loop and stream events
        try:
            async for event in _agent_loop.run(task):
                yield {
                    "type": event.type.value if hasattr(event.type, 'value') else event.type,
                    "task_id": event.task_id,
                    "step_id": event.step_id or "",
                    "content": event.content,
                    "tool_name": event.tool_name or "",
                    "tool_args": event.tool_args or "",
                    "tool_result": event.tool_result or "",
                    "approval_id": event.approval_id or "",
                    "progress": {k: str(v) for k, v in (event.progress or {}).items()},
                }
        finally:
            _running_tasks.pop(task.id, None)

    async def StreamTaskEvents(self, request, context):
        """Reconnect to an already-running task's event stream."""
        task_id = request.task_id

        if task_id not in _running_tasks:
            context.abort(grpc.StatusCode.NOT_FOUND, f"Task {task_id} is not running")
            return

        # TODO: Implement event replay/fan-out via Redis pubsub
        context.abort(
            grpc.StatusCode.UNIMPLEMENTED,
            "Event stream reconnection coming in next iteration",
        )

    async def ApproveAction(self, request, context):
        """Approve or deny a pending agent action."""
        from agent.types import ApprovalStatus

        try:
            await _agent_persistence.resolve_approval(
                approval_id=request.approval_id,
                status=ApprovalStatus.APPROVED if request.approved else ApprovalStatus.DENIED,
                decided_by=request.user_id,
            )
            return {"success": True, "error": ""}
        except Exception as e:
            return {"success": False, "error": str(e)}

    async def CancelTask(self, request, context):
        """Cancel a running agent task."""
        task_id = request.task_id

        if task_id in _running_tasks:
            task = _running_tasks[task_id]
            task.status = "cancelled"
            await _agent_persistence.update_task(task)
            _running_tasks.pop(task_id, None)
            return {"success": True}

        # Try updating DB directly
        pool = await get_pool()
        await pool.execute(
            "UPDATE agent_tasks SET status = 'cancelled' WHERE id = $1",
            task_id,
        )
        return {"success": True}

    async def ListTasks(self, request, context):
        """List agent tasks for a user/workspace."""
        pool = await get_pool()
        query = """
            SELECT id, goal, status, iterations, tool_calls_count,
                   result, error, created_at, completed_at
            FROM agent_tasks
            WHERE user_id = $1
        """
        params = [request.user_id]

        if request.workspace_id:
            query += " AND workspace_id = $2"
            params.append(request.workspace_id)

        if request.status:
            query += f" AND status = ${len(params) + 1}"
            params.append(request.status)

        query += " ORDER BY created_at DESC LIMIT 50"

        rows = await pool.fetch(query, *params)
        tasks = []
        for row in rows:
            tasks.append(brain_pb2.TaskSummary(
                id=str(row["id"]),
                goal=row["goal"],
                status=row["status"],
                iterations=row["iterations"],
                tool_calls=row["tool_calls_count"],
                result=row["result"] or "",
                error=row["error"] or "",
                created_at=row["created_at"].isoformat() if row["created_at"] else "",
                completed_at=row["completed_at"].isoformat() if row["completed_at"] else "",
            ))

        return brain_pb2.ListTasksResponse(tasks=tasks)

    # ── Provider Configuration ───────────────────────────────────



    async def ListProviderConfigs(self, request, context):
        rows = await list_provider_configs(request.workspace_id)
        configs = []
        for row in rows:
            configs.append(brain_pb2.ProviderConfig(
                workspace_id=str(row['workspace_id']),
                provider=row['provider'],
                model=row['model'] or "",
                temperature=row['temperature'],
                max_tokens=row['max_tokens'],
                system_prompt=row['system_prompt'] or "",
                rag_enabled=row['rag_enabled'],
                rag_top_k=row['rag_top_k'],
                rag_min_similarity=row['rag_min_similarity'],
                is_default=row['is_default'],
                api_key_encrypted=row['api_key_encrypted'] or "",
                created_at=row['created_at'].isoformat(),
                updated_at=row['updated_at'].isoformat()
            ))
        return brain_pb2.ListProviderConfigsResponse(configs=configs)

    async def SetProviderConfig(self, request, context):
        config_dict = {
            'model': request.model,
            'temperature': request.temperature,
            'max_tokens': request.max_tokens,
            'system_prompt': request.system_prompt,
            'rag_enabled': request.rag_enabled,
            'rag_top_k': request.rag_top_k,
            'rag_min_similarity': request.rag_min_similarity,
            'is_default': request.is_default,
        }
        if request.api_key_encrypted:
            config_dict['api_key_encrypted'] = request.api_key_encrypted
            
        row = await set_provider_config(request.workspace_id, request.provider, config_dict)
        
        return brain_pb2.SetProviderConfigResponse(
            config=brain_pb2.ProviderConfig(
                workspace_id=str(row['workspace_id']),
                provider=row['provider'],
                model=row['model'] or "",
                temperature=row['temperature'],
                max_tokens=row['max_tokens'],
                system_prompt=row['system_prompt'] or "",
                rag_enabled=row['rag_enabled'],
                rag_top_k=row['rag_top_k'],
                rag_min_similarity=row['rag_min_similarity'],
                is_default=row['is_default'],
                created_at=row['created_at'].isoformat(),
                updated_at=row['updated_at'].isoformat()
            )
        )

    async def DeleteProviderConfig(self, request, context):
        await delete_provider_config(request.workspace_id, request.provider)
        return brain_pb2.DeleteProviderConfigResponse(success=True)


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

    # Initialize vector store + RAG pipelines
    vector_store = VectorStore()
    await vector_store.initialize()
    logger.info("Vector store initialized")

    global _retrieval, _embedding_pipeline
    _retrieval = RetrievalPipeline(vector_store)
    _embedding_pipeline = EmbeddingPipeline(vector_store)
    await _embedding_pipeline.start()
    logger.info("RAG pipelines initialized")

    # Initialize agent runtime
    from agent.tools import build_tool_registry
    from agent.guardrails import Guardrails
    from agent.loop import AgentLoop
    from agent.persistence import PostgresTaskPersistence

    global _agent_loop, _agent_persistence
    tool_registry = build_tool_registry()
    guardrails = Guardrails()
    _agent_persistence = PostgresTaskPersistence(pool=await get_pool())
    _agent_loop = AgentLoop(
        provider=get_provider("local"),
        tool_registry=tool_registry,
        guardrails=guardrails,
        persistence=_agent_persistence,
    )
    logger.info(f"Agent runtime initialized ({len(tool_registry._definitions)} tools)")

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
