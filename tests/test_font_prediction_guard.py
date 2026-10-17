from __future__ import annotations

import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = REPO_ROOT / "scripts"
PAGE_DIR = SCRIPTS_DIR / "page"
for path in (SCRIPTS_DIR, PAGE_DIR):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from layout.font_predict import font_prediction_applies  # noqa: E402
from layout.grouping import unify_group_sizes  # noqa: E402


def _record(text: str, font: str, family: str | None,
            conf: float | None) -> dict:
    rec = {
        "text": text,
        "font": font,
        "color": "#222222",
        "box": [10, 10, 60, 22],
        "bold": False,
        "italic": False,
    }
    if family is not None:
        rec["font_pred"] = {
            "family": family,
            "family_confidence": conf,
            "bold_confidence": None,
            "italic_confidence": 0.0,
        }
    return rec


class FontPredictionGuardTests(unittest.TestCase):
    def test_monospace_rejected_on_prose(self) -> None:
        self.assertFalse(font_prediction_applies("E-commerce", "Consolas"))
        self.assertFalse(font_prediction_applies("Finance", "Consolas"))
        # Proportional Latin faces are fine on English prose; low-confidence
        # ones are still held back by the 0.60 threshold in text_emit.
        self.assertTrue(font_prediction_applies("Finance", "Tahoma"))

    def test_monospace_kept_for_code(self) -> None:
        self.assertTrue(font_prediction_applies("a[i];", "Consolas"))

    def test_latin_only_rejected_on_cjk(self) -> None:
        self.assertFalse(font_prediction_applies("零售电商", "Calibri"))
        self.assertTrue(font_prediction_applies("零售电商", "Microsoft YaHei"))

    def test_group_does_not_propagate_monospace(self) -> None:
        recs = [
            _record("E-commerce", "Microsoft YaHei", "Consolas", 0.77),
            _record("Finance", "Microsoft YaHei", None, None),
        ]
        out = unify_group_sizes(recs)
        self.assertEqual(out[0]["font"], "Microsoft YaHei")
        self.assertEqual(out[1]["font"], "Microsoft YaHei")


if __name__ == "__main__":
    unittest.main()
