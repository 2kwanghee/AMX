# AMX contract code generation.
#
# Regenerates go/, python/ and typescript/ from ../proto/amx.proto.
# Generated sources are committed (design SSOT §4) so that consumers do not need
# the toolchain; run `make verify` after regenerating.
#
# Toolchain setup is documented in README.md. Everything installs under $HOME —
# no root required.

SHELL := /bin/bash
.SHELLFLAGS := -eu -o pipefail -c

GEN_DIR   := $(patsubst %/,%,$(dir $(abspath $(lastword $(MAKEFILE_LIST)))))
CONTRACTS := $(abspath $(GEN_DIR)/..)
PROTO_DIR := $(CONTRACTS)/proto
PROTO     := $(PROTO_DIR)/amx.proto

GO_ROOT_DIR   ?= $(HOME)/go-toolchain/go
export PATH   := $(HOME)/.local/bin:$(GO_ROOT_DIR)/bin:$(HOME)/go/bin:$(PATH)

BUF ?= buf
UV  ?= uv
NPM ?= npm

GO_OUT     := $(GEN_DIR)/go
PY_OUT     := $(GEN_DIR)/python
TS_OUT     := $(GEN_DIR)/typescript/src

.PHONY: all lint go python typescript verify verify-go verify-python verify-typescript verify-schemas clean tools-check

all: lint go python typescript ## Regenerate every language

## ----------------------------------------------------------------- generation

lint: ## Lint the proto (buf STANDARD, see ../buf.yaml)
	cd $(CONTRACTS) && $(BUF) lint

# Go and TypeScript go through buf; the ts_proto plugin is resolved from the
# typescript workspace's node_modules, so `npm install` must have run.
go: ## Generate Go (protoc-gen-go + protoc-gen-go-grpc)
	rm -f $(GO_OUT)/*.pb.go $(GO_OUT)/*_grpc.pb.go
	cd $(CONTRACTS) && $(BUF) generate --template buf.gen.go.yaml

typescript: $(GEN_DIR)/typescript/node_modules ## Generate TypeScript (ts-proto)
	rm -rf $(TS_OUT)
	cd $(CONTRACTS) && $(BUF) generate --template buf.gen.ts.yaml

# grpcio-tools carries the Python gRPC plugin inside its wheel, so Python is
# generated with protoc directly instead of through buf.
python: ## Generate Python (grpc_tools.protoc, stubs + .pyi)
	rm -f $(PY_OUT)/amx_pb2.py $(PY_OUT)/amx_pb2.pyi $(PY_OUT)/amx_pb2_grpc.py
	cd $(PY_OUT) && $(UV) run python -m grpc_tools.protoc \
		--proto_path=$(PROTO_DIR) \
		--python_out=. --pyi_out=. --grpc_python_out=. \
		$(PROTO)

$(GEN_DIR)/typescript/node_modules: $(GEN_DIR)/typescript/package.json
	cd $(GEN_DIR)/typescript && $(NPM) install
	@touch $@

## --------------------------------------------------------------- verification

verify: verify-breaking verify-go verify-python verify-typescript verify-schemas ## Breaking-change check + compile-check all three languages + the JSON schemas

# Wire compatibility against the last contract merged to origin/main. Needs the
# origin/main ref locally — `git fetch origin main` first if it is stale.
verify-breaking:
	cd $(CONTRACTS) && $(BUF) breaking --against "$(CONTRACTS)/../.git#branch=origin/main,subdir=contracts"

verify-go:
	cd $(GO_OUT) && GOFLAGS=-mod=mod go build ./... && go vet ./...

verify-python:
	cd $(PY_OUT) && $(UV) run python -c "import amx_pb2, amx_pb2_grpc; print('amx_pb2 ok:', amx_pb2.DESCRIPTOR.package)"

verify-typescript: $(GEN_DIR)/typescript/node_modules
	cd $(GEN_DIR)/typescript && $(NPM) run --silent typecheck

# The report schemas are hand-written, not generated. This checks they are valid
# draft 2020-12, that the cross-file $refs into common.schema.json resolve, and
# that the §6.5 sample payloads still validate.
verify-schemas:
	cd $(GEN_DIR) && $(UV) run --no-project --with jsonschema python validate_schemas.py

## -------------------------------------------------------------------- utility

tools-check: ## Report which generators are on PATH
	@command -v $(BUF) >/dev/null && $(BUF) --version || echo "buf: MISSING"
	@command -v go >/dev/null && go version || echo "go: MISSING"
	@command -v protoc-gen-go >/dev/null && echo "protoc-gen-go: ok" || echo "protoc-gen-go: MISSING"
	@command -v protoc-gen-go-grpc >/dev/null && echo "protoc-gen-go-grpc: ok" || echo "protoc-gen-go-grpc: MISSING"
	@command -v $(UV) >/dev/null && $(UV) --version || echo "uv: MISSING"
	@command -v $(NPM) >/dev/null && echo "npm: $$($(NPM) --version)" || echo "npm: MISSING"

clean:
	rm -f $(GO_OUT)/*.pb.go $(GO_OUT)/*_grpc.pb.go
	rm -f $(PY_OUT)/amx_pb2.py $(PY_OUT)/amx_pb2.pyi $(PY_OUT)/amx_pb2_grpc.py
	rm -rf $(TS_OUT)
