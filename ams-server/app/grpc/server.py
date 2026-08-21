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
import json
import logging
import os
import unicodedata
import uuid
from datetime import UTC, datetime, timedelta

import grpc
from google.protobuf.json_format import MessageToDict
from google.protobuf.timestamp_pb2 import Timestamp
from sqlalchemy import or_, select, update
from sqlalchemy.orm import Session, sessionmaker

from app.core import crypto
from app.core.kek import KekError
from app.db import get_sessionmaker, try_advisory_xact_lock as _try_advisory_xact_lock
from app.grpc import signing
from app.grpc.proto import pb, pb_grpc
from app.models import Account, AgentCommand, Assignment, Server, UsageSnapshot
from app.services import (
    alert_webhook,
    alerts,
    audit,
    billing,
    commands,
    inventory,
    langfuse_alerts,
    langfuse_metrics,
    pool,
    reconcile,
    session_usage,
    usage_cost,
)

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


# Upper bound on how far ahead of AMS wall-clock a re-sync's observed_at may sit.
# observed_at is the monotonicity authority; without a ceiling a single push with a
# far-future stamp would pin the account and reject every later honest rotation
# (credential lock-in). Small enough to bound the attack, large enough to absorb
# ordinary agent/AMS clock skew.
_OBSERVED_AT_MAX_SKEW = timedelta(minutes=5)


def _is_blank(text: str) -> bool:
    """Whether ``text`` carries no credential information: every character is
    whitespace or a control character (Unicode category Cc).

    Deliberately NOT ``str.strip()``: it counts U+001C–U+001F as whitespace while
    Go's ``unicode.IsSpace`` does not, so a token of only those bytes would pass
    the agent-side check, advance the AMA baseline, and then be refused here —
    leaving AMS on the stale copy with no retry. The definition (space OR Cc) is a
    parity contract with ama-agent's ``isBlankCredential``; change neither side
    alone.
    """
    return all(ch.isspace() or unicodedata.category(ch) == "Cc" for ch in text)


def _token_material(obj: dict, *keys: str) -> tuple[bool, bool]:
    """``(any key present, any present key carrying material)`` for ``keys``.

    ``null`` reads as blank (present, no material). A value that is neither a
    string nor ``null`` is a shape this cannot judge, so it counts as material
    (the caller's bias is toward keeping the credential). Mirrors ama-agent's
    ``tokenMaterial``.
    """
    present = False
    material = False
    for key in keys:
        if key not in obj:
            continue
        present = True
        value = obj[key]
        if value is None:
            continue
        if not isinstance(value, str):
            material = True  # unexpected type: unjudgeable -> treat as material
        elif not _is_blank(value):
            material = True
    return present, material


def _credential_has_material(secret: str, provider: str) -> bool:
    """Whether a re-synced credential set still carries token material.

    Answers one question only: is this a logged-out shell — a set whose token
    block carries the token keys but nothing in them? A re-sync is applied silently
    and overwrites the at-rest copy, so the failure this guards is an agent pushing
    an emptied credential file over the live one AMS holds (the agent-side length
    check only sees a truncated file;
    ``{"claudeAiOauth":{"accessToken":"","refreshToken":""}}`` is non-empty bytes).

    Deliberately conservative — False ONLY for a definitely token-less set. A body
    that does not parse (an ``api_key`` credential is an opaque string), a
    non-object top level, a missing or non-object token block, a token block
    holding NEITHER token key (an unknown schema inside the block is as
    unjudgeable as one outside it), an unknown provider: all return True. The one
    non-JSON body that IS refused is a blank one, which no opaque ``api_key``
    could be.

    Note this does NOT mirror enroll, which requires a refresh_token: enroll can
    answer a 400 to the operator, whereas a rejected re-sync is silent, and a
    ``claude setup token`` account legitimately carries only a long-lived
    accessToken.

    ``json.loads`` is the risk here: a deeply nested body raises RecursionError and
    a huge one can raise MemoryError, and this runs on the session read loop, so
    every parse failure degrades to "cannot judge" rather than escaping.

    Judgement parity with ama-agent's ``HasCredentialMaterial`` is a contract; the
    shared case table lives in tests/test_credential_resync.py.
    """
    if _is_blank(secret):
        return False  # whitespace/control characters only: not even an api_key
    try:
        root = json.loads(secret)
    except (ValueError, TypeError, RecursionError, MemoryError):
        return True
    if not isinstance(root, dict):
        return True
    if provider == "codex":
        if "tokens" not in root:
            return True
        tokens = root["tokens"]
        if not isinstance(tokens, dict):
            return True
        present, material = _token_material(tokens, "refresh_token", "access_token")
        if not present:
            return True  # neither token key present: unknown schema inside tokens
        if material:
            return True
        # An emptied tokens block is still usable in the api-key form.
        return _token_material(root, "OPENAI_API_KEY")[1]
    if provider == "claude":
        if "claudeAiOauth" not in root:
            return True
        oauth = root["claudeAiOauth"]
        if not isinstance(oauth, dict):
            return True
        present, material = _token_material(oauth, "accessToken", "refreshToken")
        if not present:
            return True  # neither token key present: unknown schema in the block
        return material
    return True  # unknown provider: no schema to judge against


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


