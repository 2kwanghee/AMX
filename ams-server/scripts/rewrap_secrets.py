#!/usr/bin/env python3
"""Batch rewrap legacy (Fernet) credentials into v2 tenant-DEK envelopes.

Step C of the F2 rollout (design §4-C). Write-path traffic (enroll, update, O9
re-sync) lazily promotes rows to v2 on their next write; this script sweeps the
cold rows that never get written, so the legacy Fernet key can eventually be
retired.

Idempotent and safe to re-run: rows already tagged ``v2:`` are skipped. Requires
``AMX_ENVELOPE_WRITE=1`` (v2 writes must be enabled, i.e. the rollback boundary
is already crossed) so a rewrap can never silently re-emit Fernet. Never prints
credential material — only counts and account ids.

    AMX_DATABASE_URL=... AMX_ENCRYPTION_KEY=... AMX_ADMIN_TOKEN=... \\
    AMX_ENVELOPE_WRITE=1 python scripts/rewrap_secrets.py [--tenant <uuid>] [--dry-run]
"""

from __future__ import annotations

import argparse
import sys
import uuid

from sqlalchemy import select

from app.core import crypto, kek
from app.core.crypto import CredentialDecryptionError
from app.db import get_sessionmaker
from app.models import Account


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tenant", type=uuid.UUID, default=None, help="limit to one tenant")
    parser.add_argument("--dry-run", action="store_true", help="report, do not write")
    args = parser.parse_args()

    if not args.dry_run and not kek.envelope_write_enabled():
        print("refusing: AMX_ENVELOPE_WRITE=1 is required to write v2 (rollback boundary)")
        return 2

    scanned = rewrapped = skipped = failed = 0
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
            if account.encrypted_secret.startswith("v2:"):
                skipped += 1
                continue
            try:
                plaintext = crypto.decrypt_secret(
                    account.encrypted_secret, tenant_id=account.tenant_id, db=db
                )
            except CredentialDecryptionError:
                failed += 1
                print(f"failed(decrypt): account={account_id}")
                continue
            try:
                new_ct = crypto.encrypt_secret(
                    plaintext, tenant_id=account.tenant_id, db=db
                )
            finally:
                del plaintext
            if not new_ct.startswith("v2:"):
                # AMX_ENVELOPE_WRITE not honoured — do not overwrite with Fernet.
                failed += 1
                print(f"failed(not v2): account={account_id}")
                continue
            rewrapped += 1
            if not args.dry_run:
                account.encrypted_secret = new_ct
                db.commit()

    print(
        f"scanned={scanned} rewrapped={rewrapped} skipped_v2={skipped} "
        f"failed={failed} dry_run={str(args.dry_run).lower()}"
    )
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
