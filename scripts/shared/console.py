"""Keep console output from dying on characters the local codec cannot encode.

A Windows pipe defaults to a legacy code page (GBK on zh-CN), so printing the
confidence-tier emoji raised UnicodeEncodeError and killed the whole run before
the deck was built. Reconfiguring the streams with errors="replace" degrades
such characters to "?" instead of failing.

This does not force a different encoding: a real UTF-8 terminal keeps its emoji,
and the web UI exports PYTHONIOENCODING=utf-8 so the job log shows them too.
"""
from __future__ import annotations

import sys


def use_safe_console() -> None:
    """Make stdout/stderr tolerant of characters the local codec lacks."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError, OSError):
            # Not a reconfigurable text stream (redirected, wrapped, closed).
            pass
