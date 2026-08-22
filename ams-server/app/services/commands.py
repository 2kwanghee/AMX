"""Command outbox service — design note §2, §5.

The REST transition actions call the ``request_*`` helpers here. Each one
re-checks the tenant (via :func:`inventory.get_assignment`, the service-layer
half of §7's defence in depth), validates the current assignment state, writes
one ``agent_commands`` row, and advances the assignment. The gRPC session
process drains the row and :mod:`app.services.reconcile` closes the loop on the
agent's ack.
"""

from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session

from app.core import crypto
from app.core.errors import conflict
from app.models import Account, AgentCommand, Assignment, Server
from app.services import alerts, inventory

# D2 sent-未ack recovery (recovery-architecture §2). A command the poll loop
# pushed but the agent never acked stays ``sent`` forever, stranding its
# assignment in delivering/recalling. The sent-timeout sweeper re-queues it (same
# command_id, idempotent) up to MAX_SEND_ATTEMPTS, then fails it and reverts the
# assignment to a re-issuable resting state. Timeout is 2–3× the AMA heartbeat
# (default 90s = 3×30s) so an ordinary in-flight ack is never mistaken for a loss.
SENT_ACK_TIMEOUT_SECONDS = float(os.environ.get("AMX_SENT_ACK_TIMEOUT", "90"))
MAX_SEND_ATTEMPTS = int(os.environ.get("AMX_MAX_SEND_ATTEMPTS", "5"))

# D1 recall-failure recovery (recovery-architecture §1). A failed recall settles
# to ``recalling`` (pending_command_id NULL) and REST ``:recall`` re-arms it. This
# caps the manual re-arms so a permanently-failing recall cannot be re-issued
# forever; past the cap the action 409s and only opens a ``recall_failed`` alert.
MAX_RECALL_RETRIES = int(os.environ.get("AMX_MAX_RECALL_RETRIES", "3"))

# D3 queued-never-sent recovery. ``deliver`` has no server-online gate, so a
# command aimed at a server whose agent is missing or too old to open a session
# is never polled: it stays ``queued`` forever, ``pending_command_id`` never
# clears, and the console shows "동기화중" indefinitely. This sweep ages out a
# ``queued`` row past the timeout the same way the sent-ack sweeper ages out a
# stuck ``sent`` row. Two-tier: a server whose agent has never once connected
# (``last_seen_at`` NULL) is swept on the short ``QUEUED_TIMEOUT_SECONDS`` — no
# session has ever formed, so there is nothing "in progress" to wait out. A
# server that has connected before but is between sessions (restart,
# maintenance) gets the much longer ``QUEUED_STALE_SECONDS`` so an ordinary
# reconnect still delivers the queued command instead of losing the race to
# the sweep. ``QUEUED_STALE_SECONDS`` must stay above the pool chain's own
# step timeout (``pool_chain_step_timeout_minutes``, default 600s) — otherwise
# this sweep would fail a command the pool chain's own expiry check
# (``pool.py`` ``_expired``) hasn't given up on yet, and the chain would just
# re-issue a fresh deliver into the same stall.
QUEUED_TIMEOUT_SECONDS = float(os.environ.get("AMX_QUEUED_TIMEOUT", "180"))
QUEUED_STALE_SECONDS = float(os.environ.get("AMX_QUEUED_STALE_TIMEOUT", "1800"))

# Command types that never set assignment.pending_command_id (§6.3: non-state
# commands). For these a None/mismatched marker is not a settle/supersede signal —
# they have no successor — so their cap-exhausted final failure alerts regardless
# of the marker. Marker-setting types (deliver/recall/activate/deactivate) instead
# treat None (settled, e.g. _settle_recall_detached) or a mismatch (superseded) as
# "already handled elsewhere" and suppress the alert.
_MARKERLESS_COMMAND_TYPES = frozenset({"switch_now"})


def _now() -> datetime:
    return datetime.now(UTC)


def _new_command_id() -> str:
    return "cmd_" + crypto.new_token(16)


def enqueue(
    db: Session,
    *,
    assignment: Assignment,
    command_type: str,
    payload: dict,
) -> AgentCommand:
    """Insert a queued outbox row for ``assignment``'s server and tenant."""
    command = AgentCommand(
        tenant_id=assignment.tenant_id,
        server_id=assignment.server_id,
        assignment_id=assignment.id,
        command_id=_new_command_id(),
        command_type=command_type,
        payload=payload,
        status="queued",
    )
    db.add(command)
    return command


