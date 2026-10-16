"""Unit tests for the cloud OCR backend (no network access).

The remote client's HTTP half is exercised by real runs; what is pinned
here is the half that decides whether downstream can trust a file —
coordinate scaling, the words/word_boxes alignment guard, and token
resolution order.
"""
from __future__ import annotations

import unittest
from pathlib import Path
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
OCR_DIR = ROOT / "scripts" / "ocr"
if str(OCR_DIR) not in sys.path:
    sys.path.insert(0, str(OCR_DIR))

import ocr_remote  # noqa: E402


def pruned(texts, boxes, scores=None, words=None, word_boxes=None):
    payload = {
        "rec_texts": texts,
        "rec_boxes": boxes,
        "rec_scores": scores if scores is not None else [1.0] * len(texts),
    }
    if words is not None:
        payload["text_word"] = words
        payload["text_word_boxes"] = word_boxes
    return payload


class ExtractItemsTests(unittest.TestCase):
    def test_maps_rows_to_the_local_item_contract(self) -> None:
        items = ocr_remote.extract_items(
            pruned(["标题", "正文"], [[10, 20, 110, 50], [10, 60, 210, 90]]),
            min_conf=0.5,
        )
        self.assertEqual(
            [i["text"] for i in items], ["标题", "正文"])
        self.assertEqual(
            (items[0]["x1"], items[0]["y1"], items[0]["x2"], items[0]["y2"]),
            (10, 20, 110, 50))
        self.assertEqual(items[0]["confidence"], 1.0)

    def test_drops_low_confidence_and_blank_rows(self) -> None:
        items = ocr_remote.extract_items(
            pruned(["keep", "drop", "  "], [[0, 0, 10, 10]] * 3,
                   scores=[0.9, 0.2, 0.99]),
            min_conf=0.5,
        )
        self.assertEqual([i["text"] for i in items], ["keep"])

    def test_sorts_top_to_bottom(self) -> None:
        items = ocr_remote.extract_items(
            pruned(["low", "high"],
                   [[0, 90, 10, 100], [0, 10, 10, 20]]),
            min_conf=0.0,
        )
        self.assertEqual([i["text"] for i in items], ["high", "low"])

    def test_scales_ocr_space_onto_source_pixels(self) -> None:
        """A PDF upload comes back at the service's own render scale, so
        every box has to be mapped back onto the source grid."""
        items = ocr_remote.extract_items(
            pruned(["x"], [[100, 200, 300, 400]]),
            min_conf=0.0, scale_x=0.5, scale_y=0.5,
        )
        self.assertEqual(
            (items[0]["x1"], items[0]["y1"], items[0]["x2"], items[0]["y2"]),
            (50, 100, 150, 200))

    def test_clips_to_source_bounds_and_drops_boxes_outside(self) -> None:
        items = ocr_remote.extract_items(
            pruned(["edge", "outside"],
                   [[-5, -5, 200, 200], [400, 400, 500, 500]]),
            min_conf=0.0, bounds=(100, 100),
        )
        self.assertEqual([i["text"] for i in items], ["edge"])
        self.assertEqual(
            (items[0]["x1"], items[0]["y1"], items[0]["x2"], items[0]["y2"]),
            (0, 0, 100, 100))

    def test_attaches_word_boxes_when_they_rebuild_the_line(self) -> None:
        items = ocr_remote.extract_items(
            pruned(["63个"], [[0, 0, 60, 20]],
                   words=[["63", "个"]],
                   word_boxes=[[[0, 0, 40, 20], [40, 0, 60, 20]]]),
            min_conf=0.0,
        )
        self.assertEqual(items[0]["words"], ["63", "个"])
        self.assertEqual(items[0]["word_boxes"],
                         [[0, 0, 40, 20], [40, 0, 60, 20]])
        # Not every word is a single character, so no per-char view.
        self.assertNotIn("chars", items[0])

    def test_emits_char_boxes_for_pure_cjk_lines(self) -> None:
        items = ocr_remote.extract_items(
            pruned(["中文"], [[0, 0, 40, 20]],
                   words=[["中", "文"]],
                   word_boxes=[[[0, 0, 20, 20], [20, 0, 40, 20]]]),
            min_conf=0.0,
        )
        self.assertEqual(items[0]["chars"], ["中", "文"])
        self.assertEqual(items[0]["char_boxes"],
                         [[0, 0, 20, 20], [20, 0, 40, 20]])

    def test_ignores_word_boxes_that_do_not_rebuild_the_line(self) -> None:
        """Unaligned words would corrupt word-aligned reflow, so they are
        dropped rather than passed through."""
        items = ocr_remote.extract_items(
            pruned(["63个"], [[0, 0, 60, 20]],
                   words=[["63", "只"]],
                   word_boxes=[[[0, 0, 40, 20], [40, 0, 60, 20]]]),
            min_conf=0.0,
        )
        self.assertNotIn("words", items[0])

    def test_scales_word_boxes_with_the_line_box(self) -> None:
        items = ocr_remote.extract_items(
            pruned(["中文"], [[0, 0, 80, 40]],
                   words=[["中", "文"]],
                   word_boxes=[[[0, 0, 40, 40], [40, 0, 80, 40]]]),
            min_conf=0.0, scale_x=0.5, scale_y=0.5,
        )
        self.assertEqual(items[0]["word_boxes"],
                         [[0, 0, 20, 20], [20, 0, 40, 20]])


class ResponseShapeTests(unittest.TestCase):
    def test_ocr_results_rejects_a_layout_only_payload(self) -> None:
        """PaddleOCR-VL answers with layout blocks; consuming that as if
        it were line-level OCR would silently coarsen every text box."""
        with self.assertRaises(ocr_remote.RemoteOCRError):
            ocr_remote.ocr_results({"result": {"layoutParsingResults": []}})

    def test_page_dims_prefers_per_page_sizes(self) -> None:
        data = {"width": 100, "height": 50,
                "pages": [{"width": 200, "height": 100},
                          {"width": 400, "height": 200}]}
        self.assertEqual(ocr_remote._page_dims(data, 0), (200.0, 100.0))
        self.assertEqual(ocr_remote._page_dims(data, 1), (400.0, 200.0))

    def test_page_dims_falls_back_to_document_size(self) -> None:
        self.assertEqual(
            ocr_remote._page_dims({"width": 100, "height": 50}, 3),
            (100.0, 50.0))


class TokenTests(unittest.TestCase):
    def test_explicit_token_wins(self) -> None:
        self.assertEqual(ocr_remote.resolve_token("explicit"), "explicit")

    def test_falls_back_to_the_editppt_config_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Path(tmp) / "config.yaml"
            cfg.write_text(
                "OPENAI_API_KEY: <unset>\n"
                "PADDLE_OCR_TOKEN: from-config\n",
                encoding="utf-8",
            )
            self.assertEqual(ocr_remote.read_config_token(cfg), "from-config")

    def test_missing_config_is_not_an_error(self) -> None:
        self.assertEqual(
            ocr_remote.read_config_token(Path("/nonexistent/config.yaml")),
            "")


if __name__ == "__main__":
    unittest.main()
