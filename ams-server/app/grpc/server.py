"""AMS gRPC control-plane server (design note §1, §5).

A standalone ``grpc.aio`` process (default port 50051), independent of the
FastAPI REST app; the only coupling is the database. It:

* accepts the AMA ``Session`` bidi stream, authenticates the first ``Register``
  (enroll_token promotion or server_credential re-auth, §2), and binds the
  session to the tenant recorded on the server row — never a client-supplied one;
* sends ``SessionSetup`` unconditionally right after Register (§3 rule 1),
  delivering a per-session KEK held only in the agent's memory (O1);
* polls the ``agent_commands`` outbox (0.5 s) for the connected server, signs
  each command with Ed25519 and pushes it down the stream, then reconciles the
  agent's ``CommandAck`` back onto the assignment (§5).

Secrets — the KEK, the signing key, decrypted credential plaintext — are never
logged (§7). ``ReportUsage`` is the unary report-only fallback.
"""

from __future__ import annotations

import asyncio
import logging
import os
import uuid
from datetime import UTC, datetime

import grpc
from google.protobuf.json_format import MessageToDict
from google.protobuf.timestamp_pb2 import Timestamp
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.core import crypto
from app.db import get_sessionmaker
from app.grpc import signing
from app.grpc.proto import pb, pb_grpc
from app.models import Account, AgentCommand, Assignment, Server, UsageSnapshot
from app.services import alerts, commands, reconcile

_logger = logging.getLogger("ams.grpc")

POLL_INTERVAL_SECONDS = float(os.environ.get("AMX_GRPC_POLL_INTERVAL", "0.5"))
DEFAULT_PORT = int(os.environ.get("AMX_GRPC_PORT", "50051"))
# Offline detection (design note §8). AMA heartbeats on this cadence; a server
# unseen for 3 beats is presumed offline even if its gRPC stream is half-open,
# so the sweeper forces it offline and alarms. The sweeper itself wakes once per
# heartbeat interval.
HEARTBEAT_INTERVAL_SECONDS = float(os.environ.get("AMX_HEARTBEAT_INTERVAL", "30"))
OFFLINE_AFTER_SECONDS = float(
    os.environ.get("AMX_OFFLINE_AFTER", str(3 * HEARTBEAT_INTERVAL_SECONDS))
)
SWEEP_INTERVAL_SECONDS = float(
    os.environ.get("AMX_OFFLINE_SWEEP_INTERVAL", str(HEARTBEAT_INTERVAL_SECONDS))
)

_CREDENTIAL_TYPE = {
    "oauth": pb.CREDENTIAL_TYPE_OAUTH,
    "api_key": pb.CREDENTIAL_TYPE_API_KEY,
}
_CONVERGENCE = {
    pb.CommandAck.CONVERGENCE_CONVERGED: reconcile.CONVERGED,
    pb.CommandAck.CONVERGENCE_PENDING: reconcile.PENDING,
    pb.CommandAck.CONVERGENCE_DIVERGED: reconcile.DIVERGED,
    pb.CommandAck.CONVERGENCE_REJECTED: reconcile.REJECTED,
}
_SWITCH_MODE = {
    "auto": pb.SWITCH_MODE_AUTO,
    "manual": pb.SWITCH_MODE_MANUAL,
}
_SWITCH_STRATEGY = {
    "best": pb.SwitchNow.SWITCH_STRATEGY_BEST,
    "next_available": pb.SwitchNow.SWITCH_STRATEGY_NEXT_AVAILABLE,
}
# proto AllocationStatus -> the actual-status strings reconcile compares against
# (keeps app.services.reconcile protobuf-free — the translation lives here).
_ALLOCATION_STATUS = {
    pb.ALLOCATION_STATUS_ACTIVE: reconcile.ACTUAL_ACTIVE,
    pb.ALLOCATION_STATUS_INACTIVE: reconcile.ACTUAL_INACTIVE,
    pb.ALLOCATION_STATUS_QUARANTINED: reconcile.ACTUAL_QUARANTINED,
    pb.ALLOCATION_STATUS_DELIVERING: "delivering",
    pb.ALLOCATION_STATUS_RECALLING: "recalling",
    pb.ALLOCATION_STATUS_ABSENT: reconcile.ACTUAL_ABSENT,
}


