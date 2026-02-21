
import asyncio
import grpc
import sys
import os
import json

# Ensure we can import from local package
sys.path.append("/app")

# We need to import the generated protos.
# They are in /app/_generated or similar based on server.py logic.
# server.py does:
# out_dir = os.path.join(os.path.dirname(__file__), "_generated")
# sys.path.insert(0, out_dir)

out_dir = "/app/_generated"
sys.path.insert(0, out_dir)

try:
    import brain_pb2
    import brain_pb2_grpc
except ImportError:
    # If they don't exist yet (server hasn't run), we might need to generate them.
    # But server.py generates them on import? No, on run.
    # Let's assume server is running.
    pass

async def test_chat():
    print("Starting test_chat...")
    async with grpc.aio.insecure_channel('localhost:50051') as channel:
        stub = brain_pb2_grpc.BrainServiceStub(channel)
        
        # We need a valid workspace ID that has a Redis key.
        # But for now let's just use a dummy one to see logs.
        # If I want to test successful resolution, I need to insert into DB/Redis first.
        # But checking logs for "Resolved API key..." or "API key reference ... not found" is enough.
        
        provider = "google"
        model = "gemini-flash-latest"
        
        # Determine a workspace ID that has config.
        # I'll use the one from the logs: 3de6c351-410a-4062-b25d-81258d01f363
        workspace_id = "3de6c351-410a-4062-b25d-81258d01f363"
        user_id = "7698295a-d0a8-4da0-85d5-bbd3720edea5"

        messages = [
            brain_pb2.Message(role=0, content="Hello, are you working?")
        ]
        
        # Custom parameters to inject the key directly for testing
        parameters = {}

        print(f"Sending request for workspace={workspace_id}...")
        request = brain_pb2.ChatRequest(
            user_id=user_id,
            workspace_id=workspace_id,
            messages=messages,
            provider=provider,
            model="gemini-3.1-pro",
            parameters=parameters
        )
        
        try:
            async for response in stub.StreamChat(request):
                print(f"Received chunk type: {response.type}")
                if response.content_delta:
                    print(f" Content: {response.content_delta}")
                if response.error_message:
                    print(f" Error: {response.error_message}")
        except grpc.RpcError as e:
            print(f"RPC Error: {e}")
            
if __name__ == "__main__":
    try:
        asyncio.run(test_chat())
    except Exception as e:
        print(f"Script failed: {e}")
