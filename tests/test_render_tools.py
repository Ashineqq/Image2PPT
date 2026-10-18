"""Tests for the render-tool probe (no rendering, no network).

Calibration and previews both need LibreOffice + poppler. A missing install
used to surface as a mid-build SystemExit with its message swallowed, so the
probe now runs up front and the stages are skipped with a clear warning.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
VERIFY = ROOT / "scripts" / "verify"
if str(VERIFY) not in sys.path:
    sys.path.insert(0, str(VERIFY))

import render_preview  # noqa: E402


class FindToolTests(unittest.TestCase):
    def test_finds_a_tool_on_path(self) -> None:
        with mock.patch.object(render_preview.shutil, "which",
                               return_value="/usr/bin/soffice"):
            self.assertEqual(render_preview.find_tool("soffice"), "/usr/bin/soffice")

    def test_an_explicit_override_wins(self) -> None:
        self.assertEqual(
            render_preview.find_tool("soffice", "/opt/so/LibreOffice"), "/opt/so/LibreOffice")

    def test_macos_app_bundle_is_found_without_path(self) -> None:
        with mock.patch.object(render_preview.shutil, "which", return_value=None), \
             mock.patch.object(render_preview.Path, "exists", return_value=True):
            self.assertEqual(render_preview.find_tool("soffice"),
                             render_preview.MAC_SOFFICE)

    def test_reports_none_when_missing(self) -> None:
        with mock.patch.object(render_preview.shutil, "which", return_value=None), \
             mock.patch.object(render_preview.Path, "exists", return_value=False):
            self.assertIsNone(render_preview.find_tool("soffice"))

    def test_which_still_raises_for_a_missing_tool(self) -> None:
        with mock.patch.object(render_preview.shutil, "which", return_value=None), \
             mock.patch.object(render_preview.Path, "exists", return_value=False):
            with self.assertRaises(SystemExit):
                render_preview.which("pdftoppm", None)


class MissingToolsTests(unittest.TestCase):
    def test_nothing_missing_when_both_resolve(self) -> None:
        with mock.patch.object(render_preview.shutil, "which",
                               side_effect=lambda n: f"/usr/bin/{n}"):
            self.assertEqual(render_preview.missing_tools(), [])

    def test_lists_every_missing_tool(self) -> None:
        with mock.patch.object(render_preview.shutil, "which", return_value=None), \
             mock.patch.object(render_preview.Path, "exists", return_value=False):
            self.assertEqual(render_preview.missing_tools(),
                             ["soffice", "pdftoppm"])

    def test_partial_install_reports_only_what_is_missing(self) -> None:
        def fake_which(name):
            return "/usr/bin/soffice" if name == "soffice" else None
        with mock.patch.object(render_preview.shutil, "which",
                               side_effect=fake_which), \
             mock.patch.object(render_preview.Path, "exists", return_value=False):
            self.assertEqual(render_preview.missing_tools(), ["pdftoppm"])


if __name__ == "__main__":
    unittest.main()
