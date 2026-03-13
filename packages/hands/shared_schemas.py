from __future__ import annotations

import importlib
import sys
from pathlib import Path

_SHARED_PATH = Path(__file__).resolve().parents[1] / "shared"
if str(_SHARED_PATH) not in sys.path:
    sys.path.append(str(_SHARED_PATH))

_action_event_schema = importlib.import_module("action_event_schema")
_action_receipt_schema = importlib.import_module("action_receipt_schema")

build_action_event = _action_event_schema.build_action_event
build_execution_action_event = _action_event_schema.build_execution_action_event
classify_risk_class = _action_event_schema.classify_risk_class
classify_runtime_class = _action_event_schema.classify_runtime_class
dumps_action_event = _action_event_schema.dumps_action_event
normalize_action_event = _action_event_schema.normalize_action_event
stable_hash = _action_event_schema.stable_hash

build_action_receipt = _action_receipt_schema.build_action_receipt

FAILURE_CLASS_EXECUTION_ERROR = _action_receipt_schema.FAILURE_CLASS_EXECUTION_ERROR
FAILURE_CLASS_ESCALATION_REQUIRED = _action_receipt_schema.FAILURE_CLASS_ESCALATION_REQUIRED
FAILURE_CLASS_NONE = _action_receipt_schema.FAILURE_CLASS_NONE
FAILURE_CLASS_PARTIAL_OUTPUT = _action_receipt_schema.FAILURE_CLASS_PARTIAL_OUTPUT
FAILURE_CLASS_PERMISSION_DENIED = _action_receipt_schema.FAILURE_CLASS_PERMISSION_DENIED
FAILURE_CLASS_SANDBOX_CRASH = _action_receipt_schema.FAILURE_CLASS_SANDBOX_CRASH
FAILURE_CLASS_TIMEOUT = _action_receipt_schema.FAILURE_CLASS_TIMEOUT

__all__ = [
    "FAILURE_CLASS_EXECUTION_ERROR",
    "FAILURE_CLASS_ESCALATION_REQUIRED",
    "FAILURE_CLASS_NONE",
    "FAILURE_CLASS_PARTIAL_OUTPUT",
    "FAILURE_CLASS_PERMISSION_DENIED",
    "FAILURE_CLASS_SANDBOX_CRASH",
    "FAILURE_CLASS_TIMEOUT",
    "build_action_event",
    "build_action_receipt",
    "build_execution_action_event",
    "classify_risk_class",
    "classify_runtime_class",
    "dumps_action_event",
    "normalize_action_event",
    "stable_hash",
]
