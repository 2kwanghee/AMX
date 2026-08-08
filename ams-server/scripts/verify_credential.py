#!/usr/bin/env python3
"""Check that a stored credential set decrypts and is complete.

This is the P1 completion check of docs/AMX-DESIGN.md §9: enroll one real
account through the OAuth flow, then confirm `encrypted_secret` opens into a
*complete* credential set — the thing the retired setup-token path could not
produce (§2.4-5).

It prints presence, never values. No token, refresh token, email or
organization name is ever written to stdout, so the output is safe to paste
into a ticket.

    AMX_DATABASE_URL=... AMX_ENCRYPTION_KEY=... AMX_ADMIN_TOKEN=... \\
        python scripts/verify_credential.py --tenant <uuid> --account <uuid>
"""

from __future__ import annotations

import argparse
import json
import sys
import uuid

from sqlalchemy import select

from app.core.crypto import CredentialDecryptionError, decrypt_secret
from app.db import get_sessionmaker
from app.models import Account

REQUIRED_OAUTH_FIELDS = ("accessToken", "refreshToken", "expiresAt", "scopes")
OPTIONAL_OAUTH_FIELDS = (
    "accountUuid",
    "emailAddress",
    "organizationUuid",
    "organizationName",
    "subscriptionType",
)


def _present(value: object) -> bool:
    if value is None:
        return False
    if isinstance(value, (str, list, dict)):
        return len(value) > 0
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tenant", required=True, type=uuid.UUID)
    parser.add_argument("--account", required=True, type=uuid.UUID)
    args = parser.parse_args()

    with get_sessionmaker()() as db:
        account = db.scalars(
            select(Account).where(Account.id == args.account, Account.tenant_id == args.tenant)
        ).first()
        if account is None:
            print("account_found: false")
            return 2
        print("account_found: true")
        print(f"credential_type: {account.credential_type}")

        if not account.encrypted_secret:
            print("encrypted_secret_present: false")
            return 2
        print("encrypted_secret_present: true")

        try:
            plaintext = decrypt_secret(account.encrypted_secret)
        except CredentialDecryptionError:
            print("decrypts: false")
            return 2
        print("decrypts: true")

    if account.credential_type == "api_key":
        print(f"api_key_non_empty: {str(bool(plaintext.strip())).lower()}")
        return 0 if plaintext.strip() else 2

    try:
        payload = json.loads(plaintext)
    except json.JSONDecodeError:
        print("parses_as_json: false")
        return 2
    print("parses_as_json: true")

    oauth = payload.get("claudeAiOauth") if isinstance(payload, dict) else None
    if not isinstance(oauth, dict):
        print("has_claudeAiOauth_envelope: false")
        return 2
    print("has_claudeAiOauth_envelope: true")

    complete = True
    for field in REQUIRED_OAUTH_FIELDS:
        ok = _present(oauth.get(field))
        complete = complete and ok
        print(f"{field}: {str(ok).lower()}")
    for field in OPTIONAL_OAUTH_FIELDS:
        print(f"{field}: {str(_present(oauth.get(field))).lower()}")

    print(f"credential_set_complete: {str(complete).lower()}")
    return 0 if complete else 2


if __name__ == "__main__":
    sys.exit(main())
