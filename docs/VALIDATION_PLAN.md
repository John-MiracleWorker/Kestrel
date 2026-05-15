# Validation Plan

## Test pyramid

### Unit tests

Run:

```bash
pytest
```

Coverage goals:

- Model validation.
- Layer confidence gates.
- Retrieval contract.
- Context compiler budget behavior.
- Consolidation promotion thresholds.
- Golden retrieval validator.

### Memvid smoke test

Run after installing Memvid:

```bash
pip install -e .[dev,memvid]
python scripts/bootstrap_memory.py --backend memvid --memory-dir ./memory
python scripts/run_validation.py --backend memvid --memory-dir ./memory
nested-memvid compile-context --backend memvid --objective "Explain how policy promotion works"
```

### Golden retrieval tests

Use `scripts/run_validation.py` for basic smoke validation. Expand this into a `golden/` directory with JSON cases:

```json
{
  "name": "auth profile failure",
  "query": "provider specific auth startup failure",
  "expected_terms": ["auth profile", "provider"],
  "layers": ["episodic", "semantic", "procedural"]
}
```

### Promotion validation

Every proposed promotion must include:

- source memory id,
- target layer,
- validation score,
- repeat count,
- evidence refs,
- reason.

Policy promotions require repeat_count >= 5 and validation_score >= 0.95.

### Context validation

The compiled context should be checked for:

- objective present,
- relevant memories included,
- scores/confidence present,
- evidence present when available,
- total budget respected,
- no unrelated layers bloating the prompt.

### Performance validation

Track:

- retrieval latency per layer,
- context compile latency,
- number of hits per layer,
- context chars per layer,
- cache hit rate,
- failed golden questions.

For local dev, target sub-100ms retrieval across warmed local capsules before LLM calls. That target is not a law of physics; it is a useful smell test.

## Failure handling

If validation fails:

1. Do not promote new memory.
2. Write an episodic failure memory.
3. Add or update a golden question.
4. Re-run retrieval with fixed top-k and adaptive mode.
5. Inspect source frames before changing policy/procedural memory.