def enqueue_server(
    db: Session,
    *,
    tenant_id: uuid.UUID,
    server_id: uuid.UUID,
    command_type: str,
    payload: dict,
) -> AgentCommand:
    """Insert a queued server-scoped outbox row (assignment_id NULL).

    For the non-state session-control commands (set_mode / req_report /
    set_policy) that name a server, not an assignment. The tenant tie is carried
    structurally by the composite ``(server_id, tenant_id)`` foreign key.
    """
    command = AgentCommand(
        tenant_id=tenant_id,
        server_id=server_id,
        assignment_id=None,
        command_id=_new_command_id(),
        command_type=command_type,
        payload=payload,
        status="queued",
    )
    db.add(command)
    return command


# -- REST-facing transitions --------------------------------------------------
def request_deliver(
    db: Session, tenant_id: uuid.UUID, assignment_id: uuid.UUID
) -> Assignment:
    assignment = inventory.get_assignment(db, tenant_id, assignment_id)
    if assignment.state != "pending":
        raise conflict(
            "assignment.not_deliverable",
            f"deliver requires state 'pending'; assignment is '{assignment.state}'.",
        )
    # D3 queued-never-sent recovery: a server whose agent has never once
    # connected (``last_seen_at`` NULL) will never poll for this command, so
    # reject up front instead of letting it sit ``queued``. This does NOT cover
    # a too-old agent: ``_authenticate``/``_touch_server`` (grpc/server.py)
    # stamps ``last_seen_at`` on a successful enroll/credential auth *before*
    # the version/KEK check that can still abort the session with
    # FAILED_PRECONDITION, so an incompatible agent that got that far already
    # has a non-NULL ``last_seen_at`` and passes this check — only the queued
    # sweep (:func:`sweep_queued_timeouts`) catches that case, via the longer
    # ``QUEUED_STALE_SECONDS`` tier. ``status == "offline"`` alone is NOT
    # rejected here either — that also covers an agent that connected before
    # and is merely between sessions, which recovers on its own and should not
    # block deliver.
    server = inventory.get_server(db, tenant_id, assignment.server_id)
    if server.last_seen_at is None:
        raise conflict(
            "assignment.server_never_connected",
            f"deliver requires a server the agent has connected to at least "
            f"once; server '{server.id}' has never connected.",
        )
    # `pinned` is AMS-internal and is translated into desired_status here rather
    # than shipped on the wire (proto DeliverAccount §5.2): a pinned assignment
    # is delivered inactive so it is installed but excluded from rotation.
    desired_status = "inactive" if assignment.pinned else "active"
    command = enqueue(
        db,
        assignment=assignment,
        command_type="deliver",
        payload={"desired_status": desired_status},
    )
    assignment.state = "delivering"
    assignment.pending_command_id = command.command_id
    assignment.last_error = None
    assignment.updated_at = _now()
    db.commit()
    db.refresh(assignment)
    return assignment


def recall_purges_local_copy(db: Session, assignment: Assignment) -> bool:
    """Whether this assignment's recall must wipe the agent's local copy.

    O2 결정 변경(2026-08-14, 사용자 지시): 회수 = 해당 서버에서 계정 완전
    분리. 이력은 detached 할당 행과 이벤트로만 남기고, 로컬 복사본은 항상
    제거한다. 따라서 provider 무관 항상 True.

    이전 O2 기본값은 보존(preservation)이었다: Claude 회수는 자격증명을
    disable만 하고 매니페스트 레코드를 INACTIVE로 남겨 재할당을 싸게 했다.
    그러나 그 잔존이 usage 보고에 INACTIVE로 계속 실려 reconcile의
    detached↔inactive 불일치와 비용의 옛 서버 배분을 유발했다. 회수의 계약을
    "완전 분리, 이력만 보존"으로 통일하면서 disable 경로는 폐기한다.

    Codex는 애초에 purge가 필수였다: config home의 identity sidecar가 남으면
    bridge의 Add가 다른 email을 거부(codex_single_account, bridge.go:131)해
    호스트가 첫 계정에 영구히 묶인다. purge는 에이전트를 Remove로 라우팅해
    sidecar를 삭제(bridge.go:218)하고 호스트를 해방한다. 이제 Claude도 동일.

    함수 시그니처는 유지한다(호출부 commands.py request_recall,
    reconcile.py가 그대로 사용).
    """
    return True


