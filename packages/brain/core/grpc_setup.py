import os
import sys

# We use a runtime proto loading approach so we don't need compiled stubs
import grpc_reflection.v1alpha.reflection as reflection
from grpc_tools.protoc import main as protoc_main

# Resolve paths relative to this file (packages/brain/core/grpc_setup.py)
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_BRAIN_DIR = os.path.dirname(_THIS_DIR)  # packages/brain/

# Load proto definition at runtime
# In Docker: /app/core/../ = /app/, then ../shared/proto = /shared/proto
# Locally: packages/brain/core/../ = packages/brain/, then ../shared/proto = packages/shared/proto
PROTO_PATH = os.path.join(_BRAIN_DIR, "../shared/proto")
BRAIN_PROTO = os.path.join(PROTO_PATH, "brain.proto")

# Dynamic proto loading
from grpc_tools import protoc
import importlib

# Generate Python stubs in the _generated dir at the brain package root
out_dir = os.path.join(_BRAIN_DIR, "_generated")
os.makedirs(out_dir, exist_ok=True)

protoc.main([
    "grpc_tools.protoc",
    f"-I{PROTO_PATH}",
    f"--python_out={out_dir}",
    f"--grpc_python_out={out_dir}",
    "brain.proto",
])

# Import generated modules
sys.path.insert(0, out_dir)
import brain_pb2
import brain_pb2_grpc

# Explicitly export them so they can be imported cleanly
__all__ = ["brain_pb2", "brain_pb2_grpc", "reflection"]
