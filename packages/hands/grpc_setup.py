from __future__ import annotations

import importlib
import sys
from pathlib import Path

from grpc_tools import protoc

_THIS_DIR = Path(__file__).resolve().parent
_PROTO_PATH = _THIS_DIR.parent / "shared" / "proto"
_OUT_DIR = _THIS_DIR / "_generated"
_OUT_DIR.mkdir(exist_ok=True)

protoc.main(
    [
        "grpc_tools.protoc",
        f"-I{_PROTO_PATH}",
        f"--python_out={_OUT_DIR}",
        f"--grpc_python_out={_OUT_DIR}",
        "hands.proto",
    ]
)

if str(_OUT_DIR) not in sys.path:
    sys.path.insert(0, str(_OUT_DIR))

hands_pb2 = importlib.import_module("hands_pb2")
hands_pb2_grpc = importlib.import_module("hands_pb2_grpc")

__all__ = ["hands_pb2", "hands_pb2_grpc"]