def _now() -> datetime:
    return datetime.now(UTC)


def _event_detail(payload: dict) -> dict:
    """Small, credential-free alert detail lifted from an AccountEvent (§7)."""
    return {
        "event_id": payload.get("event_id"),
        "kind": payload.get("kind"),
        "trigger": payload.get("trigger"),
        "pool_summary": payload.get("pool_summary"),
        "detail": payload.get("detail"),
    }


def _event_account_id(payload: dict) -> uuid.UUID | None:
    """The account an event is about: ``to`` if present, else ``from``.

    Quarantine leaves ``to`` unset (proto §6.5), so the quarantined account is
    carried in ``from``. A malformed id degrades to a server-scoped alert rather
    than raising inside the session thread."""
    for field in ("to", "from"):
        ref = payload.get(field)
        if isinstance(ref, dict):
            raw = ref.get("ams_account_id")
            if raw:
                try:
                    return uuid.UUID(str(raw))
                except (ValueError, TypeError):
                    return None
    return None


def _now_ts() -> Timestamp:
    ts = Timestamp()
    ts.FromDatetime(_now())
    return ts


def _set_policy_msg(threshold_pct, default_strategy) -> pb.SetPolicy:
    """Build a SetPolicy from stored columns. NULL/absent values push nothing —
    threshold_pct 0 and strategy UNSPECIFIED both mean "keep the tsamx-local
    default" per the proto (O4-C)."""
    policy = pb.SetPolicy()
    if threshold_pct:
        policy.threshold_pct = float(threshold_pct)
    if default_strategy:
        policy.default_strategy = _SWITCH_STRATEGY.get(
            default_strategy, pb.SwitchNow.SWITCH_STRATEGY_UNSPECIFIED
        )
    return policy


def sign_command(signer: signing.Signer, command: pb.AmsCommand) -> None:
    """Ed25519-sign a command over its serialization with ``signature`` cleared."""
    command.signature = b""
    command.signature = signer.sign(command.SerializeToString(deterministic=True))


def command_signature_valid(public_key, command: pb.AmsCommand) -> bool:
    """Verify a signed command (the agent's check; used by tests standing in)."""
    received = command.signature
    command.signature = b""
    payload = command.SerializeToString(deterministic=True)
    command.signature = received
    return signing.verify(public_key, received, payload)


