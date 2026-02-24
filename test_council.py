import asyncio
from httpx import AsyncClient

async def test_council():
    async with AsyncClient() as client:
        # Simulate a complex request to trigger planning > 1 step
        payload = {
            "message": "Analyze the codebase for all python files, look for hardcoded api keys, and then replace them with env variables.",
            "workspaceId": "02bf0409-802f-4555-8d25-544ec79dd75b"
        }
        
        # Stream the chat
        timeout = 60.0
        async with client.stream("POST", "http://localhost:3000/api/chat", json=payload, timeout=timeout) as response:
            async for line in response.aiter_lines():
                if line:
                    if "council" in line.lower() or "verdict" in line.lower():
                        print(f"COUNCIL EVENT: {line}")
                    elif "plan_created" in line:
                         print(f"PLAN: {line}")

if __name__ == "__main__":
    asyncio.run(test_council())
