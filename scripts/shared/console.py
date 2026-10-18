"""Keep the pipeline's text I/O safe on a non-UTF-8 platform.

Two failure modes drive this, both seen on Windows where a pipe defaults to
GBK rather than UTF-8:

* writing — printing a character the codec lacks raised UnicodeEncodeError
  and killed a run that had already finished OCR (the tier emoji);
* reading — a parent that captured a child's stdout with
  ``text=True`` decoded it with the locale codec, so once the child was told
  to emit UTF-8 the reader thread died with UnicodeDecodeError and left
  ``stdout`` as None.

``use_safe_console`` fixes the writing half. ``run_capture`` fixes the
reading half: pipeline children are Python scripts, so it pins both ends to
UTF-8 (PYTHONIOENCODING for the child, an explicit codec here) and degrades
undecodable bytes instead of raising.
"""
from __future__ import annotations

import os
import subprocess
import sys


def use_safe_console() -> None:
    """Make stdout/stderr tolerant of characters the local codec lacks."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError, OSError):
            # Not a reconfigurable text stream (redirected, wrapped, closed).
            pass


def utf8_env() -> dict[str, str]:
    """Environment for a pipeline child: its stdio must be UTF-8."""
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def run_capture(cmd: list[str], **kwargs) -> subprocess.CompletedProcess:
    """`subprocess.run` for a pipeline child, with UTF-8 on both ends.

    Use this instead of ``capture_output=True, text=True``: the locale codec
    that ``text=True`` implies is what broke on Windows. Anything the child
    writes that is not valid UTF-8 is replaced rather than fatal.
    """
    kwargs.setdefault("capture_output", True)
    kwargs.setdefault("encoding", "utf-8")
    kwargs.setdefault("errors", "replace")
    kwargs.setdefault("env", utf8_env())
    return subprocess.run(cmd, **kwargs)
