# Runtime Wiring

## One-turn flow

```text
1. CLI receives user message
2. Agent writes user observation to working memory
3. ContextCompiler searches all layers
4. Agent builds messages:
   - system prompt
   - compiled nested memory context
   - available tools
   - user message
5. LLM provider returns either:
   - final answer, or
   - JSON envelope with tool calls
6. ToolRegistry executes allowed tools
7. Agent writes tool outputs/failures to working memory
8. LLM receives tool results
9. Agent returns final answer
10. Agent writes turn summary to episodic memory
11. Agent seals all memory layers
```

## Multi-turn flow

The session ID ties together:

- event log entries
- working memory observations
- episodic summaries
- tool results
- later consolidation candidates

The agent should not depend on an ever-growing chat transcript. The next turn is reconstructed from:

- current user message
- compiled memory context
- recent working-memory state
- optionally the last N messages

## Tool-call format

Until native provider-specific tool calling is hardened, the portable envelope is:

```json
{
  "message": "brief progress note",
  "tool_calls": [
    {"name": "memory.search", "arguments": {"query": "project auth failure", "k": 5}}
  ]
}
```

Codex should upgrade this to native Responses API function calling for OpenAI, while keeping the JSON envelope fallback for provider portability.

## Permission model

Default:

- memory search: allowed
- memory write: allowed except policy writes
- file list/read: allowed inside workspace
- file write: blocked
- shell run: blocked
- git commit/push: blocked

High-risk tool enablement is config-level for MVP and should become interactive approval later.

## Memory sealing

After every turn:

```python
memory.seal_all()
```

Memvid writes should be explicitly sealed. The adapter must avoid accidental overwrites by opening existing files with `use(...)` and creating only missing files.