def _settle_recall_pending_to_detached(db: Session, assignment: Assignment) -> Assignment:
    """Settle a never-delivered ``pending`` assignment straight to ``detached``.

    Equivalent to ``reconcile._settle_recall_detached`` (state-only settle: no
    agent command, since there is no remote install to recall) — duplicated
    here rather than imported because ``reconcile`` imports this module, and
    the reverse import would be circular.

    D2 잔재 보호: ``last_error`` 가 ``"sent_ack_timeout"`` 이면 이 pending은
    D2 sent-ack 스위퍼(``sweep_sent_timeouts``)가 ``sent`` 상태에서 되돌린
    것이다 — 에이전트가 페이로드를 이미 가져가 설치까지 했지만 ack만 유실됐을
    수 있으므로, 원격에 실제 잔재가 남아 있을 가능성을 배제할 수 없다. 이
    경우 ``resolve_server_account_alerts`` 를 건너뛰어 ``command_send_failed``
    경보를 열린 채 남기고 ``last_error`` 도 지우지 않는다 — 잔재 가능성의
    유일한 가시 신호이기 때문이다. detach 전이와 계정 available 복귀는
    그대로 수행한다(운영자가 명시적으로 회수를 지시했으므로). 에이전트가 다시
    붙었을 때 실제로 계정이 살아있다면 reconcile의 drift 감지가
    (state=detached인데 actual != ABSENT → CORRECTION_RECALL,
    reconcile.py:405-406) 잔재를 발견해 정리한다 — 코드로 보장되지 않는
    전제이므로 여기 남긴다. 어느 경우든 ack된 적이 없으므로 ``acked_at`` 은
    찍지 않는다."""
    stranded_install = assignment.last_error == "sent_ack_timeout"
    assignment.state = "detached"
    assignment.pending_command_id = None
    if not stranded_install:
        assignment.last_error = None
    assignment.recall_retry_count = 0
    assignment.updated_at = _now()
    alerts.resolve(
        db,
        server_id=assignment.server_id,
        kind="recall_failed",
        account_id=assignment.account_id,
    )
    if not stranded_install:
        alerts.resolve_server_account_alerts(
            db,
            server_id=assignment.server_id,
            account_id=assignment.account_id,
        )
    account = db.scalar(
        select(Account).where(
            Account.id == assignment.account_id, Account.tenant_id == assignment.tenant_id
        )
    )
    if account is not None and account.status == "assigned":
        account.status = "available"
    db.commit()
    db.refresh(assignment)
    return assignment


