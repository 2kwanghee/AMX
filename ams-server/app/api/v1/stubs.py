"""Endpoints that cannot work until the AMS↔AMA channel exists (P2).

Every action in §5.2's state machine converges on the agent's `CommandAck`, so
an AMS that moved an assignment to `delivering` with no channel to deliver over
would be recording a state that can never be reached. These return 501 with the
reason instead — an honest "not yet", not a silent no-op.

The route bodies still resolve the tenant and the resource first, so a
cross-tenant probe gets 404 rather than a 501 that would confirm the id exists.
"""

from __future__ import annotations

from app.core.errors import ApiError, not_implemented

P2_REASON = (
    "Requires the AMS↔AMA gRPC channel, which lands in P2 "
    "(docs/AMX-DESIGN.md §9). P1 is inventory only: the row exists, but no "
    "command can be delivered to an agent yet."
)


def requires_channel(code: str, endpoint: str) -> ApiError:
    return not_implemented(f"{code}.not_implemented", f"{endpoint} — {P2_REASON}")
