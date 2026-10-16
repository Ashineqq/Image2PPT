"""Unit tests for the cloud table backend (no network access).

The half worth pinning is the one that decides what lands in the deck:
the HTML grid walk (merges share one box with the local model, so the
placement algorithm has to follow the HTML table model), and the mapping
of service coordinates back onto the source pixel grid.
"""
from __future__ import annotations

import unittest
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
for sub in ("scripts", "scripts/tables", "scripts/ocr"):
    target = ROOT / sub
    if str(target) not in sys.path:
        sys.path.insert(0, str(target))

import table_remote  # noqa: E402


def html(*rows: str) -> str:
    body = "".join(f"<tr>{r}</tr>" for r in rows)
    return f"<html><body><table>{body}</table></body></html>"


class ParseHtmlGridTests(unittest.TestCase):
    def test_reads_a_plain_grid_in_document_order(self) -> None:
        cells = table_remote.parse_html_grid(html(
            "<td>a</td><td>b</td>",
            "<td>c</td><td>d</td>",
        ))
        self.assertEqual(
            [(c["row"], c["col"], c["text"]) for c in cells],
            [(0, 0, "a"), (0, 1, "b"), (1, 0, "c"), (1, 1, "d")])

    def test_colspan_advances_the_column_cursor(self) -> None:
        cells = table_remote.parse_html_grid(html(
            '<td colspan="2">head</td>',
            "<td>a</td><td>b</td>",
        ))
        self.assertEqual((cells[0]["row"], cells[0]["col"],
                          cells[0]["colspan"]), (0, 0, 2))
        self.assertEqual((cells[1]["col"], cells[2]["col"]), (0, 1))

    def test_rowspan_keeps_its_column_occupied_below(self) -> None:
        """The whole point of the occupancy grid: the cell after a
        rowspan must not be placed on top of the spanned column."""
        cells = table_remote.parse_html_grid(html(
            '<td rowspan="2">side</td><td>a</td>',
            "<td>b</td>",
        ))
        self.assertEqual(
            [(c["row"], c["col"], c["text"]) for c in cells],
            [(0, 0, "side"), (0, 1, "a"), (1, 1, "b")])

    def test_combined_span(self) -> None:
        cells = table_remote.parse_html_grid(html(
            '<td colspan="2" rowspan="2">block</td><td>c</td>',
            "<td>d</td>",
        ))
        self.assertEqual(
            [(c["row"], c["col"], c["rowspan"], c["colspan"], c["text"])
             for c in cells],
            [(0, 0, 2, 2, "block"), (0, 2, 1, 1, "c"), (1, 2, 1, 1, "d")])

    def test_strips_markup_and_decodes_entities(self) -> None:
        cells = table_remote.parse_html_grid(html(
            "<td><span>a&amp;b</span></td><td>c<br/>d</td>",
        ))
        self.assertEqual(cells[0]["text"], "a&b")
        self.assertEqual(cells[1]["text"], "c\nd")

    def test_accepts_th_cells(self) -> None:
        cells = table_remote.parse_html_grid(html("<th>head</th><td>x</td>"))
        self.assertEqual([c["text"] for c in cells], ["head", "x"])

    def test_empty_html_yields_no_cells(self) -> None:
        self.assertEqual(table_remote.parse_html_grid(""), [])


def entry(block_bbox, cell_boxes, pred_html, score=0.9):
    return {
        "block": {"block_label": "table", "block_bbox": list(block_bbox)},
        "structure": {"pred_html": pred_html, "cell_box_list": cell_boxes},
        "score": score,
    }