def _set_policy_msg(
    threshold_pct, default_strategy, cooldown_seconds=None, hysteresis_pct=None
) -> pb.SetPolicy:
    """Build a SetPolicy from stored columns.

    threshold_pct 0 and strategy UNSPECIFIED both mean "keep the tsamx-local
    default" per the proto (O4-C). cooldown_seconds/hysteresis_pct use the F4
    (O4-B) convention instead: a stored 0 is a real value, so NULL is delivered
    as the negative "unset" sentinel — and must be set explicitly, since the
    proto's own 0.0 default would otherwise read as a real value on the agent."""
    policy = pb.SetPolicy()
    if threshold_pct:
        policy.threshold_pct = float(threshold_pct)
    if default_strategy:
        policy.default_strategy = _SWITCH_STRATEGY.get(
            default_strategy, pb.SwitchNow.SWITCH_STRATEGY_UNSPECIFIED
        )
    policy.cooldown_seconds = float(cooldown_seconds) if cooldown_seconds is not None else -1.0
    policy.hysteresis_pct = float(hysteresis_pct) if hysteresis_pct is not None else -1.0
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
        # C2 per-agent KEK wrapping (proto §6.2, §7). Seal the session KEK to the
        # agent's ephemeral X25519 public key (NaCl sealed box) so it is never
        # cleartext even where TLS terminates ahead of AMS. A capable agent (one
        # that sent a public key) is always sealed — never downgraded — while a
        # keyless agent is refused unless AMX_ALLOW_RAW_KEK is set (dev fallback).
        allow_raw = os.environ.get("AMX_ALLOW_RAW_KEK") == "1"
        try:
            wrapped_key = signing.wrap_kek(
                kek, reg.agent_public_key, allow_raw=allow_raw
            )
        except signing.InvalidAgentPublicKey:
            # Public key is malformed/unusable. Refuse before any KEK leaves AMS;
            # the KEK itself is never mentioned (§7).
            await context.abort(
                grpc.StatusCode.INVALID_ARGUMENT, "invalid agent_public_key"
            )
        except signing.RawKekNotAllowed:
            # Keyless agent and no dev opt-in: the session cannot receive a KEK it
            # can protect, so it is refused rather than handed a raw KEK.
            await context.abort(
                grpc.StatusCode.FAILED_PRECONDITION, "agent_public_key required"
            )
        if not reg.agent_public_key:
            _logger.warning(
                "session KEK delivered RAW (AMX_ALLOW_RAW_KEK=1, no agent_public_key) "
                "for server %s — dev only, not for production (§7)",
                server_id,
            )
        setup = self._build_session_setup(server_credential, wrapped_key, key_id, agent_id)
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
                await asyncio.to_thread(
                    self._handle_upstream,
                    msg,
                    server_id,
                    tenant_id,
                    agent_id,
                    kek,
                    key_id,
                )
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
                # F3: fetch + claim (mark 'sent') happen in one transaction inside
                # _build_queued_commands, under FOR UPDATE SKIP LOCKED, so a second
                # AMS instance never re-sends the same row. The wire write follows
                # the claim; a write that fails is recovered by the D2 sent-ack
                # sweeper (idempotent command_id), never re-sent by another instance.
                built = await asyncio.to_thread(
                    self._build_queued_commands, server_id, agent_id, kek, key_id
                )
                for cmd in built:
                    async with write_lock:
                        await context.write(cmd)
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
    ) -> list[pb.AmsCommand]:
        out: list[pb.AmsCommand] = []
        with self._sm() as db:
            # fetch_queued row-locks each row (FOR UPDATE SKIP LOCKED); claim_sent
            # marks it 'sent' in this same transaction, and the single commit below
            # releases the locks with the rows already claimed — an atomic hand-off
            # no concurrent instance can duplicate. Rows we do not claim (build
            # returned None) stay 'queued' and are retried next tick.
            for row in commands.fetch_queued(db, server_id):
                cmd = self._build_command(db, row, agent_id, kek, key_id)
                if cmd is not None:
                    commands.claim_sent(row)
                    out.append(cmd)
            db.commit()
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
                    row.payload.get("threshold_pct"),
                    row.payload.get("default_strategy"),
                    row.payload.get("cooldown_seconds"),
                    row.payload.get("hysteresis_pct"),
                )
            )
            sign_command(self._signer, cmd)
            return cmd
        if ctype == "set_mode":
            mode = _SWITCH_MODE.get(row.payload.get("mode"), pb.SWITCH_MODE_UNSPECIFIED)
            cmd.set_mode.CopyFrom(pb.SetSwitchMode(mode=mode))
            sign_command(self._signer, cmd)
            return cmd
        if ctype == "self_update":
            # Only the optional commit pin crosses the wire. There is deliberately
            # no source field to fill in (proto SelfUpdate): the agent updates from
            # the clone the operator configured on it, never from anything AMS
            # names. An agent built before this command existed sees an unknown
            # oneof, so GetCmd() is nil and it nacks REJECTED/unknown_command
            # rather than misinterpreting the bytes.
            cmd.self_update.CopyFrom(
                pb.SelfUpdate(expected_commit=row.payload.get("expected_commit") or "")
            )
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
            provider=account.provider or "",
        )
        if ctype == "deliver":
            cmd.deliver.CopyFrom(
                self._build_deliver(
                    db, account, assignment, account_ref, row, agent_id, kek, key_id
                )
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
            cooldown_seconds = server.cooldown_seconds
            hysteresis_pct = server.hysteresis_pct
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
        set_policy.set_policy.CopyFrom(
            _set_policy_msg(
                threshold_pct, default_strategy, cooldown_seconds, hysteresis_pct
            )
        )
        sign_command(self._signer, set_policy)
        out.append(set_policy)
        return out

    def _build_deliver(
        self,
        db: Session,
        account: Account,
        assignment: Assignment,
        account_ref: pb.AccountRef,
        row: AgentCommand,
        agent_id: str,
        kek: bytes,
        key_id: str,
    ) -> pb.DeliverAccount:
        # Open the at-rest envelope (tenant DEK v2 or legacy Fernet, auto-detected
        # by the stored tag), immediately re-seal under the session KEK bound to
        # (account, agent). Read-only w.r.t. at-rest storage — no re-encrypt here,
        # so it never contends with O9 monotonicity. Plaintext exists only between
        # these two calls and is never logged.
        plaintext = crypto.decrypt_secret(
            account.encrypted_secret or "", tenant_id=account.tenant_id, db=db
        )
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
        self, server_credential: str, wrapped_key: bytes, key_id: str, agent_id: str
    ) -> pb.AmsCommand:
        setup = pb.SessionSetup(
            server_credential=server_credential or "",
            keys=[
                pb.SessionSetup.WrappedKey(
                    key_id=key_id,
                    # Sealed to the agent's X25519 public key (C2, §6.2); the raw
                    # KEK only when AMX_ALLOW_RAW_KEK is set. Memory-only on the agent.
                    wrapped_key=wrapped_key,
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

    def _handle_upstream(
        self,
        msg: pb.AmaMessage,
        server_id: uuid.UUID,
        tenant_id: uuid.UUID,
        agent_id: str,
        kek: bytes,
        key_id: str,
    ) -> None:
        kind = msg.WhichOneof("msg")
        if kind == "hb":
            self._touch_last_seen(server_id, msg.hb)
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
        elif kind == "cred_update":
            self._apply_cred_update(
                msg.cred_update, server_id, tenant_id, agent_id, kek, key_id
            )

    def _touch_last_seen(self, server_id: uuid.UUID, hb: pb.Heartbeat | None = None) -> None:
        with self._sm() as db:
            server = db.get(Server, server_id)
            if server is not None:
                now = _now()
                server.last_seen_at = now
                server.status = "online"
                server.updated_at = now
                # Host metrics are optional on the beat (proto §8). Overwrite the
                # columns only when the sample is actually present — an old agent,
                # a non-Linux host, or a failed sample omits the submessage, and
                # HasField is False, so the previous values (or NULL) are kept
                # rather than clobbered with 0%.
                if hb is not None and hb.HasField("metrics"):
                    m = hb.metrics
                    server.cpu_pct = m.cpu_pct
                    server.mem_pct = m.mem_pct
                    server.disk_pct = m.disk_pct
                    server.metrics_reported_at = now
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
        # 계정 풀 P0: 같은 트랜잭션에서 계정별 창(pct·resets_at)을 정규화 테이블에
        # upsert 하고, 고사용 계정의 경보를 열고 닫는다. 30초 풀 스윕이 읽는 입력이
        # 여기서 만들어진다 — JSONB 원장은 그대로 두고 최신값만 따로 둔다.
        pool.ingest_usage_report(
            db,
            tenant_id=server.tenant_id,
            server_id=server.id,
            payload=snapshot.payload,
            reported_at=_now(),
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
            # The timeline entry is committed BEFORE any alert is opened, so a
            # rejected alert write cannot take the event with it (design note §4
            # kept the two in one transaction; the split is deliberate and only
            # ever loses the alert, never the event). commit also assigns
            # snapshot.id, which the alert cites.
            db.commit()
            snapshot_id = snapshot.id
            # Promote the P3-carried all_exhausted hook to a real alert, and open
            # the quarantine / credential_unusable alerts.
            #
            # Every branch's write is isolated: the DB can refuse a kind the
            # deployed schema does not yet admit (`ck_alerts_kind` is widened by a
            # migration, and a server running one migration behind a newer agent
            # WILL see a kind it does not know), and an uncaught IntegrityError
            # here unwinds the session read loop and drops the whole agent stream —
            # which the agent then re-kills on every reconnect, blocking
            # deliver/recall for that server indefinitely. Same opaque convention
            # as _apply_cred_update: roll back, log identifiers only (never the
            # exception text or SQL parameters, §7), keep the session alive. This
            # is what demotes a migration applied out of order from "that server is
            # dead" to "that one alert is missing".
            try:
                self._open_event_alert(
                    db,
                    event,
                    payload,
                    server_id=server_id,
                    tenant_id=tenant_id,
                    snapshot_id=snapshot_id,
                )
                db.commit()
            except Exception:  # noqa: BLE001 - opaque: never surface SQL/parameters (§7)
                db.rollback()
                # The account is part of the identity of a lost alert: two of the
                # three kinds opened here (quarantine, credential_unusable) are
                # account-scoped, so server+kind alone cannot tell an operator WHOSE
                # signal went missing. Derived the same way the alert was keyed;
                # None for a server-scoped kind or an unparsable ref (§7: an id,
                # never credential material).
                _logger.warning(
                    "account event alert not opened (server %s, kind %s, account %s)",
                    server_id,
                    pb.AccountEvent.Kind.Name(event.kind),
                    _event_account_id(payload),
                )

    def _open_event_alert(
        self,
        db,
        event: pb.AccountEvent,
        payload: dict,
        *,
        server_id: uuid.UUID,
        tenant_id: uuid.UUID,
        snapshot_id: uuid.UUID,
    ) -> None:
        """Open the alert an AccountEvent kind implies. Caller commits and owns
        the failure path (see _store_event) — every write in here is inside that
        caller's guarded transaction."""
        if event.kind == pb.AccountEvent.KIND_ALL_EXHAUSTED:
            alerts.open_alert(
                db,
                tenant_id=tenant_id,
                server_id=server_id,
                kind="all_exhausted",
                severity="critical",
                detail=_event_detail(payload),
                source_snapshot_id=snapshot_id,
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
                source_snapshot_id=snapshot_id,
            )
        elif event.kind == pb.AccountEvent.KIND_CREDENTIAL_UNUSABLE:
            # The agent's §5.7 material guard dropped a re-sync push: the
            # active account's on-disk credential carries no token material.
            # This is the FIRST-incident signal — without it the operator only
            # learns of a dead credential once the account is quarantined, by
            # which time it is already unusable for work. Account-scoped
            # (`{server}:{kind}:{account}`) because one account's credential is
            # what went bad; the agent sends it edge-triggered, and open_alert
            # is idempotent by dedupe_key, so a repeat only refreshes.
            # Resolved by the next cred_update that actually stores (see
            # _apply_cred_update), not from a report.
            alerts.open_alert(
                db,
                tenant_id=tenant_id,
                server_id=server_id,
                account_id=_event_account_id(payload),
                kind="credential_unusable",
                severity="warning",
                detail=_event_detail(payload),
                source_snapshot_id=snapshot_id,
            )

    def _apply_cred_update(
        self,
        cred: pb.CredentialUpdate,
        server_id: uuid.UUID,
        tenant_id: uuid.UUID,
        agent_id: str,
        kek: bytes,
        key_id: str,
    ) -> None:
        """Upstream credential re-sync (§5.7, O9 rotating refresh tokens).

        The agent pushes a refreshed OAuth set sealed under this session's KEK
        (AAD = ams_account_id‖agent_id, same envelope as DeliverAccount). AMS
        opens it, re-encrypts under the at-rest Fernet key, and stores it — but
        only when the account belongs to this session's tenant (§7: never trust a
        client tenant), is actually assigned to THIS session's server (a session
        may only re-seal a credential it legitimately holds — otherwise any
        session could overwrite a sibling server's account under its own KEK), and
        the observed_at is strictly newer than the stored one (monotonicity) yet
        not implausibly in the future (skew clamp, no lock-in), and the set still
        carries token material (a logged-out shell must never overwrite the live
        copy — see ``_credential_has_material``). Every failure path
        is opaque: only account identifiers
        are logged, never ciphertext, KEK, or plaintext (§7). Plaintext lives in
        memory only from open to re-seal and is dropped immediately after.
        """
        raw_id = cred.account.ams_account_id
        try:
            account_id = uuid.UUID(str(raw_id))
        except (ValueError, TypeError):
            _logger.warning("cred_update rejected: malformed account id")
            return
        # observed_at is the monotonicity authority; a re-sync without it cannot
        # be ordered against the stored copy, so it is refused.
        if not cred.HasField("observed_at"):
            _logger.warning("cred_update rejected: no observed_at (account %s)", account_id)
            return
        # protobuf does not range-check Timestamp seconds on the wire, so a
        # malformed stamp (e.g. seconds=2**62) makes ToDatetime raise. Left
        # uncaught it would unwind the session read loop and drop the whole agent
        # stream (availability); isolate it as an opaque reject like the guards
        # around it. The raw value is never logged, only the account id.
        try:
            observed_at = cred.observed_at.ToDatetime(tzinfo=UTC)
        except (ValueError, OverflowError):
            _logger.warning(
                "cred_update rejected: invalid observed_at (account %s)", account_id
            )
            return
        # observed_at drives an irreversible monotonic ratchet: once stored, every
        # future re-sync must beat it. A stamp implausibly far in the future would
        # therefore pin the account forever and starve honest rotations (lock-in).
        # Reject anything past now + allowed skew; genuine agent clock drift stays
        # under the bound, so the past/near-now monotonicity path is untouched.
        if observed_at > _now() + _OBSERVED_AT_MAX_SKEW:
            _logger.warning(
                "cred_update rejected: observed_at too far in the future (account %s)",
                account_id,
            )
            return
        enc = cred.encrypted_credential

        with self._sm() as db:
            account = db.scalar(
                select(Account).where(
                    Account.id == account_id, Account.tenant_id == tenant_id
                )
            )
            if account is None:
                # Unknown to this tenant, or cross-tenant probe — indistinguishable
                # by design (§7 tenant isolation).
                _logger.warning("cred_update rejected: unknown account for tenant")
                return
            # Ownership: this session may only re-seal a credential its own server
            # actually holds. Without this, a session could push a set sealed under
            # its KEK for a sibling server's account (AAD re-derives from the
            # session agent_id, so it authenticates) and overwrite that account's
            # at-rest secret. Require an assignment of this account to THIS session's
            # server in a state where the credential is actually resident locally
            # (§5.2): active (enable), inactive (disable, O2-preserved), quarantined
            # (exhausted). pending is pre-deliver (no local copy yet), recalling is
            # mid-removal, detached is terminal — none legitimately hold it, so all
            # are excluded. A missing assignment is refused opaquely, so an unowned
            # account is indistinguishable from an unknown one (§7).
            owns = db.scalar(
                select(Assignment.id).where(
                    Assignment.account_id == account.id,
                    Assignment.server_id == server_id,
                    Assignment.tenant_id == tenant_id,
                    Assignment.state.in_(("active", "inactive", "quarantined")),
                )
            )
            if owns is None:
                _logger.warning("cred_update rejected: unknown account for tenant")
                return
            # The KEK is per-session; a mismatched key_id cannot be opened with the
            # KEK we hold, so reject before touching the AEAD.
            if not enc.key_id or enc.key_id != key_id:
                _logger.warning("cred_update rejected: unknown key id (account %s)", account.id)
                return
            # Monotonicity pre-check (the atomic guard is the conditional UPDATE
            # below; this only avoids needless crypto on a stale push).
            if (
                account.credential_observed_at is not None
                and observed_at <= account.credential_observed_at
            ):
                _logger.info("cred_update ignored: not newer (account %s)", account.id)
                return

            # AAD is derived from values AMS already holds — the looked-up account
            # id and the session's own agent_id — never from the wire aad_* copies
            # (proto §6.2 warning). A record sealed for a different (account, agent)
            # fails authentication here.
            try:
                plaintext = signing.open_credential(
                    kek,
                    enc.ciphertext,
                    enc.nonce,
                    ams_account_id=str(account.id),
                    agent_id=agent_id,
                )
            except Exception:  # noqa: BLE001 - opaque: never surface crypto detail (§7)
                _logger.warning(
                    "cred_update rejected: decryption/authentication failed (account %s)",
                    account.id,
                )
                return

            # A record can authenticate (right KEK + AAD) yet carry non-UTF-8 bytes
            # — a self-sealed malformed push. Treat a decode failure as an opaque
            # reject, not an exception: an uncaught UnicodeDecodeError would unwind
            # the session read loop and drop the stream. Wipe the plaintext on both
            # paths (§7); only the account id is logged.
            try:
                secret = plaintext.decode()
            except UnicodeDecodeError:
                _logger.warning(
                    "cred_update rejected: credential is not valid UTF-8 (account %s)",
                    account.id,
                )
                return
            finally:
                del plaintext
            # A well-formed, authenticated push can still be a logged-out shell —
            # a credential file emptied of its tokens. Storing it would replace the
            # only live copy AMS holds with one nothing can authenticate against,
            # and the observed_at ratchet would make the loss permanent. Refuse it
            # opaquely and leave the row alone: credential_observed_at must NOT
            # advance either, or the recovered credential could not be pushed
            # afterwards.
            #
            # Credential-shape checks are uneven across the write paths, so this is
            # the only guard on THIS path: inventory.create_account/update_account
            # validate a supplied secret only when provider == "codex"
            # (_validate_codex_secret), oauth_enroll.build_credential_set requires
            # access+refresh tokens on the claude OAuth path, and a manually
            # supplied claude secret is not shape-checked at all
            # (_apply_credential_metadata only best-effort scrapes metadata and
            # ignores parse failures).
            if not _credential_has_material(secret, account.provider):
                del secret
                _logger.warning(
                    "cred_update rejected: credential carries no token material (account %s)",
                    account.id,
                )
                # An agent old enough to predate the agent-side §5.7 guard sends no
                # KIND_CREDENTIAL_UNUSABLE event, so THIS reject is the only place
                # the incident is observable — without an alert here a mixed-version
                # fleet keeps the 08-17 original silent. Same kind and the same
                # derived key (`{server}:{kind}:{account}`), so an agent that DOES
                # send the event only refreshes the one open row rather than
                # double-opening, and the recovery path (a cred_update that stores)
                # closes both.
                #
                # Isolated on purpose: the reject verdict above is already final
                # (nothing was written, the caller returns either way), and this
                # gets its own commit/rollback so a refused alert write — e.g. a
                # `ck_alerts_kind` the deployed schema has not been widened for —
                # cannot unwind the session read loop or turn the reject into an
                # exception. Opaque like every other failure path here: identifiers
                # only, never credential material (§7).
                try:
                    alerts.open_alert(
                        db,
                        tenant_id=tenant_id,
                        server_id=server_id,
                        account_id=account.id,
                        kind="credential_unusable",
                        severity="warning",
                        detail={
                            "source": "ams_cred_update_guard",
                            "detail": "pushed credential carries no token material",
                            "provider": account.provider,
                        },
                    )
                    db.commit()
                except Exception:  # noqa: BLE001 - opaque, and never revives the push
                    db.rollback()
                    _logger.warning(
                        "credential_unusable alert not opened "
                        "(server %s, account %s)",
                        server_id,
                        account.id,
                    )
                return
            # Re-encrypt under the at-rest key and drop the plaintext before any
            # DB round-trip. A missing tenant DEK or a KEK-provider failure raises
            # KekError here; left uncaught it would unwind the session read loop and
            # drop the whole agent stream (availability). Isolate it as an opaque
            # reject — same convention as the crypto failures above — keep the
            # session alive, leave the account untouched (no write happened yet),
            # and wipe the plaintext on the failure path too (§7).
            try:
                new_secret = crypto.encrypt_secret(secret, tenant_id=tenant_id, db=db)
            except KekError:
                del secret
                _logger.warning(
                    "cred_update rejected: at-rest encryption unavailable (account %s)",
                    account.id,
                )
                return
            new_mask = crypto.mask_secret(account.credential_type, secret)
            # Lift the non-secret metadata (expiry, scopes, account/org identity)
            # from the plaintext already in hand — re-fetching it later is not
            # possible, and without this the row keeps whatever the credential
            # said at enrolment while every rotation moves the real expiry
            # forward, so the console eventually reports a live credential as
            # expired. Extraction is best-effort and never raises: an opaque or
            # unparsable secret yields {} and leaves those columns untouched,
            # and a raise here would unwind the session read loop. The fields are
            # UNTRUSTED: the signature and the AAD prove which agent sealed the
            # record, not that its contents are sane, so the extractor also drops
            # anything a text/JSONB column cannot hold.
            new_meta = inventory.credential_metadata_values(account.provider, secret)
            del secret

            # Atomic monotonic update: the WHERE clause makes the observed_at guard
            # part of the write, so a concurrent re-sync or a deliver reading in
            # parallel cannot lose to a stale push.
            stmt = (
                update(Account)
                .where(
                    Account.id == account.id,
                    Account.tenant_id == tenant_id,
                    or_(
                        Account.credential_observed_at.is_(None),
                        Account.credential_observed_at < observed_at,
                    ),
                )
                .values(
                    encrypted_secret=new_secret,
                    secret_masked=new_mask,
                    credential_observed_at=observed_at,
                    updated_at=_now(),
                    # Metadata rides in THIS statement, never a second one: the
                    # WHERE clause above is the monotonicity guard, so a separate
                    # UPDATE would write outside it and let a stale push repaint a
                    # newer row's expiry/scopes. When the guard rejects the push
                    # (rowcount 0) the metadata is correctly dropped with it.
                    **new_meta,
                )
            )
            # Last line of defence. `credential_metadata_values` already refuses
            # the known unstorable shapes, but an escaping DataError here would
            # unwind the session read loop and kill the stream — and the agent
            # would re-kill it on every reconnect, blocking deliver/recall for
            # that server. Worse, a driver exception carries the statement's bound
            # parameters, so it would surface the at-rest ciphertext and the mask
            # in a gRPC status detail. Same opaque convention as the crypto
            # failures above: roll back, log the account id and nothing else,
            # leave the row untouched, keep the session alive.
            try:
                result = db.execute(stmt)
                db.commit()
            except Exception:  # noqa: BLE001 - opaque: never surface SQL/parameters (§7)
                db.rollback()
                _logger.warning(
                    "cred_update rejected: could not be stored (account %s)", account.id
                )
                return
            if result.rowcount:
                _logger.info("cred_update applied (account %s)", account.id)
                # A push that actually stored is first-hand proof the credential
                # came back, so close any credential_unusable alert this account
                # left open — otherwise it stands open forever, since nothing else
                # observes the recovery. The credential write is COMMITTED above and
                # must survive a failure here, so this resolve gets its own
                # transaction and its own rollback: at worst a stale alert stays
                # open, never a lost credential.
                try:
                    alerts.resolve(
                        db,
                        server_id=server_id,
                        kind="credential_unusable",
                        account_id=account.id,
                    )
                    db.commit()
                except Exception:  # noqa: BLE001 - opaque, and never undoes the store
                    db.rollback()
                    _logger.warning(
                        "credential_unusable alert not resolved (account %s)", account.id
                    )
            else:
                _logger.info("cred_update ignored: not newer (account %s)", account.id)

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
    if os.environ.get("AMX_ALLOW_RAW_KEK") == "1":
        # Startup guard (ADVERSARY a): if this ever leaks into production it hands
        # a raw KEK to any keyless client. Real AMA always sends a public key so
        # the raw path is unreachable, but there is no prod/dev hard gate — so we
        # shout once at startup, in addition to the per-fallback warning.
        _logger.warning(
            "SECURITY: AMX_ALLOW_RAW_KEK=1 — raw KEK fallback enabled for keyless "
            "agents. This is a DEV/TEST-ONLY option and MUST NOT be set in "
            "production (§7)."
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
        # D2 sent-未ack recovery (recovery-architecture §2): a sibling sweep on the
        # same timer — a command pushed but never acked is re-queued (idempotent)
        # or, past the attempt cap, failed with its assignment reverted. Isolated
        # from the offline sweep above so one failing never suppresses the other.
        try:
            requeued, failed = await asyncio.to_thread(
                _sweep_sent_once, session_factory
            )
            if requeued or failed:
                _logger.info(
                    "sent-ack sweeper re-queued %d, failed %d command(s)",
                    len(requeued),
                    len(failed),
                )
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - a sweep failure must not kill the process
            _logger.warning("sent-ack sweep iteration failed", exc_info=False)
        # F5 billing outbox: a third sibling sweep on the same timer aggregates
        # newly-closed UTC days from the usage_snapshots ledger into
        # billing_events. Its own advisory lock (…03) and isolated try/except
        # keep it independent of the two sweeps above.
        try:
            created = await asyncio.to_thread(_sweep_billing_once, session_factory)
            if created:
                _logger.info("billing sweeper created %d billing event(s)", created)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - a sweep failure must not kill the process
            _logger.warning("billing sweep iteration failed", exc_info=False)
        # Usage-cost rollup: a fourth sibling sweep on the same timer compacts
        # newly-closed UTC days from the usage_snapshots ledger into
        # usage_daily_rollup (the cost-allocation input). Its own advisory lock
        # (…04), cursor ("usage_rollup"), and isolated try/except keep it fully
        # independent of the billing sweep above.
        try:
            rolled = await asyncio.to_thread(_sweep_usage_rollup_once, session_factory)
            if rolled:
                _logger.info("usage-rollup sweeper upserted %d rollup row(s)", rolled)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - a sweep failure must not kill the process
            _logger.warning("usage-rollup sweep iteration failed", exc_info=False)
        # Snapshot retention: a fifth sibling sweep on the same timer purges raw
        # usage_snapshots past the retention window that are already settled (both
        # the rollup and billing watermarks have sealed them). Own advisory lock
        # (…05) and isolated try/except keep it independent of the sweeps above.
        try:
            purged = await asyncio.to_thread(
                _sweep_snapshot_retention_once, session_factory
            )
            if purged:
                _logger.info("snapshot retention sweeper purged %d snapshot(s)", purged)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - a sweep failure must not kill the process
            _logger.warning("snapshot retention sweep iteration failed", exc_info=False)
        # G27 watermark-future guard: a sixth sibling sweep flags the case where a
        # forward wall-clock step has parked the rollup watermark ahead of real
        # time, stranding below-watermark snapshots as silently unbilled. Its own
        # advisory lock (…06) and isolated try/except keep it independent.
        try:
            watermark_future = await asyncio.to_thread(
                _sweep_watermark_future_once, session_factory
            )
            if watermark_future:
                _logger.warning(
                    "usage-rollup watermark is ahead of real time; "
                    "billing_watermark_future alert(s) open"
                )
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - a sweep failure must not kill the process
            _logger.warning("watermark-future sweep iteration failed", exc_info=False)
        # P4 Langfuse metrics: a seventh sibling sweep polls the external Langfuse
        # Metrics API and compacts the recent-day window into langfuse_usage_rollup
        # for the console. Its own advisory lock (…07, owned inside the service),
        # HTTP-only input, and isolated try/except keep it independent of the ledger
        # sweeps above; a no-op unless the four AMX_LANGFUSE_* settings are present.
        try:
            langfuse_rolled = await asyncio.to_thread(
                _sweep_langfuse_metrics_once, session_factory
            )
            if langfuse_rolled:
                _logger.info(
                    "langfuse metrics sweeper upserted %d rollup row(s)", langfuse_rolled
                )
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - a sweep failure must not kill the process
            _logger.warning("langfuse metrics sweep iteration failed", exc_info=False)
        # P5 Langfuse 임계값 경보(BACKLOG G41): 여덟 번째 형제 스윕이 usage_spike/stale/
        # latency 3종을 실측 평가해 open/resolve 한다. langfuse 활성 게이트/폴 주기를
        # 공유하고 자체 락(…09)·격리 try/except로 독립적이다(무설정 시 no-op).
        try:
            langfuse_alerted = await asyncio.to_thread(
                _sweep_langfuse_alerts_once, session_factory
            )
            if langfuse_alerted:
                _logger.info(
                    "langfuse alert sweeper opened %d threshold alert(s)", langfuse_alerted
                )
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - a sweep failure must not kill the process
            _logger.warning("langfuse alert sweep iteration failed", exc_info=False)
        # Assignment retention: a ninth sibling sweep purges detached assignment
        # history older than AMX_ASSIGNMENT_RETENTION_DAYS (G54). Own advisory lock
        # (…0A) and isolated try/except keep it independent; a no-op when disabled.
        try:
            assignments_purged = await asyncio.to_thread(
                _sweep_assignment_retention_once, session_factory
            )
            if assignments_purged:
                _logger.info(
                    "assignment retention sweeper purged %d detached assignment(s)",
                    assignments_purged,
                )
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - a sweep failure must not kill the process
            _logger.warning("assignment retention sweep iteration failed", exc_info=False)
        # Audit retention: a tenth sibling sweep purges admin audit rows older than
        # AMX_AUDIT_RETENTION_DAYS (G53). Own advisory lock (…0B) and isolated
        # try/except keep it independent; a no-op unless retention is opt-in (>0).
        try:
            audit_purged = await asyncio.to_thread(
                _sweep_audit_retention_once, session_factory
            )
            if audit_purged:
                _logger.info(
                    "audit retention sweeper purged %d audit log(s)", audit_purged
                )
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - a sweep failure must not kill the process
            _logger.warning("audit retention sweep iteration failed", exc_info=False)
        # Session-usage retention: an eleventh sibling sweep purges session_usage
        # rows older than AMX_SESSION_USAGE_RETENTION_DAYS. Own advisory lock (…0C)
        # and isolated try/except keep it independent. Unlike the snapshot purge it
        # needs no settlement guard — nothing integrates over that table.
        try:
            sessions_purged = await asyncio.to_thread(
                _sweep_session_usage_retention_once, session_factory
            )
            if sessions_purged:
                _logger.info(
                    "session usage retention sweeper purged %d row(s)", sessions_purged
                )
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - a sweep failure must not kill the process
            _logger.warning("session usage retention sweep iteration failed", exc_info=False)
        # 계정 풀 P1: 열두 번째 형제 스윕이 배정·창 관측으로부터 계정의 pool_state 를
        # 다시 계산하고, mode=auto 서버의 교체 권고를 갱신한다. 자체 락(…0D)과 격리
        # try/except 로 독립적이며, **명령은 한 줄도 내지 않는다**(관측만).
        try:
            pool_changes = await asyncio.to_thread(_sweep_pool_once, session_factory)
            if pool_changes:
                _logger.info("pool sweeper applied %d change(s)", pool_changes)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - a sweep failure must not kill the process
            _logger.warning("pool sweep iteration failed", exc_info=False)
        # NOTE: 경보 웹훅 드레인은 이 공유 루프에 두지 않는다 — 불량 수신자로 인한 HTTP
        # 지연이 오프라인 탐지·명령 복구를 밀어내지 못하게, 전용 백그라운드 태스크
        # (_alert_webhook_drainer, 자체 주기)로 분리했다. 락 …08은 그대로 유지한다.


ALERT_WEBHOOK_DRAIN_MIN_SECONDS = 5.0


async def _alert_webhook_drainer(
    session_factory: sessionmaker[Session],
    *,
    interval: float | None = None,
) -> None:
    """경보 웹훅 아웃박스를 드레인하는 전용 루프(offline 스위퍼와 분리).

    자체 주기(``AMX_ALERT_WEBHOOK_DRAIN_SECONDS``, 최소 5초 클램프)로 돌며, 불량 수신자의
    느린 POST가 오프라인 탐지·명령 복구 같은 다른 배경 작업을 지연시키지 못하게 한다.
    드레인은 전용 락 …08을 잡아 다중 인스턴스에서 한 인스턴스만 발송한다(무설정 시 no-op).
    한 반복의 실패는 로그만 남기고 루프를 계속한다.
    """
    if interval is None:
        from app.config import get_settings

        interval = max(
            ALERT_WEBHOOK_DRAIN_MIN_SECONDS, get_settings().alert_webhook_drain_seconds
        )
    while True:
        await asyncio.sleep(interval)
        try:
            sent = await asyncio.to_thread(_sweep_alert_webhook_once, session_factory)
            if sent:
                _logger.info("alert webhook drainer delivered %d event(s)", sent)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - a drain failure must not kill the process
            _logger.warning("alert webhook drain iteration failed", exc_info=False)


# F3 multi-instance: the sweeps below are idempotent, but running them from every
# AMS instance every tick is wasted work and needless row contention. A fixed
# transaction-scoped PostgreSQL advisory lock lets exactly one instance own each
# sweep per tick; instances that fail to acquire it skip this tick. The lock is
# transaction-scoped (pg_try_advisory_xact_lock) so it auto-releases on commit or
# rollback — no explicit unlock, and no risk of a leaked lock on a pooled
# connection. Distinct keys keep the offline and sent-ack sweeps independent, so
# they can run on different instances concurrently, matching their isolated
# error handling in the sweeper loop.
_OFFLINE_SWEEP_LOCK_KEY = 0x414D580F01
_SENT_SWEEP_LOCK_KEY = 0x414D580F02
# …03 billing, …04 rollup, …05 snapshot-retention, …07 langfuse-metrics,
# …08 alert-webhook, …09 langfuse-alerts, …0A assignment-retention,
# …0B audit-retention, …0C session-usage-retention, …0D account-pool
# (in their own modules).
_WATERMARK_SWEEP_LOCK_KEY = 0x414D580F06


def _sweep_once(
    session_factory: sessionmaker[Session], stale_after: float
) -> list[uuid.UUID]:
    with session_factory() as db:
        if not _try_advisory_xact_lock(db, _OFFLINE_SWEEP_LOCK_KEY):
            return []
        return alerts.sweep_offline(db, stale_after_seconds=stale_after)


def _sweep_sent_once(
    session_factory: sessionmaker[Session],
) -> tuple[list[str], list[str]]:
    with session_factory() as db:
        if not _try_advisory_xact_lock(db, _SENT_SWEEP_LOCK_KEY):
            return [], []
        return commands.sweep_sent_timeouts(db)


def _sweep_billing_once(session_factory: sessionmaker[Session]) -> int:
    # billing.sweep_billing takes its own advisory lock (…03) and commits.
    with session_factory() as db:
        return billing.sweep_billing(db)


def _sweep_usage_rollup_once(session_factory: sessionmaker[Session]) -> int:
    # usage_cost.sweep_usage_rollup takes its own advisory lock (…04), separate
    # cursor ("usage_rollup"), and commits — independent of the billing sweep.
    with session_factory() as db:
        return usage_cost.sweep_usage_rollup(db)


def _sweep_snapshot_retention_once(session_factory: sessionmaker[Session]) -> int:
    # usage_cost.sweep_snapshot_retention takes its own advisory lock (…05) and
    # commits per batch — independent of the rollup and billing sweeps.
    with session_factory() as db:
        return usage_cost.sweep_snapshot_retention(db)


def _sweep_langfuse_metrics_once(session_factory: sessionmaker[Session]) -> int:
    # langfuse_metrics.sweep_langfuse_metrics takes its own advisory lock (…07),
    # polls the external Metrics API and commits — a no-op when unconfigured.
    with session_factory() as db:
        return langfuse_metrics.sweep_langfuse_metrics(db)


def _sweep_alert_webhook_once(session_factory: sessionmaker[Session]) -> int:
    # alert_webhook.sweep_alert_webhook takes its own advisory lock (…08), drains
    # the outbox with HTTP POSTs outside the lock, and commits — a no-op when the
    # webhook is unconfigured.
    with session_factory() as db:
        return alert_webhook.sweep_alert_webhook(db)


def _sweep_assignment_retention_once(session_factory: sessionmaker[Session]) -> int:
    # inventory.sweep_assignment_retention takes its own advisory lock (…0A) and
    # commits per batch — independent of the ledger sweeps. Purges aged-out
    # detached assignment history (G54); a no-op when retention is disabled.
    with session_factory() as db:
        return inventory.sweep_assignment_retention(db)


def _sweep_audit_retention_once(session_factory: sessionmaker[Session]) -> int:
    # audit.sweep_audit_retention takes its own advisory lock (…0B) and commits
    # per batch. Purges aged-out admin audit rows (G53); a no-op unless
    # AMX_AUDIT_RETENTION_DAYS > 0 (default keeps the trail forever).
    with session_factory() as db:
        return audit.sweep_audit_retention(db)


def _sweep_session_usage_retention_once(session_factory: sessionmaker[Session]) -> int:
    # session_usage.sweep_session_usage_retention takes its own advisory lock (…0C)
    # and commits per batch. Purges aged-out session cost-structure rows; a no-op
    # when AMX_SESSION_USAGE_RETENTION_DAYS <= 0.
    with session_factory() as db:
        return session_usage.sweep_session_usage_retention(db)


def _sweep_pool_once(session_factory: sessionmaker[Session]) -> int:
    # pool.sweep_pool 은 관측(…0D 락)과 체인 전진·자동 착수(…0E 락)를 이 순서로
    # 한 틱에 돈다. P2/P3 부터는 명령을 발행하므로 더 이상 무해한 스윕이 아니다 —
    # 다만 체인 한 걸음은 판단과 발행이 같은 트랜잭션이라, 중간에 죽어도 절반만
    # 적용된 상태는 남지 않고 다음 틱이 배정 상태를 보고 이어 간다.
    with session_factory() as db:
        return pool.sweep_pool(db)


def _sweep_langfuse_alerts_once(session_factory: sessionmaker[Session]) -> int:
    # langfuse_alerts.sweep_langfuse_alerts takes its own advisory lock (…09),
    # shares the langfuse enable-gate/poll cadence, and commits — a no-op when
    # langfuse is unconfigured.
    with session_factory() as db:
        return langfuse_alerts.sweep_langfuse_alerts(db)


def _sweep_watermark_future_once(session_factory: sessionmaker[Session]) -> bool:
    # usage_cost.sweep_watermark_future reads the rollup cursor and commits the
    # per-tenant alert lifecycle; the transaction-scoped lock (…06) makes exactly
    # one instance run it per tick.
    with session_factory() as db:
        if not _try_advisory_xact_lock(db, _WATERMARK_SWEEP_LOCK_KEY):
            return False
        return usage_cost.sweep_watermark_future(db)


async def serve(port: int = DEFAULT_PORT) -> None:
    from app.config import get_settings

    get_settings()  # fail fast on missing configuration (§7)
    signer = signing.Signer.from_env_or_generate()
    server, servicer = create_server(signer)
    mode = configure_port(server, port)
    await server.start()
    _logger.info("AMS gRPC control plane listening on :%s (%s)", port, mode)
    sweeper = asyncio.create_task(_offline_sweeper(servicer._sm))
    # 웹훅 드레인은 offline 스위퍼와 독립된 전용 태스크로 돈다(불량 수신자 격리).
    webhook_drainer = asyncio.create_task(_alert_webhook_drainer(servicer._sm))
    try:
        await server.wait_for_termination()
    finally:
        sweeper.cancel()
        webhook_drainer.cancel()


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    asyncio.run(serve())


if __name__ == "__main__":
    main()
