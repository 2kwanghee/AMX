"""하트비트 메트릭 presence 불변식 (rev-B C2).

메트릭을 실은 하트비트는 cpu/mem/disk와 `metrics_reported_at`을 덮어쓰고, 메트릭이
없는 하트비트는 그 값을 **보존해야** 한다. 후자가 핵심이다 — 0으로 밀어버리면
Windows처럼 메트릭을 안 보내는 에이전트가 붙을 때마다 기존 값이 지워진다.

`scripts/verify_metrics_presence.py`가 같은 검사를 하던 스탠드얼론 스크립트였다.
그 스크립트의 존재 이유는 "pytest가 이 환경에서 gRPC 모듈을 수집하지 못한다"였는데
그 전제가 더 이상 사실이 아니다(test_credential_resync.py가 같은 모듈을 쓰며 통과한다).
이 저장소에 CI가 없어 실제로 돌아가는 게이트는 pytest 스위트뿐이므로, 검사를 두 곳에
두는 대신 스위트로 옮겼다.
"""

from __future__ import annotations

import uuid

import pytest

from app.db import get_sessionmaker
from app.grpc import signing
from app.grpc.proto import pb
from app.grpc.server import ControlPlaneServicer
from app.models import Server, Tenant

METRICS = pb.Heartbeat.SystemMetrics(cpu_pct=12.5, mem_pct=63.0, disk_pct=41.2)


@pytest.fixture()
def servicer(app_env):
    return ControlPlaneServicer(
        signing.Signer.from_env_or_generate(), session_factory=get_sessionmaker()
    )


@pytest.fixture()
def server_id(app_env):
    """빈 테넌트에 서버 한 대. 정리는 autouse clean_tables가 맡는다."""
    with get_sessionmaker()() as db:
        tenant = Tenant(name=f"metrics-{uuid.uuid4().hex[:8]}")
        db.add(tenant)
        db.flush()
        server = Server(tenant_id=tenant.id, name=f"srv-{uuid.uuid4().hex[:8]}")
        db.add(server)
        db.flush()
        sid = server.id
        db.commit()
    return sid


def _metrics(server_id):
    with get_sessionmaker()() as db:
        s = db.get(Server, server_id)
        return (s.cpu_pct, s.mem_pct, s.disk_pct, s.metrics_reported_at)


def test_a_fresh_server_reports_no_metrics(server_id):
    assert _metrics(server_id) == (None, None, None, None)


def test_a_heartbeat_carrying_metrics_writes_the_columns(servicer, server_id):
    servicer._touch_last_seen(server_id, pb.Heartbeat(agent_id="ama_test", metrics=METRICS))

    cpu, mem, disk, reported_at = _metrics(server_id)
    assert (cpu, mem, disk) == (12.5, 63.0, 41.2)
    assert reported_at is not None


def test_a_heartbeat_without_metrics_preserves_them(servicer, server_id):
    servicer._touch_last_seen(server_id, pb.Heartbeat(agent_id="ama_test", metrics=METRICS))
    written = _metrics(server_id)

    # HasField 게이트가 없으면 여기서 0으로 밀린다.
    servicer._touch_last_seen(server_id, pb.Heartbeat(agent_id="ama_test"))

    assert _metrics(server_id) == written
