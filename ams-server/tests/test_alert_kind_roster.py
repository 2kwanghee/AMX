"""The alert-kind roster is hand-copied into four places. Force them to agree.

A new kind has to be added to `models.ALERT_KINDS`, to `schemas.AlertKind`, to the
`ck_alerts_kind` CHECK a migration rebuilds, and to the console twice — the
`AlertKind` union in `types.ts` and the `KR_LABEL` map in `common.tsx`. Nothing
ties them together, and each omission fails somewhere else entirely:

* missing from the CHECK -> the INSERT raises and the alert is dropped (the gRPC
  paths catch it opaquely, so the only trace is one log line),
* missing from `schemas.AlertKind` -> the *whole* alert list dies with a response
  validation 500 the moment one such row exists,
* missing from `KR_LABEL` -> the operator reads raw English snake_case,
* missing from the `types.ts` union -> the console type stops being a contract.

Six of the fourteen kinds had drifted out of `KR_LABEL` before PR #121 noticed.
Same shape of gate as `test_error_code_coverage.py`, and for the same reason:
this repo has no CI, so the pytest run is the only thing that actually executes.

The CHECK is read from the migrated database rather than parsed out of the
migration files — the constraint is rebuilt by whichever migration widened it
last (0004, 0011, 0012, 0014, 0019, 0022, 0023, 0026 so far) and each builds the
expression from computed strings, so the applied constraint is the only honest
answer.
"""

from __future__ import annotations

import pathlib
import re
import typing

import pytest
from sqlalchemy import text

from app import models, schemas

# tests/ -> ams-server/ -> repo root
ROOT = pathlib.Path(__file__).resolve().parents[2]
TYPES_TS = ROOT / "ams-web/src/lib/api-client/types.ts"
COMMON_TSX = ROOT / "ams-web/src/components/common.tsx"

needs_console = pytest.mark.skipif(
    not (TYPES_TS.exists() and COMMON_TSX.exists()),
    reason="console source not in this checkout (server-only tree)",
)


def _roster() -> set[str]:
    return set(models.ALERT_KINDS)


def _ts_union(path: pathlib.Path, name: str) -> set[str]:
    block = re.search(rf"export type {name} =(.*?);", path.read_text(encoding="utf-8"), re.S)
    assert block, f"{name} union not found in {path.relative_to(ROOT)}"
    return set(re.findall(r"'([a-z_]+)'", block.group(1)))


def _kr_label_keys() -> set[str]:
    text_ = COMMON_TSX.read_text(encoding="utf-8")
    block = re.search(r"const KR_LABEL[^=]*=\s*\{(.*?)\n\};", text_, re.S)
    assert block, f"KR_LABEL not found in {COMMON_TSX.relative_to(ROOT)}"
    return set(re.findall(r"^\s*([a-z_][a-z0-9_]*)\s*:", block.group(1), re.M))


def test_model_roster_has_no_duplicates() -> None:
    """ALERT_KINDS is a tuple, so a duplicated entry is silent and would make
    every other comparison here pass while the roster count lies."""
    assert len(models.ALERT_KINDS) == len(set(models.ALERT_KINDS))


def test_schemas_alert_kind_matches_model_roster() -> None:
    assert set(typing.get_args(schemas.AlertKind)) == _roster()


def test_migration_check_admits_exactly_the_model_roster(app_env, engine) -> None:
    with engine.connect() as conn:
        defn = conn.scalar(
            text(
                "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
                "WHERE conname = 'ck_alerts_kind'"
            )
        )
    assert defn, "ck_alerts_kind is not on the alerts table in the migrated schema"
    # PostgreSQL rewrites `kind IN (...)` as `kind = ANY (ARRAY['x'::text, ...])`.
    admitted = set(re.findall(r"'([a-z_]+)'", defn))
    assert admitted == _roster(), (
        "the applied ck_alerts_kind and models.ALERT_KINDS disagree; a kind the "
        "CHECK refuses makes its INSERT raise and the alert is dropped:\n"
        f"  only in ALERT_KINDS: {sorted(_roster() - admitted)}\n"
        f"  only in the CHECK:   {sorted(admitted - _roster())}"
    )


@needs_console
def test_console_alert_kind_union_matches_model_roster() -> None:
    union = _ts_union(TYPES_TS, "AlertKind")
    assert union == _roster(), (
        "the console AlertKind union and models.ALERT_KINDS disagree; add the kind "
        f"to {TYPES_TS.relative_to(ROOT)}:\n"
        f"  only in ALERT_KINDS: {sorted(_roster() - union)}\n"
        f"  only in the union:   {sorted(union - _roster())}"
    )


@needs_console
def test_every_alert_kind_has_a_korean_label() -> None:
    """KR_LABEL also carries statuses, states and providers, so this is a subset
    check — every kind must be in it, not the other way round."""
    missing = sorted(_roster() - _kr_label_keys())
    assert not missing, (
        "these kinds reach the operator as raw English snake_case in the 종류 "
        f"column; add a label to KR_LABEL in {COMMON_TSX.relative_to(ROOT)}:\n"
        + "\n".join(f"  {k}" for k in missing)
    )
