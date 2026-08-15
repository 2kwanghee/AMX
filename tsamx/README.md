# tsamx

AMX account switcher for Claude Code. Fork of
[claude-swap](https://github.com/realiti4/claude-swap) (MIT, v0.25.0b1) — see NOTICE.md.

The authoritative usage, internalization, and private-repo install/authentication
guide is **`docs/TSAMX-GUIDE.md`**. This file is only a stub so the packaging
metadata (`pyproject.toml` `readme`) has a target.

```bash
uv tool install --from /path/to/tsamx tsamx
tsamx            # interactive TUI
tsamx list       # list saved accounts
tsamx add        # capture the current Claude Code account into a slot
tsamx <name>     # switch to a saved account
tsamx --help     # full command reference
```
