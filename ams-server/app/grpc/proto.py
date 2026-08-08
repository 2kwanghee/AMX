"""Import shim for the generated protobuf/gRPC stubs.

`contracts/gen/python/amx_pb2_grpc.py` does a flat ``import amx_pb2``, so that
directory must be on ``sys.path`` before the modules load. The generated code is
the frozen P0 contract and is never edited (`contracts/` is out of scope); this
module is the single place that puts it on the path and re-exports it, so the
rest of ams-server imports ``from app.grpc.proto import pb, pb_grpc`` and never
touches ``sys.path`` itself.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


def _contracts_python_dir() -> Path:
    override = os.environ.get("AMX_CONTRACTS_PYTHON_PATH", "").strip()
    if override:
        return Path(override)
    # app/grpc/proto.py -> app/grpc -> app -> ams-server -> repo root
    root = Path(__file__).resolve().parents[3]
    return root / "contracts" / "gen" / "python"


_gen_dir = str(_contracts_python_dir())
if _gen_dir not in sys.path:
    sys.path.insert(0, _gen_dir)

import amx_pb2 as pb  # noqa: E402
import amx_pb2_grpc as pb_grpc  # noqa: E402

__all__ = ["pb", "pb_grpc"]
