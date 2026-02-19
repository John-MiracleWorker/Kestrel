import asyncio
import grpc
import os
import sys

# Add packages/brain to path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '../packages/brain')))
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '../packages/shared/proto')))

import brain_pb2
import brain_pb2_grpc

async def main():
    # Connect to Brain Service
    channel = grpc.aio.insecure_channel('localhost:50051')
    stub = brain_pb2_grpc.BrainServiceStub(channel)

    print("--- Testing ListModels (Local) ---")
    request = brain_pb2.ListModelsRequest(provider="local")
    try:
        response = await stub.ListModels(request)
        print(f"Models: {len(response.models)}")
        for m in response.models:
            print(f" - {m.name} ({m.id})")
    except Exception as e:
        print(f"RPC Failed: {e}")

    print("\n--- Testing ListModels (Google - requires key) ---")
    # user needs to provided key in env or we skip
    api_key = os.getenv("GOOGLE_API_KEY")
    if api_key:
        request = brain_pb2.ListModelsRequest(provider="google", api_key=api_key)
        try:
            response = await stub.ListModels(request)
            print(f"Models: {len(response.models)}")
            if len(response.models) > 0:
                 print(f" - First model: {response.models[0].name}")
        except Exception as e:
            print(f"RPC Failed: {e}")
    else:
        print("Skipping Google test (no GOOGLE_API_KEY env)")

    print("\n--- Testing ListModels (Anthropic - static) ---")
    request = brain_pb2.ListModelsRequest(provider="anthropic")
    try:
        response = await stub.ListModels(request)
        print(f"Models: {len(response.models)}")
        for m in response.models:
            print(f" - {m.name} ({m.id})")
    except Exception as e:
        print(f"RPC Failed: {e}")

    await channel.close()

if __name__ == "__main__":
    asyncio.run(main())
