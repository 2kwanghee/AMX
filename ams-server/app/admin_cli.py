"""Bootstrap admin CLI (F1 RBAC, §2).

    python -m app.admin_cli create-admin --email a@x --role global-admin
    python -m app.admin_cli create-admin --email t@x --role tenant-admin --tenant-id <uuid>

Creates the first human admin without the API. The password is read from stdin
(getpass) or the AMX_BOOTSTRAP_PASSWORD env var for non-interactive use — never
from argv, and never echoed. No session token is minted here: the admin logs in
through `/auth/login` afterwards. Only the bcrypt hash is written.
"""

from __future__ import annotations

import argparse
import getpass
import os
import sys
import uuid

from app.core.errors import ApiError
from app.db import get_sessionmaker
from app.services import admins


def _read_password() -> str:
    env = os.environ.get("AMX_BOOTSTRAP_PASSWORD")
    if env:
        return env
    pw = getpass.getpass("New admin password: ")
    confirm = getpass.getpass("Confirm password: ")
    if pw != confirm:
        raise SystemExit("error: passwords do not match")
    if not pw:
        raise SystemExit("error: password must not be empty")
    return pw


def _create_admin(args: argparse.Namespace) -> int:
    tenant_id = uuid.UUID(args.tenant_id) if args.tenant_id else None
    password = _read_password()
    with get_sessionmaker()() as session:
        try:
            admin = admins.create_admin(
                session,
                email=args.email,
                password=password,
                role=args.role,
                tenant_id=tenant_id,
            )
        except ApiError as exc:
            print(f"error: {exc.detail or exc.title}", file=sys.stderr)
            return 1
    # Print the id and email only — never a token or the password.
    print(f"created admin {admin.id} <{admin.email}> role={admin.role}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.admin_cli")
    sub = parser.add_subparsers(dest="command", required=True)

    create = sub.add_parser("create-admin", help="create a bootstrap admin")
    create.add_argument("--email", required=True)
    create.add_argument(
        "--role", required=True, choices=("global-admin", "tenant-admin")
    )
    create.add_argument(
        "--tenant-id",
        default=None,
        help="required for tenant-admin, forbidden for global-admin",
    )
    create.set_defaults(func=_create_admin)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