def request_recall(
    db: Session, tenant_id: uuid.UUID, assignment_id: uuid.UUID, *, force: bool = False
) -> Assignment:
    assignment = inventory.get_assignment(db, tenant_id, assignment_id)
    # A pending assignment was never delivered, so there is nothing on the agent
    # side to recall — no command is ever issued for it. Settle it straight to
    # detached, the same terminal state a successful recall converges to,
    # instead of enqueueing a command a device never asked for. This mirrors
    # reconcile._settle_recall_detached (state-only settle, no command) rather
    # than the in-flight recall path below; that helper cannot be imported here
    # (app.services.reconcile imports app.services.commands, so the reverse
    # import would be circular), so the equivalent settlement is inlined below.
    if assignment.state == "pending":
        return _settle_recall_pending_to_detached(db, assignment)
    # D1 manual escape hatch (recovery-architecture §1): a settled recalling
    # (ack lost, pending_command_id NULL) is a stranded recall an operator must be
    # able to re-arm. An in-flight recalling (pending_command_id set) stays
    # rejected — its command is still live.
    settled_recalling = (
        assignment.state == "recalling" and assignment.pending_command_id is None
    )
    if (
        assignment.state not in ("delivering", "active", "inactive", "quarantined")
        and not settled_recalling
    ):
        raise conflict(
            "assignment.not_recallable",
            f"recall requires an installed assignment; state is '{assignment.state}'.",
        )
    # D1 retry cap: a settled recalling is a re-arm of a recall that already failed
    # once, so bound it. Past MAX_RECALL_RETRIES the recall is treated as durably
    # failed — 409 and a ``recall_failed`` alert, no new command. A first recall
    # from an installed state is attempt 0 and resets any stale counter.
    # ``force`` (global-admin, gated at the route) is the escape hatch: it bypasses
    # the cap and resets the counter so a permanently-stranded recall — otherwise
    # blocked from recall *and* from account/server deletion (state != detached) —
    # can be re-armed and driven to detached.
    if settled_recalling and force:
        assignment.recall_retry_count = 0
    elif settled_recalling:
        if assignment.recall_retry_count >= MAX_RECALL_RETRIES:
            alerts.open_alert(
                db,
                tenant_id=tenant_id,
                server_id=assignment.server_id,
                account_id=assignment.account_id,
                kind="recall_failed",
                severity="warning",
                detail={
                    "reason": "recall retries exhausted",
                    "retries": assignment.recall_retry_count,
                    "last_error": assignment.last_error,
                },
            )
            db.commit()
            raise conflict(
                "assignment.recall_retries_exhausted",
                f"recall has failed {assignment.recall_retry_count} times "
                f"(cap {MAX_RECALL_RETRIES}); a recall_failed alert is open for "
                "operator intervention.",
            )
        assignment.recall_retry_count += 1
    else:
        assignment.recall_retry_count = 0
    command = enqueue(
        db,
        assignment=assignment,
        command_type="recall",
        payload={"purge_local_copy": recall_purges_local_copy(db, assignment)},
    )
    assignment.state = "recalling"
    assignment.pending_command_id = command.command_id
    assignment.last_error = None
    assignment.updated_at = _now()
    db.commit()
    db.refresh(assignment)
    return assignment


def request_activate(
    db: Session, tenant_id: uuid.UUID, assignment_id: uuid.UUID
) -> Assignment:
    assignment = inventory.get_assignment(db, tenant_id, assignment_id)
    if assignment.state != "inactive":
        raise conflict(
            "assignment.not_activatable",
            f"activate requires state 'inactive'; assignment is '{assignment.state}'.",
        )
    command = enqueue(
        db,
        assignment=assignment,
        command_type="activate",
        payload={"active": True},
    )
    assignment.pending_command_id = command.command_id
    assignment.last_error = None
    assignment.updated_at = _now()
    db.commit()
    db.refresh(assignment)
    return assignment


def request_deactivate(
    db: Session, tenant_id: uuid.UUID, assignment_id: uuid.UUID
) -> Assignment:
    assignment = inventory.get_assignment(db, tenant_id, assignment_id)
    if assignment.state != "active":
        raise conflict(
            "assignment.not_deactivatable",
            f"deactivate requires state 'active'; assignment is '{assignment.state}'.",
        )
    command = enqueue(
        db,
        assignment=assignment,
        command_type="deactivate",
        payload={"active": False},
    )
    assignment.pending_command_id = command.command_id
    assignment.last_error = None
    assignment.updated_at = _now()
    db.commit()
    db.refresh(assignment)
    return assignment


# -- P3 switching-control transitions -----------------------------------------
def request_recover(
    db: Session, tenant_id: uuid.UUID, assignment_id: uuid.UUID
) -> Assignment:
    """§5.2 recover: quarantined -> active via SetAccountActive(activate).

    Carries ``clear_quarantine`` so the agent lifts the tsamx quarantine as it
    re-activates; convergence lands the assignment back on ``active``.
    """
    assignment = inventory.get_assignment(db, tenant_id, assignment_id)
    if assignment.state != "quarantined":
        raise conflict(
            "assignment.not_recoverable",
            f"recover requires state 'quarantined'; assignment is '{assignment.state}'.",
        )
    command = enqueue(
        db,
        assignment=assignment,
        command_type="activate",
        payload={"active": True, "clear_quarantine": True},
    )
    assignment.pending_command_id = command.command_id
    assignment.last_error = None
    assignment.updated_at = _now()
    db.commit()
    db.refresh(assignment)
    return assignment


