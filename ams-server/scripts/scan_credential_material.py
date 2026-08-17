#!/usr/bin/env python3
"""Sweep every stored credential for the O9 empty-token poisoning (§5.7).

Before the re-sync guard landed, an agent whose local credential file had been
emptied (a logged-out shell — `{"claudeAiOauth":{"accessToken":"","refreshToken":
""}}`) pushed that set upstream and AMS stored it over the only live copy it
held. The account then looked healthy until a cross-server re-assignment
delivered a credential nothing could authenticate against (observed 2026-08-17).
The guard stops new poisoning; it cannot repair a row already written, and
nothing else looks for one.

`encrypted_secret` is Fernet ciphertext, so SQL alone cannot judge it. This
decrypts through app.core.crypto and applies the SAME predicate the guard uses
(`app.grpc.server._credential_has_material`) rather than reimplementing it, so
the report cannot drift away from the guard's judgement.

Output is shape, never content: no token, no ciphertext, no masked digest. Emails
are withheld unless --emails is passed, so the default output is safe to paste
into a ticket; account UUIDs are enough to act on through the console or API.

Exits 0 when every credential carries token material, 1 when any does not (or
fails to decrypt), so it is usable as a manual gate.

    set -a; . ../.amx-dev/dev.env; set +a
    uv run python scripts/scan_credential_material.py [--tenant UUID] [--emails]
"""

from __future__ import annotations

import argparse
import json
import sys
import uuid

from sqlalchemy import select

from app.core.crypto import decrypt_secret
from app.db import get_sessionmaker
from app.grpc.server import _credential_has_material, _is_blank
from app.models import Account, Assignment, Server, Tenant

# provider -> (token block key, the keys inside it that carry a token)
TOKEN_KEYS = {
    "claude": ("claudeAiOauth", ("accessToken", "refreshToken")),
    "codex": ("tokens", ("refresh_token", "access_token")),
}


def describe(secret: str, provider: str) -> str:
    """A value-free description of the token block: presence and length only."""
    if _is_blank(secret):
        return "empty body" if not secret else "blank body (whitespace/control only)"
    try:
        root = json.loads(secret)
    except (ValueError, TypeError, RecursionError, MemoryError):
        return "non-JSON body (an opaque api_key looks like this)"
    if not isinstance(root, dict):
        return f"non-object JSON ({type(root).__name__})"
    spec = TOKEN_KEYS.get(provider)
    if spec is None:
        return f"unknown provider; top-level keys={sorted(root)[:6]}"
    block_key, keys = spec
    if block_key not in root:
        return f"no {block_key} block; top-level keys={sorted(root)[:6]}"
    block = root[block_key]
    if not isinstance(block, dict):
        return f"{block_key} is {type(block).__name__}, not an object"
    bits = []
    for key in keys:
        if key not in block:
            bits.append(f"{key}=absent")
        elif block[key] is None:
            bits.append(f"{key}=null")
        elif not isinstance(block[key], str):
            bits.append(f"{key}=<{type(block[key]).__name__}>")
        elif _is_blank(block[key]):
            bits.append(f"{key}=BLANK")
        else:
            bits.append(f"{key}=len{len(block[key])}")
    if provider == "codex":
        # An emptied tokens block is still usable in the api-key form, so the
        # fallback belongs in the description too.
        api_key = root.get("OPENAI_API_KEY")
        usable = isinstance(api_key, str) and not _is_blank(api_key)
        bits.append(f"OPENAI_API_KEY={'set' if usable else 'absent/blank'}")
    return ", ".join(bits)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tenant", type=uuid.UUID, help="limit the sweep to one tenant")
    parser.add_argument(
        "--emails",
        action="store_true",
        help="include account emails (output is then NOT safe to paste into a ticket)",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="list healthy accounts too, so the shape of every row is visible",
    )
    args = parser.parse_args()

    poisoned: list[str] = []
    undecryptable: list[str] = []
    no_secret: list[str] = []
    healthy: list[str] = []

    with get_sessionmaker()() as db:
        tenants = {t.id: t.name for t in db.scalars(select(Tenant)).all()}
        servers = {s.id: s.name for s in db.scalars(select(Server)).all()}
        where = [Account.tenant_id == args.tenant] if args.tenant else []
        rows = db.scalars(
            select(Account).where(*where).order_by(Account.tenant_id, Account.email)
        ).all()
        placements: dict[uuid.UUID, list[str]] = {}
        for a in db.scalars(select(Assignment)).all():
            placements.setdefault(a.account_id, []).append(
                f"{servers.get(a.server_id, str(a.server_id)[:8])}:{a.state}"
            )

        print(f"tenants={len(tenants)} accounts={len(rows)}")
        for account in rows:
            head = (
                f"  account={account.id} tenant={tenants.get(account.tenant_id, '?')} "
                f"provider={account.provider} status={account.status}"
            )
            if args.emails:
                head += f" email={account.email}"
            head += (
                f"\n    assignments=[{','.join(placements.get(account.id, [])) or '-'}]"
                f" observed_at={account.credential_observed_at}"
                f" expires={account.credential_expires_at}"
            )
            if account.encrypted_secret is None:
                no_secret.append(head)
                continue
            try:
                secret = decrypt_secret(
                    account.encrypted_secret, tenant_id=account.tenant_id, db=db
                )
            except Exception as exc:  # noqa: BLE001 - any failure is equally actionable
                undecryptable.append(head + f"\n    decrypt_error={type(exc).__name__}")
                continue
            carries = _credential_has_material(secret, account.provider)
            detail = head + f"\n    shape: {describe(secret, account.provider)}"
            del secret
            (healthy if carries else poisoned).append(detail)

    for label, rows_out in (
        ("POISONED (the re-sync guard would refuse this set)", poisoned),
        ("NULL encrypted_secret", no_secret),
        ("UNDECRYPTABLE", undecryptable),
    ):
        print(f"\n=== {label}: {len(rows_out)} ===")
        for row in rows_out:
            print(row)
    print(f"\n=== carries token material: {len(healthy)} ===")
    if args.all:
        for row in healthy:
            print(row)

    broken = len(poisoned) + len(undecryptable)
    print("\nRESULT:", "FAIL" if broken else "PASS")
    if poisoned:
        print(
            "  Re-enrol each poisoned account (recall -> delete the assignment -> "
            "delete the account -> OAuth again). AMS holds no usable copy, so a "
            "re-assignment would deliver a dead credential."
        )
    return 1 if broken else 0


if __name__ == "__main__":
    sys.exit(main())
