#!/usr/bin/env python
"""Fontconfig helpers for local Office fonts used by LibreOffice/Pillow."""

from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path


_FONTCONFIG_ENV: dict[str, str] | None = None

# Fontconfig configs to hang our override off, most specific first.
_SYSTEM_FONTCONFIG = (
    Path("/opt/homebrew/etc/fonts/fonts.conf"),
    Path("/usr/local/etc/fonts/fonts.conf"),
    Path("/etc/fonts/fonts.conf"),
)


def _office_yahei_dirs() -> list[Path]:
    roots = [
        Path("/Applications/Microsoft PowerPoint.app/Contents/Resources/DFonts"),
        Path("/Applications/Microsoft Word.app/Contents/Resources/DFonts"),
        Path("/Applications/Microsoft Excel.app/Contents/Resources/DFonts"),
        Path("/Applications/Microsoft OneNote.app/Contents/Resources/DFonts"),
    ]
    seen: set[Path] = set()
    out: list[Path] = []
    for root in roots:
        if root in seen:
            continue
        if (root / "msyh.ttc").exists():
            out.append(root)
            seen.add(root)
    return out


def _include_target(base: dict[str, str]) -> Path | None:
    """Which fontconfig to include: the caller's own if it already set
    one, else this platform's system config."""
    existing = base.get("FONTCONFIG_FILE")
    if existing and Path(existing).exists():
        return Path(existing)
    for candidate in _SYSTEM_FONTCONFIG:
        if candidate.exists():
            return candidate
    return None


def fontconfig_env(base: dict[str, str] | None = None) -> dict[str, str]:
    """Return an env that gives fontconfig the fonts LibreOffice cannot
    find on its own.

    Two reasons this exists:

    * Office's YaHei TTCs live inside the app bundle and are invisible to
      fontconfig unless their directory is added.
    * LibreOffice only falls back through fontconfig when it is told to.
      Pointing FONTCONFIG_FILE at a config that includes the system one
      is what keeps CJK text from rendering as tofu on machines where
      those system fonts are the only CJK faces available. Skipping this
      when Office is absent was the old behaviour, and it turned every
      CJK run into empty boxes.
    """
    global _FONTCONFIG_ENV
    env = dict(base or os.environ)
    if _FONTCONFIG_ENV is None:
        include = _include_target(env)
        dirs = _office_yahei_dirs()
        if include is None and not dirs:
            # Nothing to point fontconfig at; leave the environment alone.
            _FONTCONFIG_ENV = {}
        else:
            conf_path = Path(tempfile.gettempdir()) / "image2ppt-fonts.conf"
            parts = ["<?xml version=\"1.0\"?>",
                     "<!DOCTYPE fontconfig SYSTEM \"fonts.dtd\">",
                     "<fontconfig>"]
            if include is not None:
                parts.append(f"  <include ignore_missing=\"yes\">{include}"
                             f"</include>")
            parts.extend(f"  <dir>{d}</dir>" for d in dirs)
            parts.append("</fontconfig>")
            conf_path.write_text("\n".join(parts) + "\n", encoding="utf-8")
            _FONTCONFIG_ENV = {"FONTCONFIG_FILE": str(conf_path)}
    env.update(_FONTCONFIG_ENV)
    return env


def fontconfig_font_path(query: str) -> str | None:
    try:
        result = subprocess.run(
            ["fc-match", "-f", "%{file}", query],
            check=True,
            capture_output=True,
            text=True,
            env=fontconfig_env(),
        )
    except (OSError, subprocess.CalledProcessError):
        return None
    path = Path(result.stdout.strip())
    return str(path) if path.exists() else None
