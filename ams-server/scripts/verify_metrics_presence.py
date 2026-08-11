#!/usr/bin/env python3
"""Verify the heartbeat metrics presence invariant end-to-end against a DB.

The pytest gRPC suite cannot collect in this environment (grpcio does not import
under pytest's collection hook here — the modules under tests/test_grpc_*.py
error out), so this standalone script exercises the same handler path directly
through a sessionmaker, exactly as rev-B C2 asked. It is a runnable check, not a
pytest test, precisely so it does not depend on the broken collection.

It asserts, against a throwaway tenant+server it creates and deletes:

  * a heartbeat carrying SystemMetrics overwrites cpu/mem/disk + metrics_reported_at;
  * a heartbeat WITHOUT metrics preserves those columns (HasField gate) rather
    than zeroing them — the core presence invariant.

Exits 0 on PASS, 1 on FAIL, so it is usable as a CI/manual gate.

    set -a; . ../.amx-dev/dev.env; set +a
    AMX_GRPC_ALLOW_INSECURE=1 AMX_ALLOW_RAW_KEK=1 \\
        uv run python scripts/verify_metrics_presence.py
"""

from __future__ import annotations

import sys
import uuid

from app.db import get_sessionmaker
from app.grpc import signing
from app.grpc.proto import pb
from app.grpc.server import ControlPlaneServicer
from app.models import Server, Tenant


def main() -> int:
    sm = get_sessionmaker()
    servicer = ControlPlaneServicer(signing.Signer.from_env_or_generate(), session_factory=sm)

    with sm() as db:
        tenant = Tenant(name=f"metrics-check-{uuid.uuid4().hex[:8]}")
        db.add(tenant)
        db.flush()
        server = Server(tenant_id=tenant.id, name=f"srv-{uuid.uuid4().hex[:8]}")
        db.add(server)
        db.flush()
        server_id, tenant_id = server.id, tenant.id
        db.commit()

    def read():
        with sm() as db:
            s = db.get(Server, server_id)
            return (s.cpu_pct, s.mem_pct, s.disk_pct, s.metrics_reported_at)

    ok = True
    try:
        if read() != (None, None, None, None):
            print("FAIL: columns not NULL on a fresh server")
            ok = False

        # metrics-bearing heartbeat overwrites the trio.
        hb = pb.Heartbeat(
            agent_id="ama_test",
            metrics=pb.Heartbeat.SystemMetrics(cpu_pct=12.5, mem_pct=63.0, disk_pct=41.2),
        )
        servicer._touch_last_seen(server_id, hb)
        after1 = read()
        if (after1[0], after1[1], after1[2]) != (12.5, 63.0, 41.2) or after1[3] is None:
            print(f"FAIL: metrics heartbeat did not write columns: {after1}")
            ok = False

        # metrics-less heartbeat must preserve the columns (presence gate).
        servicer._touch_last_seen(server_id, pb.Heartbeat(agent_id="ama_test"))
        after2 = read()
        if after2 != after1:
            print(f"FAIL: metrics-less heartbeat mutated columns: {after1} -> {after2}")
            ok = False
    finally:
        with sm() as db:
            db.delete(db.get(Server, server_id))
            db.delete(db.get(Tenant, tenant_id))
            db.commit()

    print("RESULT:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