class ControlPlaneServicer(pb_grpc.AmxControlPlaneServicer):
    def __init__(
        self,
        signer: signing.Signer,
        session_factory: sessionmaker[Session] | None = None,
        poll_interval: float = POLL_INTERVAL_SECONDS,
    ) -> None:
        self._signer = signer
        self._sm = session_factory or get_sessionmaker()
        self._poll_interval = poll_interval
        # server_id (str) -> agent_id, for a single-instance online registry (§5).
        self._online: dict[str, str] = {}

    # -- Session --------------------------------------------------------------
    async def Session(self, request_iterator, context):  # noqa: N802
        it = request_iterator.__aiter__()
        try:
            first = await it.__anext__()
        except StopAsyncIteration:
            return
        if first.WhichOneof("msg") != "register":
            await context.abort(
                grpc.StatusCode.FAILED_PRECONDITION,
                "the first message on a session must be Register",
            )
        reg = first.register

        auth = await asyncio.to_thread(self._authenticate, reg)
        if auth is None:
            await context.abort(grpc.StatusCode.UNAUTHENTICATED, "invalid credential")
        server_id, tenant_id, server_credential = auth
        agent_id = reg.agent_id or str(server_id)

        kek = signing.new_kek()
        key_id = signing.new_key_id()
        setup = self._build_session_setup(server_credential, kek, key_id, agent_id)
        write_lock = asyncio.Lock()
        async with write_lock:
            await context.write(setup)

        # Session authority re-assertion (design note decision 5): AMA's
        # switch_mode and policy are memory-only, so AMS unconditionally re-pushes
        # SessionSetup -> SetSwitchMode -> SetPolicy every session, exempt from the
        # applied-id gate. Without it a restarted agent falls to MANUAL and auto
        # switching stops. Signed and bound to agent_id, exactly like every other
        # command; NULL policy columns push no value (the agent keeps its local
        # default).
        for cmd in await asyncio.to_thread(self._build_reassertion, server_id, agent_id):
            async with write_lock:
                await context.write(cmd)

        # Cold-start rule 3: suppress redundant redelivery only for accounts the
        # agent both reports applied AND reports present. An empty (pre-KEK)
        # Register suppresses nothing (rule 2) and never deletes anything.
        reported = {a.account.ams_account_id for a in reg.accounts if a.account.ams_account_id}
        await asyncio.to_thread(
            self._suppress, tenant_id, server_id, list(reg.applied_command_ids), reported
        )

        self._online[str(server_id)] = agent_id
        poll_task = asyncio.create_task(
            self._poll_loop(context, write_lock, server_id, tenant_id, agent_id, kek, key_id)
        )
        try:
            while True:
                try:
                    msg = await it.__anext__()
                except StopAsyncIteration:
                    break
                await asyncio.to_thread(self._handle_upstream, msg, server_id, tenant_id)
        finally:
            poll_task.cancel()
            self._online.pop(str(server_id), None)
            await asyncio.to_thread(self._mark_offline, server_id)

    async def _poll_loop(
        self,
        context,
        write_lock: asyncio.Lock,
        server_id: uuid.UUID,
        tenant_id: uuid.UUID,
        agent_id: str,
        kek: bytes,
        key_id: str,
    ) -> None:
        while True:
            try:
                built = await asyncio.to_thread(
                    self._build_queued_commands, server_id, agent_id, kek, key_id
                )
                for command_id, cmd in built:
                    async with write_lock:
                        await context.write(cmd)
                    await asyncio.to_thread(self._mark_sent, command_id)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - a poll failure must not kill the session
                _logger.warning("command poll iteration failed", exc_info=False)
            await asyncio.sleep(self._poll_interval)

    # -- ReportUsage (unary fallback) ----------------------------------------
    async def ReportUsage(self, request: pb.ReportEnvelope, context):  # noqa: N802
        accepted = await asyncio.to_thread(self._store_report_envelope, request)
        if not accepted:
            await context.abort(grpc.StatusCode.UNAUTHENTICATED, "invalid credential")
        return pb.Ack(accepted=True, message="stored", received_at=_now_ts())

    # -- DB-bound helpers (run in a thread) ----------------------------------
    def _authenticate(self, reg: pb.Register):
        which = reg.WhichOneof("auth")
        with self._sm() as db:
            if which == "enroll_token":
                server = db.scalar(
                    select(Server).where(
                        Server.enroll_token_hash == crypto.hash_token(reg.enroll_token)
                    )
                )
                if server is None:
                    return None
                if (
                    server.enroll_token_expires_at is not None
                    and server.enroll_token_expires_at < _now()
                ):
                    return None
                # Promote: mint a long-lived credential, burn the one-shot token.
                credential = crypto.new_token()
                server.server_cred_hash = crypto.hash_token(credential)
                server.enroll_token_hash = None
                server.enroll_token_expires_at = None
                self._touch_server(server, reg)
                alerts.resolve(db, server_id=server.id, kind="server_offline")
                db.commit()
                return (server.id, server.tenant_id, credential)
            if which == "server_credential":
                server = db.scalar(
                    select(Server).where(
                        Server.server_cred_hash == crypto.hash_token(reg.server_credential)
                    )
                )
                if server is None:
                    return None
                self._touch_server(server, reg)
                alerts.resolve(db, server_id=server.id, kind="server_offline")
                db.commit()
                return (server.id, server.tenant_id, "")
            return None

    @staticmethod
    def _touch_server(server: Server, reg: pb.Register) -> None:
        if reg.agent_id:
            server.agent_id = reg.agent_id
        if reg.agent_version:
            server.agent_version = reg.agent_version
        if reg.tsamx_version:
            server.tsamx_version = reg.tsamx_version
        server.status = "online"
        server.last_seen_at = _now()
        server.updated_at = _now()

    def _suppress(
        self,
        tenant_id: uuid.UUID,
        server_id: uuid.UUID,
        applied_command_ids: list[str],
        reported_account_ids: set[str],
    ) -> None:
        with self._sm() as db:
            reconcile.suppress_applied(
                db,
                tenant_id=tenant_id,
                server_id=server_id,
                applied_command_ids=applied_command_ids,
                reported_account_ids=reported_account_ids,
            )

    def _build_queued_commands(
        self, server_id: uuid.UUID, agent_id: str, kek: bytes, key_id: str
    ) -> list[tuple[str, pb.AmsCommand]]:
        out: list[tuple[str, pb.AmsCommand]] = []
        with self._sm() as db:
            for row in commands.fetch_queued(db, server_id):
                cmd = self._build_command(db, row, agent_id, kek, key_id)
                if cmd is not None:
                    out.append((row.command_id, cmd))
        return out

    def _build_command(
        self, db: Session, row: AgentCommand, agent_id: str, kek: bytes, key_id: str
    ) -> pb.AmsCommand | None:
        # Bind the command to the authenticated recipient. target_agent_id is
        # inside the signed payload, so the agent rejects any command not minted
        # for it — a captured command cannot be re-injected into another agent.
        cmd = pb.AmsCommand(
            command_id=row.command_id, issued_at=_now_ts(), target_agent_id=agent_id
        )
        ctype = row.command_type

        # Server-scoped (assignment_id NULL) session-control commands.
        if ctype == "set_policy":
            cmd.set_policy.CopyFrom(
                _set_policy_msg(
                    row.payload.get("threshold_pct"), row.payload.get("default_strategy")
                )
            )
            sign_command(self._signer, cmd)
            return cmd
        if ctype == "set_mode":
            mode = _SWITCH_MODE.get(row.payload.get("mode"), pb.SWITCH_MODE_UNSPECIFIED)
            cmd.set_mode.CopyFrom(pb.SetSwitchMode(mode=mode))
            sign_command(self._signer, cmd)
            return cmd
        if ctype == "req_report":
            cmd.req_report.CopyFrom(
                pb.RequestReport(
                    report_type=pb.RequestReport.REPORT_TYPE_USAGE,
                    reason=row.payload.get("reason", ""),
                )
            )
            sign_command(self._signer, cmd)
            return cmd

        # Account/assignment-scoped commands from here on.
        assignment = db.scalar(
            select(Assignment).where(
                Assignment.id == row.assignment_id, Assignment.tenant_id == row.tenant_id
            )
        )
        if assignment is None:
            return None
        account = db.scalar(
            select(Account).where(
                Account.id == assignment.account_id, Account.tenant_id == row.tenant_id
            )
        )
        if account is None:
            return None
        account_ref = pb.AccountRef(
            ams_account_id=str(account.id),
            email=account.email,
            account_uuid=account.account_uuid or "",
        )
        if ctype == "deliver":
            cmd.deliver.CopyFrom(
                self._build_deliver(account, assignment, account_ref, row, agent_id, kek, key_id)
            )
        elif ctype == "recall":
            cmd.recall.CopyFrom(
                pb.RecallAccount(
                    assignment_id=str(assignment.id),
                    account=account_ref,
                    purge_local_copy=bool(row.payload.get("purge_local_copy", False)),
                )
            )
        elif ctype in ("activate", "deactivate"):
            cmd.set_active.CopyFrom(
                pb.SetAccountActive(
                    assignment_id=str(assignment.id),
                    account=account_ref,
                    active=bool(row.payload.get("active", ctype == "activate")),
                    clear_quarantine=bool(row.payload.get("clear_quarantine", False)),
                )
            )
        elif ctype == "switch_now":
            switch = pb.SwitchNow(assignment_id=str(assignment.id))
            strategy = row.payload.get("strategy")
            if strategy:
                switch.strategy = _SWITCH_STRATEGY.get(
                    strategy, pb.SwitchNow.SWITCH_STRATEGY_UNSPECIFIED
                )
            else:
                switch.account.CopyFrom(account_ref)
            cmd.switch_now.CopyFrom(switch)
        else:
            return None
        sign_command(self._signer, cmd)
        return cmd

    def _build_reassertion(
        self, server_id: uuid.UUID, agent_id: str
    ) -> list[pb.AmsCommand]:
        """SetSwitchMode + SetPolicy re-asserted from the server row (decision 5).

        Not routed through the outbox: re-assertion is idempotent and must not be
        suppressed by the applied-id gate, so these carry fresh command_ids and
        are built inline every session.
        """
        with self._sm() as db:
            server = db.get(Server, server_id)
            if server is None:
                return []
            mode = _SWITCH_MODE.get(server.switch_mode, pb.SWITCH_MODE_UNSPECIFIED)
            threshold_pct = server.threshold_pct
            default_strategy = server.default_strategy
        out: list[pb.AmsCommand] = []
        set_mode = pb.AmsCommand(
            command_id="reassert_mode_" + uuid.uuid4().hex,
            issued_at=_now_ts(),
            target_agent_id=agent_id,
        )
        set_mode.set_mode.CopyFrom(pb.SetSwitchMode(mode=mode))
        sign_command(self._signer, set_mode)
        out.append(set_mode)

        set_policy = pb.AmsCommand(
            command_id="reassert_policy_" + uuid.uuid4().hex,
            issued_at=_now_ts(),
            target_agent_id=agent_id,
        )
        set_policy.set_policy.CopyFrom(_set_policy_msg(threshold_pct, default_strategy))
        sign_command(self._signer, set_policy)
        out.append(set_policy)
        return out

    def _build_deliver(
        self,
        account: Account,
        assignment: Assignment,
        account_ref: pb.AccountRef,
        row: AgentCommand,
        agent_id: str,
        kek: bytes,
        key_id: str,
    ) -> pb.DeliverAccount:
        # Open the at-rest Fernet envelope, immediately re-seal under the session
        # KEK bound to (account, agent). Plaintext exists only between these two
        # calls and is never logged.
        plaintext = crypto.decrypt_secret(account.encrypted_secret or "")
        ciphertext, nonce = signing.seal_credential(
            kek, plaintext.encode(), ams_account_id=str(account.id), agent_id=agent_id
        )
        del plaintext
        encrypted = pb.EncryptedCredential(
            algorithm=pb.ENCRYPTION_ALGORITHM_AES_256_GCM,
            ciphertext=ciphertext,
            nonce=nonce,
            key_id=key_id,
            aad_ams_account_id=str(account.id),
            aad_agent_id=agent_id,
        )
        desired = row.payload.get("desired_status", "active")
        deliver = pb.DeliverAccount(
            assignment_id=str(assignment.id),
            account=account_ref,
            credential_type=_CREDENTIAL_TYPE.get(
                account.credential_type, pb.CREDENTIAL_TYPE_UNSPECIFIED
            ),
            encrypted_credential=encrypted,
            desired_status=(
                pb.ALLOCATION_STATUS_INACTIVE
                if desired == "inactive"
                else pb.ALLOCATION_STATUS_ACTIVE
            ),
            organization_name=account.organization_name or "",
        )
        if account.credential_expires_at is not None:
            deliver.credential_expires_at.FromDatetime(account.credential_expires_at)
        return deliver

    def _build_session_setup(
        self, server_credential: str, kek: bytes, key_id: str, agent_id: str
    ) -> pb.AmsCommand:
        setup = pb.SessionSetup(
            server_credential=server_credential or "",
            keys=[
                pb.SessionSetup.WrappedKey(
                    key_id=key_id,
                    wrapped_key=kek,  # transit confidentiality is TLS (D9); memory-only on the agent
                    algorithm=pb.ENCRYPTION_ALGORITHM_AES_256_GCM,
                )
            ],
            active_key_id=key_id,
        )
        cmd = pb.AmsCommand(
            command_id="setup_" + uuid.uuid4().hex,
            issued_at=_now_ts(),
            target_agent_id=agent_id,
        )
        cmd.session_setup.CopyFrom(setup)
        sign_command(self._signer, cmd)
        return cmd

    def _mark_sent(self, command_id: str) -> None:
        with self._sm() as db:
            commands.mark_sent(db, command_id)

    def _handle_upstream(
        self, msg: pb.AmaMessage, server_id: uuid.UUID, tenant_id: uuid.UUID
    ) -> None:
        kind = msg.WhichOneof("msg")
        if kind == "hb":
            self._touch_last_seen(server_id)
        elif kind == "ack":
            ack = msg.ack
            reconcile_convergence = _CONVERGENCE.get(ack.convergence, reconcile.PENDING)
            with self._sm() as db:
                reconcile.apply_ack(
                    db,
                    tenant_id=tenant_id,
                    command_id=ack.command_id,
                    convergence=reconcile_convergence,
                    detail=ack.detail,
                    error_code=ack.error_code,
                )
        elif kind == "usage":
            self._store_usage(server_id, tenant_id, msg.usage, report_type="usage")
        elif kind == "event":
            self._store_event(server_id, tenant_id, msg.event)

    def _touch_last_seen(self, server_id: uuid.UUID) -> None:
        with self._sm() as db:
            server = db.get(Server, server_id)
            if server is not None:
                server.last_seen_at = _now()
                server.status = "online"
                server.updated_at = _now()
                # A live heartbeat clears any standing offline alert (auto-resolve
                # on reconnect / recovery from a sweeper false-positive).
                alerts.resolve(db, server_id=server_id, kind="server_offline")
                db.commit()

    def _mark_offline(self, server_id: uuid.UUID) -> None:
        with self._sm() as db:
            server = db.get(Server, server_id)
            if server is not None:
                server.status = "offline"
                server.updated_at = _now()
                alerts.open_alert(
                    db,
                    tenant_id=server.tenant_id,
                    server_id=server_id,
                    kind="server_offline",
                    severity="warning",
                    detail={"reason": "session closed"},
                )
                db.commit()

    def _persist_usage_report(
        self,
        db: Session,
        server: Server,
        report: pb.UsageReport,
        *,
        report_type: str,
    ) -> None:
        """Persist one usage report: snapshot + reconcile + alerts + liveness.

        Shared by the streaming usage path and the unary ``ReportUsage``
        fallback so both enforce the same drift correction, all_exhausted/drift
        alert sync, and offline-resolve — in a single transaction the caller
        commits (design note §4). A fallback-only server has no stream
        heartbeat, so this is its only path to those effects.
        """
        snapshot = UsageSnapshot(
            tenant_id=server.tenant_id,
            server_id=server.id,
            account_id=None,
            report_type=report_type,
            payload=MessageToDict(report, preserving_proto_field_name=True),
        )
        db.add(snapshot)
        db.flush()  # assign snapshot.id before reconcile marks drift on it
        # reconcile-on-report (design note decision 3): the report is the
        # actual authority. Translate proto AllocationStatus to the strings
        # reconcile compares, then let it detect drift + narrow corrections.
        reported = {
            a.account.ams_account_id: _ALLOCATION_STATUS.get(
                a.allocation_status, reconcile.ACTUAL_ABSENT
            )
            for a in report.accounts
            if a.account.ams_account_id
        }
        drift_entries = reconcile.reconcile_from_report(
            db,
            tenant_id=server.tenant_id,
            server_id=server.id,
            reported=reported,
            snapshot=snapshot,
        )
        # Alerts are reconciled from the SAME report in the SAME transaction
        # (design note §4): all_exhausted and drift open/refresh, and their
        # absence auto-resolves the matching open alert. Because it is driven
        # by usage_snapshots.drift + pool_summary rather than the best-effort
        # event stream, a lost switch_event still self-heals here (§8).
        alerts.sync_from_report(
            db,
            tenant_id=server.tenant_id,
            server_id=server.id,
            all_exhausted=bool(report.pool_summary.all_exhausted),
            drift_entries=drift_entries,
            source_snapshot_id=snapshot.id,
        )
        # A usage report proves liveness, so refresh last_seen and clear any
        # standing offline alert (mirrors _touch_last_seen on heartbeat). This
        # is what lets a fallback-only server auto-resolve its offline alert.
        server.last_seen_at = _now()
        server.status = "online"
        server.updated_at = _now()
        alerts.resolve(db, server_id=server.id, kind="server_offline")

    def _store_usage(
        self,
        server_id: uuid.UUID,
        tenant_id: uuid.UUID,
        report: pb.UsageReport,
        *,
        report_type: str,
    ) -> None:
        with self._sm() as db:
            server = db.get(Server, server_id)
            if server is None:
                return
            self._persist_usage_report(db, server, report, report_type=report_type)
            db.commit()

    def _store_event(
        self, server_id: uuid.UUID, tenant_id: uuid.UUID, event: pb.AccountEvent
    ) -> None:
        payload = MessageToDict(event, preserving_proto_field_name=True)
        with self._sm() as db:
            snapshot = UsageSnapshot(
                tenant_id=tenant_id,
                server_id=server_id,
                account_id=None,
                report_type="switch_event",
                payload=payload,
            )
            db.add(snapshot)
            db.flush()  # assign snapshot.id so an alert can cite it
            # Promote the P3-carried all_exhausted hook to a real alert, and open
            # a quarantine alert, in the snapshot's transaction (design note §4).
            if event.kind == pb.AccountEvent.KIND_ALL_EXHAUSTED:
                alerts.open_alert(
                    db,
                    tenant_id=tenant_id,
                    server_id=server_id,
                    kind="all_exhausted",
                    severity="critical",
                    detail=_event_detail(payload),
                    source_snapshot_id=snapshot.id,
                )
            elif event.kind == pb.AccountEvent.KIND_QUARANTINE:
                alerts.open_alert(
                    db,
                    tenant_id=tenant_id,
                    server_id=server_id,
                    account_id=_event_account_id(payload),
                    kind="quarantine",
                    severity="warning",
                    detail=_event_detail(payload),
                    source_snapshot_id=snapshot.id,
                )
            db.commit()

    def _store_report_envelope(self, envelope: pb.ReportEnvelope) -> bool:
        if not envelope.server_credential:
            return False
        with self._sm() as db:
            server = db.scalar(
                select(Server).where(
                    Server.server_cred_hash == crypto.hash_token(envelope.server_credential)
                )
            )
            if server is None:
                return False
            # Same helper as the streaming path: the unary fallback must also
            # reconcile drift and fire/resolve all_exhausted + offline alerts,
            # not merely store a snapshot (design note §4).
            self._persist_usage_report(db, server, envelope.report, report_type="usage")
            db.commit()
        return True


