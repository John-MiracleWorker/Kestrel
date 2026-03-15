from __future__ import annotations

import importlib
import sys
from pathlib import Path

_SHARED_PATH = Path(__file__).resolve().parents[2] / "shared"
if str(_SHARED_PATH) not in sys.path:
    sys.path.append(str(_SHARED_PATH))

_local_operator = importlib.import_module("local_operator")

AgentProfile = _local_operator.AgentProfile
ArtifactManifest = _local_operator.ArtifactManifest
AutonomyPolicy = _local_operator.AutonomyPolicy
BackgroundSuggestion = _local_operator.BackgroundSuggestion
LearningEvent = _local_operator.LearningEvent
Procedure = _local_operator.Procedure
ResearchSession = _local_operator.ResearchSession
VerifierResult = _local_operator.VerifierResult

__all__ = [
    "AgentProfile",
    "ArtifactManifest",
    "AutonomyPolicy",
    "BackgroundSuggestion",
    "LearningEvent",
    "Procedure",
    "ResearchSession",
    "VerifierResult",
]
