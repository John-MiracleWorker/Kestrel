# Test Matrix

## Unit tests

- dataclass validation
- memory thresholds
- retrieval ranking
- context compiler budget enforcement
- consolidation promotion rules
- tool schemas
- path safety
- permission gates
- provider parser

## Runtime tests

- one-turn mock chat
- tool-call loop
- max tool rounds stop
- memory write on user message
- memory write on tool result
- episodic summary write
- event log write

## Integration tests

Gated by env vars:

```bash
RUN_MEMVID_INTEGRATION=1 pytest -q tests/integration
RUN_OPENAI_INTEGRATION=1 pytest -q tests/integration
```

Memvid integration must create temp `.mv2` files, write records, seal, verify, search, close, reopen, search again, and ensure records persist.

OpenAI integration must call the provider with a short prompt and verify text response shape. Native tool calling integration should be mocked first, real later.

## Golden evals

Golden evals live under `golden/` and should be runnable with:

```bash
python scripts/run_golden_evals.py --backend memory
python scripts/run_golden_evals.py --backend memvid
```

A golden eval is not just a unit test. It asserts agent behavior across turns.
