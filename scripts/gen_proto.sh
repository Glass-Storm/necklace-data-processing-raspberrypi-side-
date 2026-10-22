#!/usr/bin/env bash
# Regenerate the ecosys.v1 gRPC stubs from the vendored frozen proto.
#
# The proto is the single source of truth (proto/ecosys/v1/ecosys.proto); the
# generated *_pb2.py / *_pb2_grpc.py are committed and MUST NOT be hand-edited.
# Run this only after re-vendoring the proto from the hub.
#
# Usage:  ./scripts/gen_proto.sh
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

UV="${UV:-/home/chaos/.local/bin/uv}"
if ! command -v "$UV" >/dev/null 2>&1; then
	UV="$(command -v uv)"
fi

# grpc_tools.protoc does NOT emit __init__.py for the generated package path;
# the stubs import `ecosys.v1.ecosys_pb2`, so the package markers must exist.
mkdir -p src/ecosys/v1
touch src/ecosys/__init__.py src/ecosys/v1/__init__.py

"$UV" run python -m grpc_tools.protoc \
	-Iproto \
	--python_out=src \
	--grpc_python_out=src \
	proto/ecosys/v1/ecosys.proto

echo "generated: src/ecosys/v1/ecosys_pb2.py src/ecosys/v1/ecosys_pb2_grpc.py"
