---
description: AI agent architecture including memory, reasoning, coordination, and execution systems
---

# Agent Architecture

The agent architecture implements a sophisticated multi-component autonomous system.

## Core Agent Systems
- **Evidence Chain**: Tracks decision trails with categorized evidence nodes and relevance scoring
- **Memory Graph**: Semantic knowledge network with time-based decay and custom traversal
- **Council System**: Multi-agent debate mechanism with role-based specialists and weighted voting
- **Reflection System**: "Red team" critique framework with risk assessment and mitigation
- **Persona Learning**: Adapts to user preferences across code style, communication, workflow

## Provider Integration Layer
- Unified LLM provider management across OpenAI, Anthropic, Google
- Provider-specific message transformations and tool protocols
- Custom token streaming implementations per provider
- Time-based retry logic with exponential backoff

## Multi-Channel Communication
- Cross-platform message routing (Web, Telegram, Discord)
- Channel-specific adapters with platform capabilities
- Identity linking and deduplication across channels
- Custom notification preference system

## Memory & RAG Pipeline
- Conversation embedding with background processing
- Time-weighted relevance scoring (10% decay/week)
- Context formatting and injection
- Automatic prompt augmentation
- Workspace-scoped vector search

## Execution Environment
- Sandboxed skill execution with Docker isolation
- Resource limits and network controls
- Security audit logging
- Tool approval workflows
- Risk-based execution guardrails

## Key File Locations
- `brain/agent/reflection.py` — Reflection system
- `brain/agent/memory_graph.py` — Memory graph
- `brain/agent/council.py` — Council system
- `brain/providers/cloud.py` — Provider management
- `gateway/src/channels/registry.ts` — Channel coordination
- `hands/executor.py` — Execution environment
