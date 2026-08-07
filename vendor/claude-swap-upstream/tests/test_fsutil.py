"""Tests for claude_swap.fsutil filesystem primitives."""

from __future__ import annotations

import os

import pytest

from claude_swap.fsutil import replace_with_retry


class TestReplaceWithRetry:
    """os.replace is not reliably available on Windows: antivirus and the
    search indexer open freshly-written files opportunistically, so a replace
    onto a just-created target fails with ERROR_ACCESS_DENIED/SHARING_VIOLATION
    for a few milliseconds. Measured at ~44% of replaces on a Defender-scanned
    temp dir, which silently broke credential and usage-store writes.
    """

    def _win_oserror(self, winerror: int) -> OSError:
        e = OSError(13, "Access is denied")
        e.winerror = winerror
        return e

    @pytest.mark.parametrize("winerror", [5, 32, 33])
    def test_retries_transient_windows_errors(self, tmp_path, monkeypatch, winerror):
        monkeypatch.setattr("claude_swap.fsutil.sys.platform", "win32")
        calls = []
        real_replace = os.replace

        def flaky(src, dst):
            calls.append(1)
            if len(calls) < 4:
                raise self._win_oserror(winerror)
            return real_replace(src, dst)

        monkeypatch.setattr("claude_swap.fsutil.os.replace", flaky)
        src = tmp_path / "tmp.tmp"
        src.write_text("payload")
        dst = tmp_path / "target.json"

        replace_with_retry(src, dst)

        assert len(calls) == 4
        assert dst.read_text() == "payload"

    def test_gives_up_and_raises_after_attempts(self, tmp_path, monkeypatch):
        monkeypatch.setattr("claude_swap.fsutil.sys.platform", "win32")

        def always_fail(src, dst):
            raise self._win_oserror(5)

        monkeypatch.setattr("claude_swap.fsutil.os.replace", always_fail)
        with pytest.raises(OSError):
            replace_with_retry(tmp_path / "a", tmp_path / "b", attempts=3)

    def test_does_not_retry_real_errors(self, tmp_path, monkeypatch):
        """A genuine failure (e.g. missing source) must surface immediately."""
        monkeypatch.setattr("claude_swap.fsutil.sys.platform", "win32")
        calls = []

        def missing(src, dst):
            calls.append(1)
            raise self._win_oserror(2)  # ERROR_FILE_NOT_FOUND

        monkeypatch.setattr("claude_swap.fsutil.os.replace", missing)
        with pytest.raises(OSError):
            replace_with_retry(tmp_path / "a", tmp_path / "b")
        assert len(calls) == 1

    def test_posix_never_retries(self, tmp_path, monkeypatch):
        monkeypatch.setattr("claude_swap.fsutil.sys.platform", "linux")
        calls = []

        def fail(src, dst):
            calls.append(1)
            raise self._win_oserror(5)

        monkeypatch.setattr("claude_swap.fsutil.os.replace", fail)
        with pytest.raises(OSError):
            replace_with_retry(tmp_path / "a", tmp_path / "b")
        assert len(calls) == 1

    def test_rejects_nonpositive_attempts(self, tmp_path):
        """attempts < 1 would silently skip the replace; refuse it instead."""
        src = tmp_path / "tmp.tmp"
        src.write_text("payload")
        with pytest.raises(ValueError):
            replace_with_retry(src, tmp_path / "target.json", attempts=0)
        assert src.exists()
