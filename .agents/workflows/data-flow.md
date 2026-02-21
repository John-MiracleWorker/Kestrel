---
description: Data flow patterns between system components including agent communication, tool execution, and memory systems
---

# Data Flow Patterns

## Inter-Agent Communication
- Session-based message routing system with parent-child relationships
- Message types: text, request, response, announce
- Session discovery and tracking mechanisms
- Workspace-scoped message history persistence

## Tool Execution Pipeline
Orchestration layer manages tool lifecycle:
```
Tool Request -> Risk Assessment -> Access Control -> Execution -> Result Capture
```
- Sandboxed code execution via external Hands service
- Security boundary enforcement with workspace isolation
- Language-specific execution environment mapping

## Memory Retrieval Process
- Vector-based similarity search with workspace scoping
- Time-weighted relevance scoring with 10% weekly decay
- Background embedding processing for conversation turns
- Automatic context injection into prompts

## Provider Communication Flow
- Unified streaming interface across multiple LLM providers
- Provider-specific message format transformations
- Tool/function calling protocol adaptations
- Workspace-level configuration management

## Channel Message Routing
- Multi-channel message orchestration (Web, Telegram, Discord)
- Three routing strategies:
  - `same_channel`: Origin channel only
  - `all_channels`: Broadcast mode
  - `prefer_web`: Web interface priority
- Cross-channel identity resolution
- Message deduplication across platforms

## Core Data Flow Paths
```
User Input -> Channel Router -> Agent Session -> Tool Registry -> Execution Service
Memory Retrieval -> Context Injection -> LLM Provider -> Response Generation
Agent Response -> Channel Router -> User Output
```
