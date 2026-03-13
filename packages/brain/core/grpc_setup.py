from __future__ import annotations

import importlib
import sys
from pathlib import Path

import grpc_reflection.v1alpha.reflection as reflection
from grpc_tools import protoc

_THIS_DIR = Path(__file__).resolve().parent
_BRAIN_DIR = _THIS_DIR.parent
_PROTO_PATH = _BRAIN_DIR.parent / "shared" / "proto"
_OUT_DIR = _BRAIN_DIR / "_generated"
_OUT_DIR.mkdir(exist_ok=True)

protoc.main(
    [
        "grpc_tools.protoc",
        f"-I{_PROTO_PATH}",
        f"--python_out={_OUT_DIR}",
        f"--grpc_python_out={_OUT_DIR}",
        "brain.proto",
    ]
)

if str(_OUT_DIR) not in sys.path:
    sys.path.insert(0, str(_OUT_DIR))

brain_pb2 = importlib.import_module("brain_pb2")
brain_pb2_grpc = importlib.import_module("brain_pb2_grpc")

__all__ = ["brain_pb2", "brain_pb2_grpc", "reflection"]
