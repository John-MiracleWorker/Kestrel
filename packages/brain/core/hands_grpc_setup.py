import os
import sys

from grpc_tools import protoc

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_BRAIN_DIR = os.path.dirname(_THIS_DIR)
PROTO_PATH = os.path.join(_BRAIN_DIR, "../shared/proto")

out_dir = os.path.join(_BRAIN_DIR, "_generated_hands")
os.makedirs(out_dir, exist_ok=True)

protoc.main(
    [
        "grpc_tools.protoc",
        f"-I{PROTO_PATH}",
        f"--python_out={out_dir}",
        f"--grpc_python_out={out_dir}",
        "hands.proto",
    ]
)

sys.path.insert(0, out_dir)
import hands_pb2  # type: ignore
import hands_pb2_grpc  # type: ignore

__all__ = ["hands_pb2", "hands_pb2_grpc"]
