import asyncio
import time
import json
from agent.types import ToolCall, ToolDefinition, RiskLevel
from agent.tools import ToolRegistry
from db import get_redis

async def my_slow_tool(target: str) -> dict:
    print(f"  [Handler Executing] computing for {target}...")
    await asyncio.sleep(1.0)
    return {"result": f"hello {target}", "computed": True}

async def main():
    print("Testing ToolRegistry Caching...")
    
    # 1. Clear cache for test
    redis = await get_redis()
    await redis.flushdb()
    
    registry = ToolRegistry()
    registry.register(
        definition=ToolDefinition(
            name="my_slow_tool",
            description="A test tool that is slow",
            parameters={"type": "object", "properties": {"target": {"type": "string"}}},
            cache_ttl_seconds=10
        ),
        handler=my_slow_tool
    )
    
    call1 = ToolCall(name="my_slow_tool", arguments={"target": "world"})
    
    print("\nFirst call (Expect ~1.0s wait):")
    start = time.time()
    res1 = await registry.execute(call1)
    duration1 = time.time() - start
    print(f"Result: {res1.output}")
    print(f"Duration: {duration1:.3f}s")
    
    print("\nSecond call with identical args (Expect ~0.0s wait, cache hit):")
    start = time.time()
    res2 = await registry.execute(call1)
    duration2 = time.time() - start
    print(f"Result: {res2.output}")
    print(f"Duration: {duration2:.3f}s")
    
    print("\nThird call with different args (Expect ~1.0s wait, cache miss):")
    call2 = ToolCall(name="my_slow_tool", arguments={"target": "kestrel"})
    start = time.time()
    res3 = await registry.execute(call2)
    duration3 = time.time() - start
    print(f"Result: {res3.output}")
    print(f"Duration: {duration3:.3f}s")

    assert duration1 >= 1.0, "First call was too fast"
    assert duration2 < 0.1, "Second call was not cached!"
    assert duration3 >= 1.0, "Third call should not have hit cache"
    print("\n✅ All tests passed!")

if __name__ == "__main__":
    asyncio.run(main())