def request_switch_now(
    db: Session,
    tenant_id: uuid.UUID,
    assignment_id: uuid.UUID,
    *,
    strategy: str | None = None,
) -> Assignment:
    """Manual switch (§6.3). Non-state command: it moves no assignment state,
    only ``last_switched_at`` on the origin account. With ``strategy`` the agent
    lets tsamx rank candidates; without it, it switches to this assignment's
    account."""
    assignment = inventory.get_assignment(db, tenant_id, assignment_id)
    if assignment.state not in ("active", "inactive"):
        raise conflict(
            "assignment.not_switchable",
            f"switch-now requires an installed account (active/inactive); "
            f"state is '{assignment.state}'.",
        )
    payload: dict = {}
    if strategy is not None:
        payload["strategy"] = strategy
    enqueue(db, assignment=assignment, command_type="switch_now", payload=payload)
    account = db.scalar(
        select(Account).where(
            Account.id == assignment.account_id, Account.tenant_id == tenant_id
        )
    )
    if account is not None:
        account.last_switched_at = _now()
    assignment.updated_at = _now()
    db.commit()
    db.refresh(assignment)
    return assignment


def request_switch_mode(
    db: Session, tenant_id: uuid.UUID, server_id: uuid.UUID, *, mode: str
) -> Server:
    """Set a server's switch mode: persist ``servers.switch_mode`` and, so a
    connected agent applies it immediately, queue a SetSwitchMode. A restart
    recovers it from the column via session re-assertion."""
    server = inventory.get_server(db, tenant_id, server_id)
    server.switch_mode = mode
    server.updated_at = _now()
    enqueue_server(
        db,
        tenant_id=tenant_id,
        server_id=server_id,
        command_type="set_mode",
        payload={"mode": mode},
    )
    db.commit()
    db.refresh(server)
    return server


def request_refresh_usage(
    db: Session, tenant_id: uuid.UUID, server_id: uuid.UUID
) -> None:
    """Ask the agent for an immediate usage report (§6.3 req_report)."""
    inventory.get_server(db, tenant_id, server_id)
    enqueue_server(
        db,
        tenant_id=tenant_id,
        server_id=server_id,
        command_type="req_report",
        payload={"reason": "console refresh"},
    )
    db.commit()


def request_self_update(
    db: Session, tenant_id: uuid.UUID, server_id: uuid.UUID, *, expected_commit: str = ""
) -> None:
    """Ask the agent to rebuild itself from its own working tree and restart
    (§6.3 self_update).

    Server-scoped (``assignment_id`` NULL): it moves no assignment state. The
    payload carries at most a commit pin — never a repository, branch or build
    flag, so a compromised AMS cannot point an agent at foreign source (proto
    ``SelfUpdate``). ``expected_commit`` is optional; empty means "whatever the
    agent's configured upstream tip is".

    Only one may be in flight per server. A self update costs a full rebuild and a
    restart, so a double-clicked button — or a script fanning out across the fleet
    twice — would otherwise queue several: the agent honours the first, restarts,
    and comes back to find the rest waiting. They do not collapse into no-ops
    either, since each carries its own command_id and the applied-log gate is
    keyed on that."""
    inventory.get_server(db, tenant_id, server_id)
    in_flight = db.scalar(
        select(AgentCommand).where(
            AgentCommand.server_id == server_id,
            AgentCommand.tenant_id == tenant_id,
            AgentCommand.command_type == "self_update",
            AgentCommand.status.in_(("queued", "sent")),
        )
    )
    if in_flight is not None:
        raise conflict(
            "self_update_already_pending",
            f"a self update is already {in_flight.status} for this server "
            f"(command {in_flight.command_id}); wait for it to ack or fail.",
        )
    enqueue_server(
        db,
        tenant_id=tenant_id,
        server_id=server_id,
        command_type="self_update",
        payload={"expected_commit": expected_commit or ""},
    )
    db.commit()


def request_set_policy(
    db: Session, tenant_id: uuid.UUID, server_id: uuid.UUID
) -> None:
    """Re-deliver the server's stored O4-C policy to a connected agent (§O4-C).

    Snapshots the current columns into the command payload; NULL columns push no
    value (the agent keeps its local default)."""
    server = inventory.get_server(db, tenant_id, server_id)
    enqueue_server(
        db,
        tenant_id=tenant_id,
        server_id=server_id,
        command_type="set_policy",
        payload={
            "threshold_pct": server.threshold_pct,
            "default_strategy": server.default_strategy,
            "cooldown_seconds": server.cooldown_seconds,
            "hysteresis_pct": server.hysteresis_pct,
        },
    )
    db.commit()


