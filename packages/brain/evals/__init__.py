"""
Evaluation Harness — measure and track agent performance over time.

Components:
  - EvalScenario: Defines a test scenario with success criteria
  - EvalRunner: Executes scenarios and collects metrics
  - EvalMetrics: Aggregates and queries performance data
"""

from evals.scenarios import EvalScenario, BUILT_IN_SCENARIOS
from evals.runner import EvalRunner, EvalResult
from evals.metrics import EvalMetrics

__all__ = [
    "EvalScenario",
    "EvalRunner",
    "EvalResult",
    "EvalMetrics",
    "BUILT_IN_SCENARIOS",
]
