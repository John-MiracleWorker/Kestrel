import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import db as brain_db
from agent.tools import project_context
from memory.vector_store import VectorStore
from native_backends import LocalRedis


def test_local_redis_backend_supports_core_commands(tmp_path, monkeypatch):
    monkeypatch.setenv("KESTREL_HOME", str(tmp_path / ".kestrel"))
    monkeypatch.setenv("KESTREL_RUNTIME_MODE", "native")
    monkeypatch.setenv("BRAIN_REDIS_BACKEND", "local")

    async def scenario():
        await brain_db.reset_cached_clients()
        redis = await brain_db.get_redis()
        assert isinstance(redis, LocalRedis)

        await redis.flushdb()
        await redis.setex("alpha", 60, "one")
        assert await redis.get("alpha") == "one"

        await redis.sadd("members", "a", "b")
        assert await redis.smembers("members") == {"a", "b"}

        await redis.rpush("events", "first", "second", "third")
        await redis.ltrim("events", -2, -1)
        assert await redis.lrange("events", 0, -1) == ["second", "third"]

        pipe = redis.pipeline()
        pipe.delete("alpha")
        await pipe.execute()
        assert await redis.get("alpha") is None

        pubsub = redis.pubsub()
        await pubsub.subscribe("notifications")
        await redis.publish("notifications", "hello")
        message = await pubsub.get_message(timeout=0.2)
        await pubsub.close()
        assert message is not None
        assert message["data"] == "hello"

        await brain_db.reset_cached_clients()

    asyncio.run(scenario())


def test_vector_store_local_backend_supports_upsert_and_filtered_search(tmp_path, monkeypatch):
    monkeypatch.setenv("KESTREL_HOME", str(tmp_path / ".kestrel"))
    monkeypatch.setenv("KESTREL_RUNTIME_MODE", "native")
    monkeypatch.setenv("BRAIN_VECTOR_BACKEND", "local")

    async def scenario():
        store = VectorStore()
        await store.initialize()
        assert store.backend_name == "sqlite_exact"

        await store.upsert(
            workspace_id="ws-1",
            source_filter="memory_graph",
            documents=[
                {
                    "content": "Architect council agreed to use a native daemon control plane",
                    "metadata": {"entity_name": "control-plane", "source": "memory_graph"},
                },
                {
                    "content": "Beach weather notes for a weekend trip",
                    "metadata": {"entity_name": "weather", "source": "memory_graph"},
                },
            ],
        )

        results = await store.search(
            workspace_id="ws-1",
            query="native daemon control plane",
            top_k=2,
            source_filter="memory_graph",
        )
        assert results
        assert results[0]["source_type"] == "memory_graph"
        assert results[0]["score"] >= results[-1]["score"]
        assert "created_at" in results[0]["metadata"]

        await store.close()

    asyncio.run(scenario())


def test_project_context_works_with_local_vector_backend(tmp_path, monkeypatch):
    monkeypatch.setenv("KESTREL_HOME", str(tmp_path / ".kestrel"))
    monkeypatch.setenv("KESTREL_RUNTIME_MODE", "native")
    monkeypatch.setenv("BRAIN_VECTOR_BACKEND", "local")

    async def scenario():
        store = VectorStore()
        await store.initialize()
        project_context.set_vector_store(store)

        tree_result = {
            "path": "/workspace/librebird",
            "tech_stack": ["Python", "TypeScript"],
            "project": {
                "name": "LibreBird",
                "description": "Autonomous agent platform",
                "dependencies": ["grpcio", "fastify"],
                "scripts": ["test", "build"],
            },
            "summary": {"files": 42, "directories": 8},
            "tree": "packages/\n  brain/\n  gateway/\n",
        }

        memory_id = await project_context.save_project_context(tree_result, workspace_id="ws-2")
        assert memory_id

        recalled = await project_context.recall_project_context("LibreBird", workspace_id="ws-2")
        assert recalled is not None
        assert recalled["project"] == "LibreBird"
        assert "Autonomous agent platform" in recalled["summary"]

        projects = await project_context.list_known_projects(workspace_id="ws-2")
        assert len(projects) == 1
        assert projects[0]["name"] == "LibreBird"

        await store.close()
        project_context.set_vector_store(None)

    asyncio.run(scenario())