def create_server(
    signer: signing.Signer | None = None,
    session_factory: sessionmaker[Session] | None = None,
    poll_interval: float = POLL_INTERVAL_SECONDS,
) -> tuple[grpc.aio.Server, ControlPlaneServicer]:
    """Build (but do not start) an aio server with the control-plane servicer."""
    servicer = ControlPlaneServicer(
        signer or signing.Signer.from_env_or_generate(),
        session_factory=session_factory,
        poll_interval=poll_interval,
    )
    server = grpc.aio.server()
    pb_grpc.add_AmxControlPlaneServicer_to_server(servicer, server)
    return server, servicer


def configure_port(server, port: int) -> str:
    """Bind ``port`` on ``server``, choosing TLS from the environment (§7).

    * ``AMX_GRPC_TLS_CERT`` + ``AMX_GRPC_TLS_KEY`` (PEM paths) -> ``add_secure_port``.
      ``AMX_GRPC_TLS_CA`` additionally enables mutual TLS (client-cert required).
    * otherwise the server refuses to start unless ``AMX_GRPC_ALLOW_INSECURE=1``
      is set as an explicit opt-in, and logs a plaintext-exposure warning. This
      closes the ADVERSARY finding where a plaintext port leaked the KEK.

    Returns ``"tls"`` or ``"insecure"``.
    """
    cert = os.environ.get("AMX_GRPC_TLS_CERT")
    key = os.environ.get("AMX_GRPC_TLS_KEY")
    if cert and key:
        with open(key, "rb") as fh:
            key_bytes = fh.read()
        with open(cert, "rb") as fh:
            cert_bytes = fh.read()
        ca_path = os.environ.get("AMX_GRPC_TLS_CA")
        if ca_path:
            with open(ca_path, "rb") as fh:
                ca_bytes = fh.read()
            creds = grpc.ssl_server_credentials(
                [(key_bytes, cert_bytes)],
                root_certificates=ca_bytes,
                require_client_auth=True,
            )
        else:
            creds = grpc.ssl_server_credentials([(key_bytes, cert_bytes)])
        server.add_secure_port(f"[::]:{port}", creds)
        return "tls"
    if os.environ.get("AMX_GRPC_ALLOW_INSECURE") != "1":
        raise RuntimeError(
            "refusing to start without TLS: set AMX_GRPC_TLS_CERT and "
            "AMX_GRPC_TLS_KEY, or explicitly opt in with AMX_GRPC_ALLOW_INSECURE=1"
        )
    _logger.warning(
        "AMS gRPC starting WITHOUT TLS (AMX_GRPC_ALLOW_INSECURE=1) — the KEK is "
        "exposed to anyone who can read the wire; do not use in production (§7)"
    )
    server.add_insecure_port(f"[::]:{port}")
    return "insecure"


