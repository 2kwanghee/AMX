# AMX P2 end-to-end suite

The P2 completion criterion (design note §9) is a claim about three independent
hosts: assign ten accounts 3/5/2 across servers A, B and C, deliver them through
the channel, and each host's tsamx pool should end up holding exactly its share.
Recall two of them and the assignments detach at AMS while the local records
survive, disabled (the O2 decision).

Nothing short of three real agent processes can settle that, so this suite runs
the whole control plane as separate processes.

## Running it

```
AMX_GO_BIN=/path/to/go uv run --project ams-server pytest e2e/ -q
```

from the repository root. `AMX_GO_BIN` is only needed when `go` is not on
`PATH`; the suite also looks in `~/go-sdk`, `~/go-toolchain` and `/usr/local/go`.
A full run takes a few seconds once the Docker image, the Go build cache and the
uv cache are warm, and stays well inside a minute cold.

Requirements: Docker (for PostgreSQL), a Go toolchain, and `uv`.

### P4 console round trip

`test_p4_console_e2e.py` adds the P4 completion criterion (design note §9): the
whole account lifecycle and the alert round trip driven through the **real
ams-web BFF** against a live ams-server. Single command:

```
AMX_GO_BIN=/path/to/go uv run --project ams-server pytest e2e/test_p4_console_e2e.py -q
```

It additionally needs **Node** (to run the actual ams-web Route Handlers) on
`PATH`. Unlike P2/P3 this stands the REST API up as a real HTTP process
(uvicorn) so the BFF can `fetch` it; every console operation — create
tenant/account/server, enroll, assign, deliver, list/ack/resolve the alert,
recall — goes through the BFF's session + proxy handlers, and every BFF response
is scanned so the admin token never reaches the browser side. The alert is
induced with the P3 recipe (two accounts exhausted, an auto tick emits
`KIND_ALL_EXHAUSTED`) and resolved by a console `:refresh-usage`.

## What actually runs

| Piece | How it runs |
| --- | --- |
| PostgreSQL | throwaway container, migrated to head with alembic |
| AMS REST | in-process FastAPI `TestClient` against that database |
| AMS gRPC control plane | separate `python -m app.grpc.server` process on its own port |
| AMA daemons | three compiled `ama` binaries, each in its own HOME / `CLAUDE_CONFIG_DIR` / `XDG_DATA_HOME` |
| tsamx | the real CLI from `tsamx/`, installed into a throwaway virtualenv |

The two halves of AMS are coupled only through the database, as in production:
REST writes an `agent_commands` outbox row, and the gRPC process signs it with a
per-run Ed25519 key and pushes it down the session stream. Each agent verifies
that signature before applying anything, so a passing run is also evidence the
signing chain holds end to end.

## What is mocked, and why nothing touches the network

Credential sets are synthetic OAuth documents. They deliberately carry **no
`accessToken`**, which is what keeps the suite offline: tsamx derives a static
"no credentials" usage state for such an account and never enters its
usage-fetch path, so the Anthropic usage API is never called. There is no real
login, no real token, and no outbound request in a run.

That is the only concession. The delivery path itself is exercised for real —
sealing under the session KEK, the gRPC round trip, opening the envelope with
the locally derived AAD, staging the credential into the Claude config home, and
`tsamx add` capturing it into a slot.

## What it asserts

1. All ten assignments reach `active` after `:deliver`.
2. Each host's `tsamx list --json` holds exactly its own three / five / two
   accounts — a set comparison, so a leak across hosts fails it too — and each
   host's `manifest.enc` agrees.
3. After `:recall`, those assignments are `detached` at AMS while the local
   tsamx record is still present and merely `disabled`, and the manifest record
   is retained with `allocationStatus` INACTIVE and its ciphertext intact (O2).
4. Untouched assignments on the same host stay `active`.
5. No delivered refresh token, and no `refreshToken` key at all, appears in any
   agent log or in the manifest's plaintext metadata (§7).

Cross-tenant rejection is not re-tested here — `ams-server/tests/test_grpc_channel.py`
covers it against the same server code.

## Failure diagnosis

Every subprocess logs to `<pytest tmp>/amx-e2e*/logs/`. Convergence assertions
attach the tail of all three agent logs to the failure message, so a stuck
delivery usually explains itself without re-running.
