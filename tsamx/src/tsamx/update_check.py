"""Check PyPI for newer versions of tsamx."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import urllib.request
from pathlib import Path

from tsamx.cache import CACHE_DIR, MISSING, read_cache, write_cache

CACHE_PATH = CACHE_DIR / "update_check.json"
CACHE_TTL = 24 * 3600  # 24 hours
# tsamx is not published on PyPI; the upstream claude-swap update check is
# disabled (UPDATE_CHECK_ENABLED = False) until a tsamx package exists.
PYPI_URL = "https://pypi.org/pypi/tsamx/json"
UPDATE_CHECK_ENABLED = False


def _parse_version(v: str) -> tuple[int, ...]:
    return tuple(int(x) for x in v.split("."))


def _detect_install_method() -> str | None:
    """Return 'uv', 'pipx', or None if we can't tell."""
    prefix = Path(sys.prefix)
    parts = tuple(p.lower() for p in prefix.parts)
    pairs = list(zip(parts, parts[1:]))

    if ("uv", "tools") in pairs:
        return "uv"
    if ("pipx", "venvs") in pairs:
        return "pipx"

    # Env-var override: only trust if sys.prefix is actually under it.
    for env_var, name in (("UV_TOOL_DIR", "uv"), ("PIPX_HOME", "pipx")):
        root = os.environ.get(env_var)
        if root:
            try:
                if prefix.is_relative_to(Path(root)):
                    return name
            except (ValueError, OSError):
                pass
    return None


def check_for_update(current_version: str) -> str | None:
    """Return a notification string if a newer version exists, else None."""
    if not UPDATE_CHECK_ENABLED:
        return None
    try:
        latest_version = None

        # Try reading cache
        cached_data = read_cache(CACHE_PATH, CACHE_TTL)
        if cached_data is not MISSING:
            latest_version = cached_data
        else:
            # Fetch from PyPI
            try:
                req = urllib.request.Request(PYPI_URL)
                with urllib.request.urlopen(req, timeout=2) as resp:
                    data = json.loads(resp.read().decode())
                latest_version = data["info"]["version"]
            except Exception:
                latest_version = None

            # Write cache regardless of success/failure
            write_cache(CACHE_PATH, latest_version)

        if latest_version and _parse_version(latest_version) > _parse_version(current_version):
            method = _detect_install_method()
            direct = {
                "uv": "uv tool upgrade tsamx",
                "pipx": "pipx upgrade tsamx",
            }.get(method or "")
            if direct and sys.platform != "win32":
                # tsamx upgrade actually performs the upgrade here.
                hint = "Run `tsamx upgrade` to update."
            elif direct:
                # Windows: tsamx upgrade only prints, so point at the real command.
                hint = f"Run `{direct}` to update."
            else:
                # Unknown install method: tsamx upgrade shows manual instructions.
                hint = "Run `tsamx upgrade` for upgrade instructions."
            return (
                f"A newer version of tsamx is available ({latest_version}). "
                f"You are using {current_version}. {hint}"
            )
        return None
    except Exception:
        return None


def run_self_upgrade() -> int:
    """Run the appropriate upgrade command for the current install method.

    Returns the subprocess exit code, or 1 if detection failed or the package
    manager is missing from PATH.
    """
    from tsamx.printer import accent, error

    method = _detect_install_method()
    commands = {
        "uv": ["uv", "tool", "upgrade", "tsamx"],
        "pipx": ["pipx", "upgrade", "tsamx"],
    }
    cmd = commands.get(method or "")
    if cmd is None:
        error(
            "Could not detect install method (looked for uv tool / pipx).\n"
            f"  sys.prefix:     {sys.prefix}\n"
            f"  sys.executable: {sys.executable}\n"
            "To upgrade manually, run one of:\n"
            "  uv tool upgrade tsamx\n"
            "  pipx upgrade tsamx\n"
            f"  {sys.executable} -m pip install --upgrade tsamx\n"
            "If you installed with `pip install -e .`, use `git pull` instead."
        )
        return 1

    # Windows: the running tsamx.exe launcher is locked, so an in-process
    # uv/pipx upgrade fails when it tries to replace the executable even
    # though the package itself updates. tsamx exits right after this, which
    # releases the lock, so the user can just run the command themselves.
    if sys.platform == "win32":
        print(f"To upgrade tsamx on Windows, run:\n  {accent(' '.join(cmd))}")
        return 1

    try:
        result = subprocess.run(cmd, check=False)
        return result.returncode
    except FileNotFoundError:
        error(
            f"Detected {method} install but `{cmd[0]}` is not on PATH. "
            "Run the upgrade manually from a shell where it is available."
        )
        return 1
