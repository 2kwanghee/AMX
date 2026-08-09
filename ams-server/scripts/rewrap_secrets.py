#!/usr/bin/env python3
"""Batch rewrap credentials between legacy Fernet and v2 tenant-DEK envelopes.

Forward (default) — step C of the F2 rollout (design §4-C): legacy Fernet rows
that write-path traffic never promotes get swept to v2. Requires
``AMX_ENVELOPE_WRITE=1``.

Reverse (``--reverse``) — the rollback tool: fold v2 rows back to legacy Fernet
before a code/schema downgrade, so no ciphertext is stranded when ``tenant_deks``
goes away (0008 downgrade refuses while any v2 remains). Independent of
``AMX_ENVELOPE_WRITE``.

Both directions are lost-update safe. O9 (`_apply_cred_update`) may commit a
newer credential between our read and our write; a blind UPDATE would resurrect
the stale plaintext we hold (its observed_at stays at the newer value, so the
stale copy would then be delivered and never corrected). So every write is a
compare-and-swap: ``UPDATE ... WHERE encrypted_secret = <exact value we read>``.
rowcount 0 means it changed underneath us — that copy is already current, so we
skip it, never overwrite. Never prints credential material — only counts and ids.

    AMX_DATABASE_URL=... AMX_ENCRYPTION_KEY=... AMX_ADMIN_TOKEN=... \\
    AMX_ENVELOPE_WRITE=1 python scripts/rewrap_secrets.py [--tenant <uuid>] [--dry-run]
    # rollback:
    python scripts/rewrap_secrets.py --reverse [--tenant <uuid>] [--dry-run]
"""

from __future__ import annotations

import argparse
import sys
import uuid

from sqlalchemy import select, update

from app.core import crypto, kek
from app.core.crypto import CredentialDecryptionError
from app.db import get_sessionmaker
from app.models import Account


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tenant", type=uuid.UUID, default=None, help="limit to one tenant")
    parser.add_argument("--dry-run", action="store_true", help="report, do not write")
    parser.add_argument(
        "--reverse",
        action="store_true",
        help="fold v2 -> legacy Fernet (rollback), independent of the write flag",
    )
    args = parser.parse_args()

    if not args.reverse and not args.dry_run and not kek.envelope_write_enabled():
        print("refusing: AMX_ENVELOPE_WRITE=1 is required to write v2 (rollback boundary)")
        return 2

    want_prefix = not args.reverse  # forward wants v2 out; reverse wants no v2
    scanned = rewrapped = skipped = failed = lost = 0
    sm = get_sessionmaker()
    with sm() as db:
        where = [Account.encrypted_secret.is_not(None)]
        if args.tenant is not None:
            where.append(Account.tenant_id == args.tenant)
        account_ids = list(db.scalars(select(Account.id).where(*where)))

    for account_id in account_ids:
        # One short transaction per account so a failure isolates to its row and
        # a long run never holds a single lock. Re-read under the transaction.
        with sm() as db:
            account = db.get(Account, account_id)
            if account is None or not account.encrypted_secret:
                continue
            scanned += 1
            old = account.encrypted_secret
            is_v2 = old.startswith("v2:")
            # Forward skips rows already v2; reverse skips rows already legacy.
            if is_v2 == want_prefix:
                skipped += 1
                continue
            try:
                plaintext = crypto.decrypt_secret(
                    old, tenant_id=account.tenant_id, db=db
                )
            except CredentialDecryptionError:
                failed += 1
                print(f"failed(decrypt): account={account_id}")
                continue
            try:
                if args.reverse:
                    new_ct = crypto.encrypt_secret_fernet(plaintext)
                else:
                    new_ct = crypto.encrypt_secret(
                        plaintext, tenant_id=account.tenant_id, db=db
                    )
            finally:
                del plaintext
            if new_ct.startswith("v2:") != want_prefix:
                # Wrong direction produced (e.g. forward without the write flag);
                # never overwrite with the unintended format.
                failed += 1
                print(f"failed(wrong format): account={account_id}")
                continue
            if args.dry_run:
                rewrapped += 1
                continue
            # Compare-and-swap: only write if the stored value is still exactly
            # what we decrypted. If O9 (or anyone) changed it since our read,
            # rowcount is 0 and we skip — the newer copy stands (no stale revival).
            result = db.execute(
                update(Account)
                .where(Account.id == account_id, Account.encrypted_secret == old)
                .values(encrypted_secret=new_ct)
            )
            db.commit()
            if result.rowcount:
                rewrapped += 1
            else:
                lost += 1  # changed underneath us — already current, left as-is

    print(
        f"mode={'reverse' if args.reverse else 'forward'} scanned={scanned} "
        f"rewrapped={rewrapped} skipped={skipped} raced={lost} failed={failed} "
        f"dry_run={str(args.dry_run).lower()}"
    )
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
