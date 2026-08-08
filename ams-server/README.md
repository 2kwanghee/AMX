# ams-server

AMS management plane — P1 inventory. Tenants, accounts, servers and assignments
plus the central OAuth enrollment flow of `docs/AMX-DESIGN.md` §5.5. There is no
AMS↔AMA channel yet: every endpoint that needs one answers 501 (see
`app/api/v1/stubs.py`).

## Configuration

All three are required; the process refuses to start without them (§7).

| Variable | Purpose |
|---|---|
| `AMX_DATABASE_URL` | e.g. `postgresql+psycopg://amx:…@localhost/amx` |
| `AMX_ENCRYPTION_KEY` | Fernet key for `accounts.encrypted_secret` |
| `AMX_ADMIN_TOKEN` | Bearer token for the REST API (≥16 chars) |

```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

## Run

```bash
uv venv && uv pip install -e ".[dev]"
alembic upgrade head
uvicorn --factory app.main:create_app
```

## Test

`pytest` starts a throwaway PostgreSQL container over the docker CLI — the
§5.1 isolation invariant is composite foreign keys and a partial unique index,
which only a real PostgreSQL can enforce. No test reaches the network beyond
that container; the OAuth token endpoint is always stubbed.

```bash
pytest
```

## Verifying a stored credential

`scripts/verify_credential.py --tenant <uuid> --account <uuid>` prints one
boolean per credential field. It never prints a value, so its output is safe to
paste anywhere.
