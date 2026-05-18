# Kestrel Coding Benchmark — DeepSeek v4 Pro via Ollama Cloud

## Summary

- Kestrel: **4/5 (80%)**

- Dominion/OpenClaw reference: **2/5 (40%)**

- Delta: **+2 tasks**, **+40.0 percentage points**

- Total elapsed: **1335.1s**

- Average tool rounds/task: **7.4**


## Per-task results

- **easy-js-duration** (difficulty 1, javascript): **PASS**, 7 rounds, 171.9s
- **medium-js-lru** (difficulty 2, javascript): **PASS**, 8 rounds, 142.6s
- **hard-js-async-pool** (difficulty 3, javascript): **PASS**, 11 rounds, 257.4s
- **expert-js-json-patch** (difficulty 4, javascript): **PASS**, 9 rounds, 454.9s
- **expert-py-template-engine** (difficulty 5, python): **FAIL**, 2 rounds, 308.4s
  - Failure: `Provider error (TimeoutError): The read operation timed out`

## Interpretation

Kestrel + DeepSeek v4 Pro solved 4/5 Dominion-style coding tasks. The only failure was the hardest Python template-engine task, and it failed due to provider timeout after file listing/reading rather than a failed test assertion. This exceeds the Dominion/OpenClaw reference score of 2/5.
