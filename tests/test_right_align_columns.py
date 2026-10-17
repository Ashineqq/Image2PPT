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

from layout.right_align import apply_right_aligned_columns  # noqa: E402


def _rec(name: str, text: str, x1: int, y1: int, x2: int, y2: int,
         align: str = "left") -> dict:
    return {
        "type": "text",
        "name": name,
        "text": text,
        "box": [x1, y1, x2 - x1, y2 - y1],
        "source_bbox": [x1, y1, x2, y2],
        "align": align,
    }


class RightAlignColumnTests(unittest.TestCase):
    def test_label_stack_snaps_to_shared_right_edge(self) -> None:
        recs = [
            _rec("t1", "零售电商", 40, 84, 91, 101),
            _rec("t2", "Retail &", 43, 98, 92, 115),
            _rec("t3", "金融业", 52, 144, 92, 161),
            _rec("t4", "Finance", 42, 156, 92, 176),
        ]
        out = apply_right_aligned_columns(recs)
        by_name = {r["name"]: r for r in out}
        for r in out:
            self.assertEqual(r["align"], "right")
        rights = {round(r["box"][0] + r["box"][2], 2) for r in out}
        self.assertEqual(len(rights), 1)

    def test_long_sentences_untouched(self) -> None:
        recs = [
            _rec("t1", "这是一段说明文字，带标点", 40, 84, 91, 101),
            _rec("t2", "这是另一段说明文字，带标点", 43, 120, 92, 140),
            _rec("t3", "第三段也是说明文字，带标点", 42, 160, 92, 180),
        ]
        out = apply_right_aligned_columns(recs)
        for r in out:
            self.assertEqual(r["align"], "left")

    def test_scattered_right_edges_untouched(self) -> None:
        recs = [
            _rec("t1", "标签一", 10, 84, 60, 101),
            _rec("t2", "标签二", 10, 120, 120, 140),
            _rec("t3", "标签三", 10, 160, 200, 180),
        ]
        out = apply_right_aligned_columns(recs)
        for r in out:
            self.assertEqual(r["align"], "left")


if __name__ == "__main__":
    unittest.main()