class BuildTableTests(unittest.TestCase):
    def test_scales_cells_and_table_box_onto_source_pixels(self) -> None:
        """A page uploaded as an image comes back at source scale, but the
        PDF path does not — both go through the same scaling."""
        built = table_remote.build_table(
            entry([0, 0, 200, 100], [[0, 0, 100, 50], [100, 0, 200, 50]],
                  html("<td>a</td><td>b</td>")),
            image_size=(100, 50), ocr_scale=(0.5, 0.5), ocr_data=None,
        )
        self.assertEqual(built["bbox"], [0, 0, 100, 50])
        self.assertEqual(built["cells"][1]["bbox"], [50, 0, 100, 25])

    def test_cell_text_comes_from_the_service_html(self) -> None:
        built = table_remote.build_table(
            entry([0, 0, 10, 10], [[0, 0, 5, 5]], html("<td>识别结果</td>")),
            image_size=(10, 10), ocr_scale=(1.0, 1.0), ocr_data=None,
        )
        self.assertEqual(built["cells"][0]["text"], "识别结果")

    def test_falls_back_to_ocr_when_the_service_left_a_cell_empty(self) -> None:
        built = table_remote.build_table(
            entry([0, 0, 100, 100], [[0, 0, 100, 50]], html("<td></td>")),
            image_size=(100, 100), ocr_scale=(1.0, 1.0),
            ocr_data=[{"text": "补上", "x1": 10, "y1": 10,
                       "x2": 40, "y2": 30}],
        )
        self.assertEqual(built["cells"][0]["text"], "补上")

    def test_ocr_fallback_ignores_items_outside_the_cell(self) -> None:
        built = table_remote.build_table(
            entry([0, 0, 100, 100], [[0, 0, 100, 50]], html("<td></td>")),
            image_size=(100, 100), ocr_scale=(1.0, 1.0),
            ocr_data=[{"text": "外面", "x1": 10, "y1": 80,
                       "x2": 40, "y2": 95}],
        )
        self.assertEqual(built["cells"][0]["text"], "")

    def test_reports_the_declared_grid_from_spans(self) -> None:
        built = table_remote.build_table(
            entry([0, 0, 100, 100],
                  [[0, 0, 100, 50], [50, 0, 100, 50], [0, 50, 100, 100]],
                  html('<td colspan="2">h</td><td>x</td>',
                       "<td>a</td><td>b</td><td>c</td>")),
            image_size=(100, 100), ocr_scale=(1.0, 1.0), ocr_data=None,
        )
        self.assertEqual((built["rows"], built["cols"]), (2, 3))

    def test_a_colspan_row_alone_is_one_row_wide(self) -> None:
        built = table_remote.build_table(
            entry([0, 0, 100, 50], [[0, 0, 100, 50]],
                  html('<td colspan="3">banner</td>')),
            image_size=(100, 100), ocr_scale=(1.0, 1.0), ocr_data=None,
        )
        self.assertEqual((built["rows"], built["cols"]), (1, 3))

    def test_truncates_when_boxes_and_cells_disagree(self) -> None:
        built = table_remote.build_table(
            entry([0, 0, 10, 10], [[0, 0, 5, 5]],
                  html("<td>a</td><td>b</td>")),
            image_size=(10, 10), ocr_scale=(1.0, 1.0), ocr_data=None,
        )
        self.assertEqual(len(built["cells"]), 1)

    def test_score_passes_through(self) -> None:
        built = table_remote.build_table(
            entry([0, 0, 10, 10], [[0, 0, 5, 5]], html("<td>a</td>"),
                  score=0.987),
            image_size=(10, 10), ocr_scale=(1.0, 1.0), ocr_data=None,
        )
        self.assertAlmostEqual(built["score"], 0.987)


class PickTableTests(unittest.TestCase):
    def test_no_bbox_takes_the_topmost_table(self) -> None:
        first = entry([0, 0, 10, 10], [[0, 0, 10, 10]], html("<td>a</td>"))
        second = entry([50, 50, 60, 60], [[50, 50, 60, 60]], html("<td>b</td>"))
        self.assertIs(table_remote.pick_table([first, second], None), first)

    def test_overlap_selects_the_requested_region(self) -> None:
        first = entry([0, 0, 10, 10], [[0, 0, 10, 10]], html("<td>a</td>"))
        second = entry([50, 50, 60, 60], [[50, 50, 60, 60]], html("<td>b</td>"))
        picked = table_remote.pick_table([first, second], (52, 52, 58, 58))
        self.assertIs(picked, second)

    def test_no_tables_picks_nothing(self) -> None:
        self.assertIsNone(table_remote.pick_table([], (0, 0, 1, 1)))


class CollectTablesTests(unittest.TestCase):
    def test_pairs_blocks_and_structures_by_order(self) -> None:
        pruned = {
            "parsing_res_list": [
                {"block_label": "paragraph_title", "block_bbox": [0, 0, 5, 5]},
                {"block_label": "table", "block_bbox": [0, 10, 20, 20]},
            ],
            "table_res_list": [{"pred_html": "<table></table>"}],
            "layout_det_res": {"boxes": [
                {"label": "table", "score": 0.99,
                 "coordinate": [0, 10, 20, 20]},
            ]},
        }
        collected = table_remote.collect_tables(pruned)
        self.assertEqual(len(collected), 1)
        self.assertAlmostEqual(collected[0]["score"], 0.99)

    def test_matching_falls_back_to_overlap_when_counts_differ(self) -> None:
        pruned = {
            "parsing_res_list": [
                {"block_label": "table", "block_bbox": [0, 0, 20, 20]},
                {"block_label": "table", "block_bbox": [100, 100, 120, 120]},
            ],
            "table_res_list": [
                {"pred_html": "<table></table>",
                 "cell_box_list": [[100, 100, 120, 120]]},
            ],
            "layout_det_res": {"boxes": []},
        }
        collected = table_remote.collect_tables(pruned)
        self.assertEqual(len(collected), 1)
        self.assertEqual(collected[0]["block"]["block_bbox"], [100, 100, 120, 120])

    def test_pages_without_tables_collect_nothing(self) -> None:
        self.assertEqual(
            table_remote.collect_tables({"parsing_res_list": [],
                                         "table_res_list": []}), [])


if __name__ == "__main__":
    unittest.main()
