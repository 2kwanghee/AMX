# contracts/gen — code generation

Generated clients and stubs for the AMX control-plane contract, in Go, Python and
TypeScript. The single source is `../proto/amx.proto`; the JSON report schemas in
`../schemas/` and the REST draft in `../openapi.yaml` are hand-written contracts
that are validated, not generated.

Generated code is **committed** (design SSOT §4), so `ams-server`, `ama-agent`
and `ams-web` build without anyone installing a protobuf toolchain. That only
holds if you regenerate and re-commit whenever the proto changes:

```bash
cd contracts/gen
make all      # lint + generate go, python, typescript
make verify   # compile-check all three
```

Never edit anything under `go/`, `python/` or `typescript/src/` by hand — the
next `make all` overwrites it.

## Layout

| Path | Contents |
|---|---|
| `go/` | `amx.pb.go`, `amx_grpc.pb.go`, plus a `go.mod` making this an importable module (`github.com/2kwanghee/AMX/contracts/gen/go`) |
| `python/` | `amx_pb2.py`, `amx_pb2.pyi`, `amx_pb2_grpc.py`, plus `pyproject.toml`/`uv.lock` pinning the generator |
| `typescript/` | `src/amx.ts` and `src/google/protobuf/timestamp.ts`, plus `package.json`/`tsconfig.json` |

`typescript/node_modules/` and `python/.venv/` are build inputs and stay
untracked.

## Toolchain

Nothing here needs root; every tool installs under `$HOME`. The Makefile puts
`~/.local/bin`, `~/go-toolchain/go/bin` and `~/go/bin` on `PATH` itself, so once
these are installed the targets work from a plain shell.

Check what is present with `make tools-check`.

### buf → `~/.local/bin`

```bash
mkdir -p ~/.local/bin
curl -fsSL "https://github.com/bufbuild/buf/releases/latest/download/buf-Linux-x86_64" \
  -o ~/.local/bin/buf
chmod +x ~/.local/bin/buf
```

### Go toolchain → `~/go-toolchain`

```bash
curl -fsSL "https://go.dev/dl/go1.24.5.linux-amd64.tar.gz" -o /tmp/go.tgz
mkdir -p ~/go-toolchain && tar -C ~/go-toolchain -xzf /tmp/go.tgz
```

Override the location with `make GO_ROOT_DIR=/path/to/go`.

### Go protoc plugins → `~/go/bin`

```bash
export PATH=$HOME/go-toolchain/go/bin:$HOME/go/bin:$PATH
go install google.golang.org/protobuf/cmd/protoc-gen-go@v1.36.6
go install google.golang.org/grpc/cmd/protoc-gen-go-grpc@v1.5.1
```

### Python and TypeScript generators

Both are vendored into their own output directories and installed by the
Makefile on demand:

- Python: `uv sync` in `python/` pulls `grpcio-tools`, pinned by `uv.lock`.
- TypeScript: `npm install` in `typescript/` pulls `ts-proto` and `typescript`.

## How each language is generated

Go and TypeScript go through `buf generate` with the templates
`../buf.gen.go.yaml` and `../buf.gen.ts.yaml`. Lint rules live in `../buf.yaml`.

Python does **not** go through buf: the gRPC Python plugin ships inside the
`grpcio-tools` wheel rather than as a standalone `protoc-gen-*` binary, so
`make python` invokes `python -m grpc_tools.protoc` directly. The output is
identical to what a buf-driven run would produce.

Two ts-proto options are load-bearing and should not be dropped:

- `useDate=true` maps `google.protobuf.Timestamp` to `Date`.
- `outputServices=generic-definitions` emits transport-agnostic service
  definitions, so the generated code does not depend on `@grpc/grpc-js` or
  `nice-grpc`. Pick a transport in the consuming package instead.

`typescript/tsconfig.json` includes both the `DOM` lib and `@types/node` because
ts-proto's base64 helpers reach for `globalThis.atob` and `globalThis.Buffer`.

## Verification

`make verify` is the P0 completion gate (design §9): generated code in all three
languages must compile.

| Target | Command | Passing output |
|---|---|---|
| `verify-go` | `go build ./... && go vet ./...` in `go/` | no output |
| `verify-python` | `uv run python -c "import amx_pb2, amx_pb2_grpc"` in `python/` | `amx_pb2 ok: amx.v1` |
| `verify-typescript` | `npm run typecheck` (`tsc --noEmit`) in `typescript/` | no output |

The JSON schemas are separate from this pipeline. `usage-report` and
`switch-event` both `$ref` into `common.schema.json`, so a validator needs all
three registered before it can resolve anything — `make verify-schemas` does
that:

```bash
make verify-schemas
```

Shared shapes in `common.schema.json` carry no `required` list; each consuming
schema states its own next to the `$ref`, because the same shape has different
obligations per report (a switch event names accounts by email alone, a usage
report always has the AMS id too).

## Changing the contract

The contract changes first, then the code (design §4, contract-first principle).

1. Edit `../proto/amx.proto` (and `../schemas/`, `../openapi.yaml` if the change
   reaches the report or REST surface).
2. `make lint` — `buf lint` uses the STANDARD rule set. The exceptions in
   `../buf.yaml` are deliberate and documented there; do not add more without a
   reason recorded alongside them.
3. `buf breaking --against '.git#branch=main,subdir=contracts'` before merging a
   change to an already-deployed field. Field numbers are wire identity: renumber
   nothing, and `reserved` anything you delete.
4. `make all && make verify`, then commit the regenerated output with the proto
   change in the same commit.