async def _offline_sweeper(
    session_factory: sessionmaker[Session],
    *,
    interval: float = SWEEP_INTERVAL_SECONDS,
    stale_after: float = OFFLINE_AFTER_SECONDS,
) -> None:
    """Periodically force stale-heartbeat servers offline (design note §8).

    Runs in the gRPC process — no separate scheduler. A sweep failure is logged
    and the loop continues, exactly like the command poll loop."""
    while True:
        await asyncio.sleep(interval)
        try:
            swept = await asyncio.to_thread(
                _sweep_once, session_factory, stale_after
            )
            if swept:
                _logger.info("offline sweeper marked %d server(s) offline", len(swept))
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - a sweep failure must not kill the process
            _logger.warning("offline sweep iteration failed", exc_info=False)


def _sweep_once(
    session_factory: sessionmaker[Session], stale_after: float
) -> list[uuid.UUID]:
    with session_factory() as db:
        return alerts.sweep_offline(db, stale_after_seconds=stale_after)


async def serve(port: int = DEFAULT_PORT) -> None:
    from app.config import get_settings

    get_settings()  # fail fast on missing configuration (§7)
    signer = signing.Signer.from_env_or_generate()
    server, servicer = create_server(signer)
    mode = configure_port(server, port)
    await server.start()
    _logger.info("AMS gRPC control plane listening on :%s (%s)", port, mode)
    sweeper = asyncio.create_task(_offline_sweeper(servicer._sm))
    try:
        await server.wait_for_termination()
    finally:
        sweeper.cancel()


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    asyncio.run(serve())


if __name__ == "__main__":
    main()
