from provider_config import ProviderConfig
import grpc
from core.grpc_setup import brain_pb2
from .base import BaseServicerMixin
from crud import (
    list_conversations, create_conversation, get_messages, 
    delete_conversation, update_conversation_title
)
from db import get_pool, get_redis
from providers_registry import get_provider
from core.config import logger

class ConversationServicerMixin(BaseServicerMixin):
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
            logger.info(f"GenerateTitle: using provider={provider_name}, model=default, api_key={'present' if api_key else 'MISSING'}")
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
            raw_response = "".join(response_chunks).strip()
            logger.info(f"GenerateTitle: raw_response='{raw_response[:100]}', chunks={len(response_chunks)}")
            if not response_chunks or raw_response.startswith("[Error"):
                first_user = next((m["content"] for m in messages if m["role"] == "user"), "")
                generated_title = first_user[:50].strip() if first_user else "New Conversation"
            else:
                generated_title = raw_response.strip('"')

            # Clamp to 80 chars
            generated_title = generated_title[:80] if generated_title else "New Conversation"
            logger.info(f"GenerateTitle: final title='{generated_title}'")
            
            # Update the title in DB
            await update_conversation_title(
                request.user_id, request.workspace_id, request.conversation_id, generated_title
            )

            return brain_pb2.GenerateTitleResponse(title=generated_title)

        except Exception as e:
            logger.error(f"GenerateTitle error: {e}")
            # Fallback
            return brain_pb2.GenerateTitleResponse(title="New Conversation")