# -- gRPC-side outbox helpers -------------------------------------------------
def fetch_queued(db: Session, server_id: uuid.UUID) -> list[AgentCommand]:
    """Queued commands for one online server, oldest first.

    F3 multi-instance: ``FOR UPDATE SKIP LOCKED`` row-locks each returned row so
    two AMS instances polling the same server (a split-brain reconnect, or plain
    horizontal scale) never hand the same queued command to both agent sessions.
    The lock is only meaningful while it is held: the caller (``_build_queued_commands``)
    claims each row with :func:`claim_sent` and commits in *this same transaction*,
    so a concurrent poller either skips the locked row or, once we commit, sees it
    as ``sent`` and no longer ``queued``. A row this poller skips (build returned
    None, or it was locked) is left ``queued`` and retried on the next tick.
    """
    return list(
        db.scalars(
            select(AgentCommand)
            .where(AgentCommand.server_id == server_id, AgentCommand.status == "queued")
            .order_by(AgentCommand.created_at, AgentCommand.id)
            .with_for_update(skip_locked=True)
        ).all()
    )


def claim_sent(command: AgentCommand) -> None:
    """Mark a fetched, still-locked queued command ``sent`` — no commit.

    Called on rows returned by :func:`fetch_queued` (which holds their ``FOR
    UPDATE SKIP LOCKED`` locks) so the claim lands in the same transaction as the
    fetch; the caller commits once, atomically releasing the locks with the rows
    now ``sent``. A push that then fails on the wire is not lost: the command_id
    is unchanged and the D2 sent-ack sweeper re-queues it (idempotent)."""
    if command.status == "queued":
        command.status = "sent"
        command.sent_at = _now()
        command.updated_at = _now()


# -- D2 sent-未ack recovery ----------------------------------------------------
def sweep_sent_timeouts(
    db: Session,
    *,
    timeout_seconds: float = SENT_ACK_TIMEOUT_SECONDS,
    max_attempts: int = MAX_SEND_ATTEMPTS,
    queued_timeout_seconds: float = QUEUED_TIMEOUT_SECONDS,
    queued_stale_seconds: float = QUEUED_STALE_SECONDS,
) -> tuple[list[str], list[str]]:
    """Re-queue or fail commands stuck in ``sent`` past the ack timeout.

    A command the poll loop pushed (``claim_sent``) but the agent never acked ages
    silently: ``fetch_queued`` only re-sends ``queued`` rows, so a ``sent`` row is
    never retried and its assignment stays delivering/recalling forever
    (recovery-architecture §2). This sweep, a sibling of the offline sweeper (no
    new timer), closes that gap:

    * ``send_attempts < max_attempts`` -> ``sent`` back to ``queued`` (``sent_at``
      cleared, ``send_attempts`` incremented). The command_id is unchanged, so the
      re-send is idempotent: the agent dedupes on it and re-acks CONVERGED.
    * otherwise -> ``failed``, and the assignment is reverted to a resting state so
      it can be re-issued (deliver -> pending; recall -> settled recalling for the
      D1 path; activate/deactivate -> clear the pending marker; server-scoped and
      switch_now -> marked only, healed by the next session's re-assertion).

    No contention with reconcile-on-report: reconcile acts only on resting-state
    assignments (active/inactive/quarantined/detached) while every command swept
    here belongs to an in-flight assignment (delivering/recalling) or is
    server-scoped, so the two never move the same row.

    Also fails ``queued`` rows past their D3 timeout (:func:`_fail_queued_rows`)
    in the *same* transaction, folding those failures into ``failed_ids`` — this
    is the only sweep the gRPC process's periodic sweeper loop registers, and
    the transaction-scoped advisory lock guarding both sweeps (grpc/server.py)
    releases the instant this function's single ``db.commit()`` below runs, so
    the queued sweep has to run before that commit, not in one of its own.

    The caller need not commit — this commits its own transaction. Returns
    ``(requeued_ids, failed_ids)``.
    """
    cutoff = _now() - timedelta(seconds=timeout_seconds)
    stuck = db.scalars(
        select(AgentCommand).where(
            AgentCommand.status == "sent",
            AgentCommand.sent_at.is_not(None),
            AgentCommand.sent_at < cutoff,
        )
    ).all()
    requeued: list[str] = []
    failed: list[str] = []
    for command in stuck:
        if command.send_attempts < max_attempts:
            command.status = "queued"
            command.sent_at = None
            command.send_attempts += 1
            command.updated_at = _now()
            requeued.append(command.command_id)
        else:
            command.status = "failed"
            command.detail = "sent_ack_timeout"
            command.updated_at = _now()
            assignment = _revert_assignment_on_send_failure(db, command)
            _open_send_failure_alert(db, command, assignment)
            failed.append(command.command_id)
    failed.extend(
        _fail_queued_rows(
            db,
            never_connected_seconds=queued_timeout_seconds,
            stale_seconds=queued_stale_seconds,
        )
    )
    db.commit()
    return requeued, failed


