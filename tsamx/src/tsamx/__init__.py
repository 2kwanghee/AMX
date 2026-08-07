"""Multi-account switcher for Claude Code."""

from importlib.metadata import version

__version__ = version("tsamx")

from tsamx.switcher import ClaudeAccountSwitcher

__all__ = ["ClaudeAccountSwitcher", "__version__"]
