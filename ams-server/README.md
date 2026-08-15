# ams-server

AMS management plane. Tenants, accounts, servers and assignments, the central
OAuth enrollment flow of `docs/AMX-DESIGN.md` §5.5, and — since P2 landed — the
AMS↔AMA gRPC control channel that actually delivers, switches and recalls
accounts (P2–P5 are merged; see `docs/BACKLOG.md` "진행 현황"). Endpoints that
drive an agent go over that channel now rather than returning 501.

## Configuration

All three are required; the process refuses to start without them (§7).

| Variable | Purpose |
|---|---|
| `AMX_DATABASE_URL` | e.g. `postgresql+psycopg://amx:…@localhost/amx` |
| `AMX_ENCRYPTION_KEY` | Local KEK (a urlsafe-base64 Fernet key) that wraps each tenant's DEK for F2 envelope encryption, and still opens legacy Fernet ciphertext |
| `AMX_ADMIN_TOKEN` | Bearer token for the REST API (≥16 chars) |

```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

At-rest credentials use F2 envelope encryption: a per-tenant DEK (`tenant_deks`,
wrapped by the KEK above) encrypts `accounts.encrypted_secret` with AES-256-GCM,
tenant id bound as AAD, stored as `v2:{dek_version}:{nonce}:{ct}`. Reads
auto-detect the `v2:` prefix, so legacy Fernet ciphertext keeps decrypting;
writes emit v2 only under `AMX_ENVELOPE_WRITE=1` (the rollback boundary). See
`app/core/crypto.py` / `app/core/kek.py` and design §7.

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
