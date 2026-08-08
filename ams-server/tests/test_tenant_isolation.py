"""The §5.1 isolation invariant, tested at the layer that enforces it.

These tests bypass the service layer entirely and write raw SQL. That is the
point: §5.1 claims the boundary holds "애플리케이션 검증에 의존하지 않는"
— without relying on application checks — so a test that went through the API
would prove only that the API is careful, not that the database is.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError


def _seed(engine, *, tenants=2) -> dict:
    ids = {
        "tenant": [uuid.uuid4() for _ in range(tenants)],
        "account": [uuid.uuid4() for _ in range(tenants)],
        "server": [uuid.uuid4() for _ in range(tenants)],
    }
    with engine.begin() as conn:
        for i in range(tenants):
            conn.execute(
                text("INSERT INTO tenants (id, name, status) VALUES (:id, :name, 'active')"),
                {"id": ids["tenant"][i], "name": f"tenant-{i}"},
            )
            conn.execute(
                text(
                    "INSERT INTO accounts (id, tenant_id, email, credential_type, status) "
                    "VALUES (:id, :tid, :email, 'oauth', 'available')"
                ),
                {"id": ids["account"][i], "tid": ids["tenant"][i], "email": f"a{i}@example.com"},
            )
            conn.execute(
                text(
                    "INSERT INTO servers (id, tenant_id, name, switch_mode, status) "
                    "VALUES (:id, :tid, :name, 'auto', 'offline')"
                ),
                {"id": ids["server"][i], "tid": ids["tenant"][i], "name": f"srv-{i}"},
            )
    return ids


def _insert_assignment(conn, *, tenant, account, server, state="pending"):
    conn.execute(
        text(
            "INSERT INTO assignments (id, tenant_id, account_id, server_id, state, pinned) "
            "VALUES (:id, :tid, :aid, :sid, :state, false)"
        ),
        {
            "id": uuid.uuid4(),
            "tid": tenant,
            "aid": account,
            "sid": server,
            "state": state,
        },
    )


def test_cross_tenant_assignment_is_rejected_by_the_database(engine):
    ids = _seed(engine)

    # Tenant 0's account, tenant 1's server. Whichever tenant_id the row
    # claims, one of the two composite foreign keys cannot resolve.
    with pytest.raises(IntegrityError) as first:
        with engine.begin() as conn:
            _insert_assignment(
                conn,
                tenant=ids["tenant"][0],
                account=ids["account"][0],
                server=ids["server"][1],
            )
    assert "fk_assignments_server_tenant" in str(first.value)

    with pytest.raises(IntegrityError) as second:
        with engine.begin() as conn:
            _insert_assignment(
                conn,
                tenant=ids["tenant"][1],
                account=ids["account"][0],
                server=ids["server"][1],
            )
    assert "fk_assignments_account_tenant" in str(second.value)


def test_same_tenant_assignment_is_accepted(engine):
    ids = _seed(engine)
    with engine.begin() as conn:
        _insert_assignment(
            conn, tenant=ids["tenant"][0], account=ids["account"][0], server=ids["server"][0]
        )
    with engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM assignments")).scalar() == 1


def test_second_live_assignment_for_one_account_is_rejected(engine):
    ids = _seed(engine)
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO servers (id, tenant_id, name, switch_mode, status) "
                "VALUES (:id, :tid, 'srv-0b', 'auto', 'offline')"
            ),
            {"id": (second_server := uuid.uuid4()), "tid": ids["tenant"][0]},
        )
        _insert_assignment(
            conn,
            tenant=ids["tenant"][0],
            account=ids["account"][0],
            server=ids["server"][0],
            state="active",
        )

    with pytest.raises(IntegrityError) as exc:
        with engine.begin() as conn:
            _insert_assignment(
                conn,
                tenant=ids["tenant"][0],
                account=ids["account"][0],
                server=second_server,
            )
    assert "uq_assignments_active_account" in str(exc.value)


def test_reassignment_is_allowed_once_the_previous_one_is_detached(engine):
    ids = _seed(engine)
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO servers (id, tenant_id, name, switch_mode, status) "
                "VALUES (:id, :tid, 'srv-0b', 'auto', 'offline')"
            ),
            {"id": (second_server := uuid.uuid4()), "tid": ids["tenant"][0]},
        )
        _insert_assignment(
            conn,
            tenant=ids["tenant"][0],
            account=ids["account"][0],
            server=ids["server"][0],
            state="detached",
        )
        _insert_assignment(
            conn,
            tenant=ids["tenant"][0],
            account=ids["account"][0],
            server=second_server,
            state="pending",
        )

    with engine.connect() as conn:
        # The detached row survives — §5.2 keeps it for audit.
        assert conn.execute(text("SELECT count(*) FROM assignments")).scalar() == 2


def test_many_detached_rows_for_one_account_coexist(engine):
    """The partial index must not degenerate into "one row ever"."""
    ids = _seed(engine)
    with engine.begin() as conn:
        for _ in range(3):
            _insert_assignment(
                conn,
                tenant=ids["tenant"][0],
                account=ids["account"][0],
                server=ids["server"][0],
                state="detached",
            )
    with engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM assignments")).scalar() == 3
