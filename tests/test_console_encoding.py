"""Tests for the pipeline's text-I/O boundary (no network, no real runs).

Both halves of this broke on Windows, where a pipe defaults to GBK:
printing an emoji killed the child, and a parent that captured a UTF-8
child with `text=True` decoded it with the locale codec and lost stdout.
"""
from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from shared.console import run_capture, utf8_env, use_safe_console  # noqa: E402


class RunCaptureTests(unittest.TestCase):
    def test_round_trips_non_ascii_when_the_parent_locale_is_not_utf8(self) -> None:
        """The child is pinned to UTF-8 even when the parent env says GBK,
        and the parent decodes it as UTF-8 — so both ends agree."""
        with mock.patch.dict(os.environ, {"PYTHONIOENCODING": "gbk"}):
            r = run_capture([sys.executable, "-c",
                             "print('页面 03: 中文正常 🟢')"])
        self.assertIn("页面 03: 中文正常", r.stdout)
        self.assertIn("\U0001f7e2", r.stdout)

    def test_undecodable_bytes_do_not_raise(self) -> None:
        """A stray non-UTF-8 byte degrades instead of killing the reader
        thread, which is what left `stdout` as None."""
        code = ("import sys; sys.stdout.buffer.write(b'\\xff\\xfe ok')")
        r = run_capture([sys.executable, "-c", code])
        self.assertIsInstance(r.stdout, str)
        self.assertIn("ok", r.stdout)

    def test_captures_by_default_and_passes_check_through(self) -> None:
        r = run_capture([sys.executable, "-c", "print('x')"], check=True)
        self.assertEqual(r.returncode, 0)
        self.assertEqual(r.stdout.strip(), "x")

    def test_nonzero_exit_is_reported_not_swallowed(self) -> None:
        with self.assertRaises(Exception):
            run_capture([sys.executable, "-c", "raise SystemExit(3)"],
                        check=True)


class EnvTests(unittest.TestCase):
    def test_utf8_env_pins_the_child_stdio_encoding(self) -> None:
        env = utf8_env()
        self.assertEqual(env["PYTHONIOENCODING"], "utf-8")
        # The rest of the environment must survive: the pipeline relies on
        # PADDLE_OCR_TOKEN and friends reaching the children.
        self.assertEqual(env.get("PATH"), os.environ.get("PATH"))


class SafeConsoleTests(unittest.TestCase):
    def test_is_idempotent_and_tolerates_unreconfigurable_streams(self) -> None:
        use_safe_console()
        use_safe_console()
        with mock.patch.object(sys, "stdout", object()):
            use_safe_console()   # must not raise on a non-stream object


if __name__ == "__main__":
    unittest.main()
