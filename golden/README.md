# Golden Evals

Golden evals are executable through `scripts/run_golden_evals.py`. They assert agent behavior across turns rather than testing a single function in isolation.

Run the deterministic fast path:

```bash
python scripts/run_golden_evals.py --backend memory --provider mock
```

Run the Memvid path when `memvid-sdk` is installed:

```bash
RUN_MEMVID_INTEGRATION=1 python scripts/run_golden_evals.py --backend memvid --provider mock --memory-dir /tmp/kestrel-memvid-golden
```

Current golden coverage should protect:

- user correction recall
- prior-failure retrieval
- procedural memory formation only after repeated validated success
- policy-write refusal from ordinary events
- workspace path escape refusal
- shell blocking without enablement
- `.mv2` verification
- context packing under budget

When adding new cases, keep them deterministic under the mock provider and isolate Memvid-backed cases into their own memory/log directories to avoid `.mv2` lock contention.