# -- D3 queued-never-sent recovery ---------------------------------------------
def _fail_queued_rows(
    db: Session, *, never_connected_seconds: float, stale_seconds: float
) -> list[str]:
    """Fail aged ``queued`` rows and revert their assignment — no commit.

    Two-tier cutoff, joined against the owning server's ``last_seen_at``:

    * ``last_seen_at`` NULL (agent never once connected) -> aged out past
      ``never_connected_seconds`` (short: nothing has ever been "in progress").
    * ``last_seen_at`` set (agent has connected before, may just be between
      sessions — restart, maintenance) -> aged out past ``stale_seconds``
      (long: an ordinary reconnect must win the race and deliver it first).

    A ``queued`` row this old was never claimed by any poll loop (a claimed row
    is ``sent``), so there is no re-queue step here unlike
    :func:`sweep_sent_timeouts` — the condition that stranded it (no agent
    polling this server) would just strand a re-queued row the same way. Fails
    straight to ``failed`` and reverts the assignment exactly like a send-retry
    exhaustion (:func:`_revert_assignment_on_send_failure`), so
    ``pending_command_id`` clears and the console stops showing "동기화중"
    forever. Returns the list of failed command ids; the caller commits.
    """
    never_connected_cutoff = _now() - timedelta(seconds=never_connected_seconds)
    stale_cutoff = _now() - timedelta(seconds=stale_seconds)
    stuck = db.scalars(
        select(AgentCommand)
        .join(Server, Server.id == AgentCommand.server_id)
        .where(
            AgentCommand.status == "queued",
            or_(
                and_(
                    Server.last_seen_at.is_(None),
                    AgentCommand.created_at < never_connected_cutoff,
                ),
                and_(
                    Server.last_seen_at.is_not(None),
                    AgentCommand.created_at < stale_cutoff,
                ),
            ),
        )
    ).all()
    failed: list[str] = []
    for command in stuck:
        command.status = "failed"
        command.detail = "queued_timeout"
        command.updated_at = _now()
        assignment = _revert_assignment_on_send_failure(
            db, command, reason="queued_timeout"
        )
        _open_send_failure_alert(db, command, assignment)
        failed.append(command.command_id)
    return failed


def sweep_queued_timeouts(
    db: Session,
    *,
    timeout_seconds: float = QUEUED_TIMEOUT_SECONDS,
    stale_seconds: float = QUEUED_STALE_SECONDS,
) -> list[str]:
    """Standalone entry point for :func:`_fail_queued_rows` — commits itself.

    Not called by the gRPC sweeper loop (which only registers
    :func:`sweep_sent_timeouts`, folding this logic in directly under the same
    transaction/advisory-lock); kept for direct/test use.
    """
    failed = _fail_queued_rows(
        db, never_connected_seconds=timeout_seconds, stale_seconds=stale_seconds
    )
    db.commit()
    return failed


