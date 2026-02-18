
import asyncio
import grpc
import sys
import os

# Add package root to path
sys.path.append("c:/Users/admin/LibreBird")

from packages.shared.proto import brain_pb2, brain_pb2_grpc

async def test_chat():
    # Use the same IDs as the user would (or reasonable defaults)
    # Ideally I'd query the DB for a real workspace ID, but let's try a dummy first
    # If the DB lookup fails, it might throw, but we'll see that in the logs.
    # Actually, to be safe, I should probably list workspaces or providers first.
    
    async with grpc.aio.insecure_channel('localhost:50051') as channel:
        stub = brain_pb2_grpc.BrainServiceStub(channel)
        
        # 1. Create a dummy request. 
        # I need a valid workspace ID if I want to test the config loading logic!
        # Let's try to query the DB or use a known one if I found it.
        # But for now, let's just use "default" and see IF the log line prints "MISSING" or errors.
        
        messages = [brain_pb2.Message(role=0, content="Hello")]
        request = brain_pb2.ChatRequest(
            user_id="user_123",
            workspace_id="ws_123", # This probably won't have a config, so it should log "MISSING"
            messages=messages,
            provider="openai",
            model="gpt-4o-mini"
        )
        
        try:
            async for response in stub.StreamChat(request):
                print(f"Received: {response}")
        except grpc.RpcError as e:
            print(f"RPC Error: {e}")

if __name__ == "__main__":
    asyncio.run(test_chat())
