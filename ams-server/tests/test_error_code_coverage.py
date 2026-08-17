"""Every domain error code AMS raises must have a Korean console message.

`krApiError` (ams-web/src/lib/api-client/client.ts) falls back to the upstream
English `detail` for any code missing from `KR_API_ERROR`, so a new
`conflict("assignment.foo", ...)` silently ships an English sentence to the
operator. Nothing else catches that: the server compiles, the console
type-checks, and the gap only shows up when someone hits the error for real.
31 of 39 codes had drifted that way before this test existed.

It reads source only — no DB, no running service — which is why it lives in the
suite rather than in scripts/ next to verify_metrics_presence.py: this repo has
no CI, so the pytest run is the only gate that actually executes.

The reverse direction is checked too. Korean left behind for a code no raiser
produces usually means the code was renamed and its message quietly stopped
applying.
"""

from __future__ import annotations

import pathlib
import re

import pytest

# tests/ -> ams-server/ -> repo root
ROOT = pathlib.Path(__file__).resolve().parents[2]
SERVER_APP = ROOT / "ams-server/app"
CLIENT_TS = ROOT / "ams-web/src/lib/api-client/client.ts"

# Helpers in app.core.errors that take a machine-readable code first. Keep in
# step with that module.
RAISERS = r"(?:conflict|bad_request|not_found|forbidden|unauthorized|api_error|ApiError)"
# The code literal often sits on the line after the opening paren, so the
# newline has to be allowed: a single-line pattern finds barely a third of them.
CODE_CALL = RAISERS + r'\s*\(\s*(?:\n\s*)?"([a-z_]+\.[a-z_]+)"'

# code -> why an operator never needs Korean for it. Empty by design: an
# exemption belongs here with a reason so it stays reviewable.
EXEMPT: dict[str, str] = {}

pytestmark = pytest.mark.skipif(
    not CLIENT_TS.exists(),
    reason="console source not in this checkout (server-only tree)",
)


def _raised() -> dict[str, str]:
    """Every raised code -> the first `path:line` that raises it."""
    found: dict[str, str] = {}
    for path in sorted(SERVER_APP.rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        for m in re.finditer(CODE_CALL, text):
            line = text[: m.start()].count("\n") + 1
            found.setdefault(m.group(1), f"{path.relative_to(ROOT)}:{line}")
    return found


def _localized() -> set[str]:
    text = CLIENT_TS.read_text(encoding="utf-8")
    block = re.search(r"const KR_API_ERROR[^=]*=\s*\{(.*?)\n\};", text, re.S)
    assert block, f"KR_API_ERROR not found in {CLIENT_TS.relative_to(ROOT)}"
    return set(re.findall(r"'([a-z_]+\.[a-z_]+)'\s*:", block.group(1)))


def test_every_raised_error_code_has_a_korean_console_message() -> None:
    raised = _raised()
    assert raised, "no error codes found — the raiser pattern probably went stale"
    missing = {c: w for c, w in raised.items() if c not in _localized() and c not in EXEMPT}
    assert not missing, (
        "these codes reach the operator as raw English detail; add a sentence "
        "carrying the next action to KR_API_ERROR in "
        f"{CLIENT_TS.relative_to(ROOT)} (or to EXEMPT here, with a reason):\n"
        + "\n".join(f"  {c}  {missing[c]}" for c in sorted(missing))
    )


def test_no_korean_message_is_left_for_a_code_nothing_raises() -> None:
    stale = sorted(_localized() - set(_raised()))
    assert not stale, (
        "KR_API_ERROR maps codes no raiser produces — if one was renamed, its "
        "Korean silently stopped applying:\n" + "\n".join(f"  {c}" for c in stale)
    )