def _open_send_failure_alert(
    db: Session, command: AgentCommand, assignment: Assignment | None
) -> None:
    """Open the operator alert for a command that exhausted its send retries.

    Only account-scoped commands whose revert actually applied
    (``assignment`` non-None — see :func:`_revert_assignment_on_send_failure`)
    alert; the failure leaves the intent unrecoverable without an operator
    (deliver -> pending, activate/deactivate -> marker dropped, recall -> settled
    recalling), so it must surface:

    * ``recall`` re-uses the D1 ``recall_failed`` kind, so a re-armed recall that
      fails on the wire and one that fails on a DIVERGED ack dedupe onto the SAME
      open alert (``{server_id}:recall_failed:{account_id}``).
    * every other account-scoped type opens ``command_send_failed`` per account
      (``{server_id}:command_send_failed:{account_id}``).

    **No alert for server-scoped commands** (set_mode/set_policy/req_report,
    assignment_id NULL): their intent is not lost — the next session's policy
    re-assertion re-applies it — so a silent final failure is by design. Alerting
    them would accumulate a manual alert on a self-healing condition, share one
    dedupe key across the three types, and double-open against ``server_offline``.
    A **superseded** command (a newer command already owns the assignment) also
    does not alert: the successor reports its own result.

    ``detail`` carries only command_type / account_id / last_error — never the
    payload, which may hold credential material.
    """
    if assignment is None:
        return
    account_id = assignment.account_id
    kind = "recall_failed" if command.command_type == "recall" else "command_send_failed"
    alerts.open_alert(
        db,
        tenant_id=command.tenant_id,
        server_id=command.server_id,
        account_id=account_id,
        kind=kind,
        severity="warning",
        detail={
            "reason": "send retries exhausted",
            "command_type": command.command_type,
            "account_id": str(account_id) if account_id is not None else None,
            "last_error": command.detail,
        },
    )


def _revert_assignment_on_send_failure(
    db: Session, command: AgentCommand, *, reason: str = "sent_ack_timeout"
) -> Assignment | None:
    """Revert the assignment of a permanently-failed ``sent`` or ``queued`` command.

    ``reason`` becomes ``assignment.last_error`` — the sent-ack sweeper passes
    the default; the queued-timeout sweeper (:func:`sweep_queued_timeouts`)
    passes ``"queued_timeout"`` so the operator-visible error names the actual
    failure instead of a misleading "sent" label.

    Mirrors the DIVERGED/REJECTED ack handling in ``reconcile.apply_ack``: only
    the assignment still pointing at this command (``pending_command_id`` match) is
    touched, so a newer command is never clobbered. deliver reverts to ``pending``;
    recall is left ``recalling`` with the pending marker cleared — the settled,
    non-in-flight state the D1 reconcile/REST recall path recovers; activate/
    deactivate keep their resting state and only drop the marker. Server-scoped
    commands (assignment_id NULL) revert nothing — marking the command ``failed``
    is enough, and a session re-assertion re-applies server-scoped policy on the
    next connect. ``switch_now`` is marker-less (it never set a pending marker and
    changes no assignment state), but it still falls through to record
    ``last_error`` on its assignment and return non-None, so a permanently-failed
    manual switch is surfaced as a send-failure alert.

    Returns the reverted assignment, or None when nothing was reverted (no
    assignment, or a newer command already owns it). The caller opens the
    send-failure alert only for a non-None return, so server-scoped and
    superseded commands never alert.
    """
    if command.assignment_id is None:
        return None
    assignment = db.scalar(
        select(Assignment).where(
            Assignment.id == command.assignment_id,
            Assignment.tenant_id == command.tenant_id,
        )
    )
    if assignment is None:
        return None
    if (
        command.command_type not in _MARKERLESS_COMMAND_TYPES
        and assignment.pending_command_id != command.command_id
    ):
        # A marker-setting command whose marker is now None (settled elsewhere,
        # e.g. _settle_recall_detached detached the account) or points at another
        # command (superseded) is already handled — do not clobber the assignment
        # and do not alert (return None so the caller opens none). A marker-less
        # type (switch_now) skips this and still alerts on final failure.
        return None
    assignment.last_error = reason
    assignment.pending_command_id = None
    if command.command_type == "deliver":
        assignment.state = "pending"
    # recall: leave state 'recalling' (pending marker now NULL) -> settled recalling
    #   the D1 path re-recalls or settles to detached.
    # activate/deactivate/recover: leave the resting state (inactive/active/
    #   quarantined); dropping the marker re-opens it to a fresh command.
    assignment.updated_at = _now()
    return assignment
