import asyncio
import grpc
import json
import uuid

# We need the compiled protobuf files
import sys
import os

sys.path.append(os.path.join(os.path.dirname(__file__), 'packages', 'brain'))
import brain_pb2
import brain_pb2_grpc

async def test_council():
    print("Testing Brain gRPC directly...")
    
    # Connect directly to Brain, bypassing Gateway auth
    channel = grpc.aio.insecure_channel('localhost:50051')
    stub = brain_pb2_grpc.BrainStub(channel)

    req = brain_pb2.ChatRequest(
        user_id="2c104172-fa03-4d01-b8d2-e3939f808843",
        workspace_id="02bf0409-802f-4555-8d25-544ec79dd75b",
        session_id=str(uuid.uuid4()),
        message="I need you to write a detailed technical specification for a new feature that parses Python AST trees to find hardcoded credentials, and then automatically creates a PR to replace them with python dotenv variables. This requires careful planning, multiple steps, and considering security implications.",
        model="" # Let router decide
    )

    try:
        print("Sending request to Brain...")
        async for chunk in stub.Chat(req, timeout=120):
            if chunk.type == "agent_event":
                try:
                    data = json.loads(chunk.content)
                    event_type = data.get("type", "")
                    
                    if "council" in event_type or "plan_created" == event_type:
                        print(f"\n[{event_type.upper()}]")
                        if "content" in data:
                            print(data["content"])
                        else:
                            print(json.dumps(data, indent=2))
                except json.JSONDecodeError:
                    print(f"Raw event: {chunk.content}")
            else:
                if chunk.content and chunk.content.strip():
                    # print(f"[{chunk.type}]: {chunk.content.strip()}")
                    pass
    except Exception as e:
        print(f"Error: {e}")

if __name__ == "__main__":
    asyncio.run(test_council())
