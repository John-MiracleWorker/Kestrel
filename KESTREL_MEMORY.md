# Libre Bird / Kestrel Memory

This document serves as a persistent memory reference for the **Libre Bird** platform and its autonomous agent engine, **Kestrel**. It synthesizes details from past development conversations, describing the architecture, tools, and UI.

## 1. Libre Bird Platform Overview

Libre Bird is the overarching project—a comprehensive AI platform that features a rich UI, a multi-channel backend, and extensive local inference capabilities.

- **Frontend (`packages/web`, `packages/gateway/frontend`)**: Built with Vite, utilizing an "aurora design system". The frontend connects via WebSocket and JWT authentication, hiding the application until login.
- **Mac Native App**: The project can be run as a native macOS application (`.app` bundle) using `pywebview`. It features a custom app launcher (`app.py`), static file serving, and a clean shutdown mechanism.
- **Voice Interactivity**: Integrated voice input with wake word detection ("Hey Libre"), sending audio to the backend for processing.
- **Model Support**: Supports multiple backend providers ranging from Local Models (e.g., Moshi conversational AI, MLX for Apple Silicon) to Cloud LLMs (e.g., Gemini Flash 3).

## 2. Kestrel Agent Loop

**Kestrel** is the brain of Libre Bird—a fully autonomous agent engine capable of self-reflection, robust tool calling, and multi-agent coordination.

- **The Agent Loop (`brain/agent/loop.py`)**: Kestrel natively understands available tools, dynamically plans its steps, executes actions, and reflects on them. Accepts an `event_callback` for real-time UI visibility.
- **Agent Visibility System**: A full-stack system for showing users how Kestrel thinks:
    - **Architecture**: Modules emit structured events via async `event_callback` → `asyncio.Queue` in `server.py` → proto metadata with `agent_status: "agent_activity"` → gateway's `WebChannelAdapter` → frontend WebSocket as `tool_activity` messages.
    - **Backend Events** (all defined in `types.py` as `AGENT_ACTIVITY`):
        - `memory_recalled` — from `loop.py` Phase 0, count + entity terms + preview
        - `lessons_loaded` — from `loop.py` Phase 0, past task lessons count
        - `skill_activated` — from `server.py`, workspace skill count
        - `plan_created` — from `loop.py` Phase 1, step descriptions
        - `council_started/opinion/debate/verdict` — from `council.py`, member votes & consensus
        - `delegation_started/progress/complete` — from `coordinator.py`, sub-agent tracking
        - `reflection_started/critique/verdict` — from `reflection.py`, confidence scores
        - `evidence_summary` — from `loop.py` completion, decision records
        - `token_usage` — from `loop.py` completion, total tokens + iterations
    - **Frontend** (`ChatView.tsx`): `KestrelProcessBar` component renders a compact horizontal bar above each response: `🧠 memories → 📋 plan → ⚡ tools → 🤔 council → 🎯 confidence → 💰 tokens`. Each phase is a clickable pill that expands details. Activities accumulate in `useChat.ts` via `agentActivities[]` ref.
- **Systematic Guardrails & Honesty**: The system prompt and memory retrieval (`brain.proto`, memory graph) explicitly instruct Kestrel on its own capabilities (what it can and cannot see), forcing it to give honest "no data" responses instead of fabricating answers. Fact supersession and LLM-as-a-Judge concepts ensure high accuracy.
- **Sandboxing & execution (`packages/hands/executor.py`)**: Tool execution happens in a sandboxed Docker container environment (`SandboxManager`) with resource limits, security audit logging, and risk-based action approval (`GuardrailsSystem`).

## 3. Subsystems & Architecture (`server.py`)

At startup, `server.py` initializes a unified architecture comprising several distinct subsystems:

- **CommandParser**: Intercepts slash commands (e.g., `/status`, `/help`, `/model`) for instant execution without burning LLM tokens.
- **MetricsCollector**: Tracks token usage and operational costs.
- **SessionManager**: Manages agent-to-agent and cross-channel message routing (Web, Telegram, Discord, WhatsApp).
- **WorkflowRegistry**: Stores built-in task templates that the agent can retrieve and execute dynamically.
- **SkillManager**: Binds to a Tool Registry that dynamically loads workspace or user-created tools.

## 4. Built-in Tools & Capabilities

Libre Bird has a massive footprint of registered tools integrated into the `ToolRegistry` (18+ distinct tools), including:

- **Web Interaction**: `trafilatura` for reading and parsing web content.
- **Information Retrieval**: RAG/knowledge module powered by `ChromaDB` for searching internal document semantics.
- **System Execution**: Bash shell execution, Python code execution, and filesystem reading/searching.
- **Media Generation**: Local image generation using `mflux` (MLX Stable Diffusion).
- **Audio Output**: A callable Text-to-Speech skill loop.
- **Memory & Human Help**: Skills for modifying the Memory Graph and directly seeking human input/approval when stuck.

## 5. Known Project Rules

_(Syncs directly with `AGENTS.md` and `.cursorrules`)_

- Explanations must be backed by evidence from code and logs.
- Only modify directly relevant code. Small, incremental steps are heavily encouraged.
- No placeholder code (`# ...`) is allowed—always rewrite the entire snippet.
