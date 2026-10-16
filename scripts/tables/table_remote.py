#!/usr/bin/env python
"""Remote table structure recognition (PaddleOCR PP-StructureV3).

The cloud twin of `table_recognize.py`: the slide is sent to the Baidu AI
Studio PaddleOCR service, which detects the table itself, returns its
structure as HTML, and — unlike the local SLANet_plus model — also
recognizes the cell text, so no OCR-to-cell matching is needed to fill
the grid. Output is byte-compatible with the local script, so
`run_pipeline.py` can call either.

Why the whole slide and not the crop: PP-StructureV3 runs layout
detection first, so it does better on a page in context than on a
tightly cropped region. `--bbox` therefore selects WHICH detected table
to return (highest overlap with the region the caller asked about)
rather than defining the crop.

Pipeline:
    slide image  ──▶  AI Studio PP-StructureV3 job (~10-30s)
                            │
                            ▼
             layout blocks + table HTML + per-cell boxes
                            │
              (cells aligned with cell boxes by order)
                            │
                            ▼
                       tables.json

Output schema — identical to `table_recognize.py`, one table per
invocation:
    {
      "bbox": [x1, y1, x2, y2],         // table region in SOURCE-image coords
      "rows": 4, "cols": 5,
      "score": 0.99,                    // table block's layout confidence
      "html": "<html><body><table>...",
      "cells": [
        {"row": 0, "col": 0,
         "rowspan": 1, "colspan": 5,
         "bbox": [x1, y1, x2, y2],      // in SOURCE-image coords
         "text": "..."}
        ...
      ]
    }

Note on `score`: the local backend reports SLANet's structure confidence,
which the caller gates at `--table-score-threshold` (default 0.85). The
cloud service does not return a comparable structure score, so this
backend reports the layout detector's confidence for the table block.
That gate stays useful (it is what rejects decorative banners), but its
number is no longer measured the same way, so the threshold is not
directly transferable between the two backends.

Usage:
    python scripts/tables/table_remote.py slide.png \
        --bbox 100,200,900,650 \
        --ocr output/<run_dir>/ocr/page_01.ocr.json \
        --out output/<run_dir>/ocr/page_01.table_00.json
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))
if str(SCRIPTS / "ocr") not in sys.path:
    sys.path.insert(0, str(SCRIPTS / "ocr"))

import ocr_remote  # noqa: E402

DEFAULT_MODEL = "PP-StructureV3"

# Table recognition on, everything else off: seals, formulas and charts
# cost latency and are never consumed by the deck builder.
TABLE_PAYLOAD = {
    "useDocOrientationClassify": False,
    "useDocUnwarping": False,
    "useLayoutDetection": True,
    "useTableRecognition": True,
    "useSealRecognition": False,
    "useFormulaRecognition": False,
    "useChartRecognition": False,
    "useRegionDetection": True,
}

_CELL_RE = re.compile(
    r"<(?P<tag>td|th)(?P<attrs>[^>]*)>(?P<body>.*?)</(?P=tag)>",
    re.IGNORECASE | re.DOTALL,
)
_ROW_RE = re.compile(r"<tr[^>]*>(.*?)</tr>", re.IGNORECASE | re.DOTALL)
_TAG_RE = re.compile(r"<[^>]+>")
_BR_RE = re.compile(r"<br\s*/?>", re.IGNORECASE)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Remote table structure recognition (PP-StructureV3)."
    )
    parser.add_argument("image", help="Slide image.")
    parser.add_argument("--bbox",
                        help="Table region 'x1,y1,x2,y2' in source-image "
                             "pixel coords. Used to choose which detected "
                             "table to return; if omitted, the first table "
                             "on the page.")
    parser.add_argument("--ocr",
                        help="OCR JSON for the whole slide. Only consulted "
                             "to fill cells the service left empty.")
    parser.add_argument("--out", help="Output JSON path. Default: stdout.")
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--token", default=None,
                        help="Cloud token; defaults to $PADDLE_OCR_TOKEN "
                             "then ~/.editppt/config.yaml.")
    parser.add_argument("--timeout", type=float,
                        default=ocr_remote.DEFAULT_TIMEOUT)
    return parser.parse_args()


def _clean_text(fragment: str) -> str:
    """Cell HTML → plain text (tables in the wild contain <br>, spans)."""
    text = _BR_RE.sub("\n", fragment)
    text = _TAG_RE.sub("", text)
    text = (text.replace("&nbsp;", " ").replace("&amp;", "&")
                .replace("&lt;", "<").replace("&gt;", ">")
                .replace("&quot;", '"').replace("&#39;", "'"))
    # Collapse the whitespace HTML sources carry around cell text without
    # destroying the deliberate line breaks an in-cell <br> produced.
    lines = [" ".join(line.split()) for line in text.split("\n")]
    return "\n".join(line for line in lines if line).strip()


def _span(attrs: str, name: str) -> int:
    m = re.search(rf'{name}\s*=\s*"?(\d+)"?', attrs, re.IGNORECASE)
    if not m:
        return 1
    try:
        return max(1, int(m.group(1)))
    except ValueError:
        return 1


def parse_html_grid(html: str) -> list[dict]:
    """HTML table → cells with (row, col, rowspan, colspan, text).

    Document order is preserved, and that order is what pairs each cell
    with its box in the service's `cell_box_list`. Placement follows the
    HTML table model: a cell takes the leftmost column still free in its
    row, where earlier rowspan cells keep their columns occupied in the
    rows below.
    """
    cells: list[dict] = []
    occupied: list[list[bool]] = []

    def ensure(row: int, col: int) -> None:
        while len(occupied) <= row:
            occupied.append([])
        while len(occupied[row]) <= col:
            occupied[row].append(False)

    for row_index, row_html in enumerate(_ROW_RE.findall(html)):
        col = 0
        for match in _CELL_RE.finditer(row_html):
            rowspan = _span(match.group("attrs"), "rowspan")
            colspan = _span(match.group("attrs"), "colspan")
            while True:
                ensure(row_index, col)
                if not occupied[row_index][col]:
                    break
                col += 1
            for dr in range(rowspan):
                for dc in range(colspan):
                    ensure(row_index + dr, col + dc)
                    occupied[row_index + dr][col + dc] = True
            cells.append({
                "row": row_index,
                "col": col,
                "rowspan": rowspan,
                "colspan": colspan,
                "text": _clean_text(match.group("body")),
            })
            col += colspan
    return cells


def _aabb(box) -> tuple[int, int, int, int]:
    flat = list(box)
    if flat and isinstance(flat[0], (list, tuple)):
        xs = [float(p[0]) for p in flat]
        ys = [float(p[1]) for p in flat]
    else:
        xs = [float(flat[i]) for i in range(0, len(flat), 2)]
        ys = [float(flat[i]) for i in range(1, len(flat), 2)]
    return int(min(xs)), int(min(ys)), int(max(xs)), int(max(ys))


def _iou(a, b) -> float:
    ix1, iy1 = max(a[0], b[0]), max(a[1], b[1])
    ix2, iy2 = min(a[2], b[2]), min(a[3], b[3])
    if ix2 <= ix1 or iy2 <= iy1:
        return 0.0
    inter = (ix2 - ix1) * (iy2 - iy1)
    area_a = (a[2] - a[0]) * (a[3] - a[1])
    area_b = (b[2] - b[0]) * (b[3] - b[1])
    union = area_a + area_b - inter
    return inter / union if union > 0 else 0.0


def collect_tables(pruned: dict) -> list[dict]:
    """Pair every layout `table` block with its structure result.

    The service returns the two lists in the same per-page order, so
    index pairing is the natural fit; if the counts ever disagree we fall
    back to matching each structure entry to the block it overlaps most.
    """
    blocks = [b for b in pruned.get("parsing_res_list", [])
              if str(b.get("block_label", "")).lower() == "table"]
    structures = list(pruned.get("table_res_list") or [])
    detectors = {tuple(b.get("block_bbox") or []): b
                 for b in pruned.get("layout_det_res", {}).get("boxes", [])
                 if str(b.get("label", "")).lower() == "table"}

    def layout_score(block) -> float:
        box = tuple(block.get("block_bbox") or [])
        hit = detectors.get(box)
        if hit is not None:
            try:
                return float(hit.get("score") or 0.0)
            except (TypeError, ValueError):
                return 0.0
        # No layout box recorded for this block: fall back to the best
        # overlap against any detected table box.
        best = 0.0
        span = _aabb(box) if len(box) == 4 else (0, 0, 0, 0)
        for candidate in detectors.values():
            try:
                best = max(best, float(candidate.get("score") or 0.0)
                           * _iou(span, _aabb(candidate["coordinate"])))
            except (TypeError, ValueError, KeyError):
                continue
        return best

    out: list[dict] = []
    if len(blocks) == len(structures):
        for block, structure in zip(blocks, structures):
            out.append({"block": block, "structure": structure,
                        "score": layout_score(block)})
        return out

    for block in blocks:
        span = _aabb(block.get("block_bbox") or [0, 0, 0, 0])
        best, best_iou = None, 0.0
        for structure in structures:
            boxes = structure.get("cell_box_list") or []
            if not boxes:
                continue
            xs1 = [min(float(b[0]), float(b[2])) for b in boxes]
            ys1 = [min(float(b[1]), float(b[3])) for b in boxes]
            xs2 = [max(float(b[0]), float(b[2])) for b in boxes]
            ys2 = [max(float(b[1]), float(b[3])) for b in boxes]
            structure_span = (min(xs1), min(ys1), max(xs2), max(ys2))
            iou = _iou(span, structure_span)
            if iou > best_iou:
                best, best_iou = structure, iou
        if best is not None and best_iou > 0:
            out.append({"block": block, "structure": best,
                        "score": layout_score(block)})
    return out


def pick_table(tables: list[dict], bbox) -> dict | None:
    """The table the caller asked about — by overlap when a region was
    given, otherwise the topmost one."""
    if not tables:
        return None
    if bbox is None:
        return tables[0]
    scored = []
    for entry in tables:
        span = _aabb(entry["block"].get("block_bbox") or [0, 0, 0, 0])
        scored.append((_iou(span, bbox), span, entry))
    scored.sort(key=lambda item: item[0], reverse=True)
    return scored[0][2]


def _attach_text_from_ocr(cell_bbox, ocr_data: list[dict]) -> str:
    """Same containment rule the local backend uses: OCR items whose
    centre falls inside the cell, in reading order."""
    x1, y1, x2, y2 = cell_bbox
    hits = [it for it in ocr_data
            if x1 <= (int(it["x1"]) + int(it["x2"])) // 2 <= x2
            and y1 <= (int(it["y1"]) + int(it["y2"])) // 2 <= y2]
    hits.sort(key=lambda it: (int(it["y1"]), int(it["x1"])))
    return " ".join(str(h["text"]) for h in hits).strip()


def build_table(entry: dict, *, image_size, ocr_scale, ocr_data) -> dict:
    """One service table → the local backend's output contract."""
    block_bbox = _aabb(entry["block"].get("block_bbox") or [0, 0, 0, 0])
    scale_x, scale_y = ocr_scale
    width, height = image_size

    def to_source(box) -> list[int]:
        x1, y1, x2, y2 = _aabb(box)
        x1 = max(0, min(width, int(round(x1 * scale_x))))
        y1 = max(0, min(height, int(round(y1 * scale_y))))
        x2 = max(0, min(width, int(round(x2 * scale_x))))
        y2 = max(0, min(height, int(round(y2 * scale_y))))
        return [x1, y1, x2, y2]

    html = str(entry["structure"].get("pred_html") or "")
    cells = parse_html_grid(html) if html else []
    boxes = list(entry["structure"].get("cell_box_list") or [])

    if boxes and len(boxes) != len(cells):
        sys.stderr.write(
            f"[table_remote] WARN: {len(boxes)} cell boxes vs {len(cells)} "
            f"HTML cells — truncating to {min(len(boxes), len(cells))}.\n"
        )
    n = min(len(boxes), len(cells))
    for index in range(n):
        bbox = to_source(boxes[index])
        cells[index]["bbox"] = bbox
        if not cells[index]["text"] and ocr_data:
            cells[index]["text"] = _attach_text_from_ocr(bbox, ocr_data)
    cells = cells[:n]

    rows = max((c["row"] + c["rowspan"] for c in cells), default=0)
    cols = max((c["col"] + c["colspan"] for c in cells), default=0)
    return {
        "bbox": to_source(block_bbox),
        "rows": rows,
        "cols": cols,
        "score": float(entry.get("score") or 0.0),
        "html": html,
        "cells": cells,
    }


def recognize(image_path: Path, *, token: str, model: str = DEFAULT_MODEL,
              bbox=None, ocr_data=None,
              timeout: float = ocr_remote.DEFAULT_TIMEOUT) -> dict:
    """Run the service on one slide and return the requested table (or {})."""
    session = ocr_remote._session(token)
    job_id = ocr_remote.submit_job(session, image_path, model, TABLE_PAYLOAD)
    print(f"[table_remote] job {job_id} (model={model})", file=sys.stderr)
    status = ocr_remote.poll_job(session, job_id, timeout)
    result = ocr_remote.fetch_result(session, status["resultUrl"]["jsonUrl"])

    data_info = result.get("result", {}).get("dataInfo", {})
    ocr_w, ocr_h = ocr_remote._page_dims(data_info, 0)
    width, height = ocr_remote.source_size(image_path)
    scale = (width / ocr_w if ocr_w else 1.0,
             height / ocr_h if ocr_h else 1.0)

    payloads = ocr_remote.ocr_results(result, key="layoutParsingResults")
    pruned = payloads[0]
    entry = pick_table(collect_tables(pruned), bbox)
    if entry is None:
        return {}
    return build_table(entry, image_size=(width, height), ocr_scale=scale,
                       ocr_data=ocr_data)


def main() -> int:
    args = parse_args()
    image_path = Path(args.image)
    if not image_path.exists():
        sys.stderr.write(f"ERROR: image not found: {image_path}\n")
        return 1

    bbox = None
    if args.bbox:
        try:
            bbox = tuple(int(v) for v in args.bbox.split(","))
            if len(bbox) != 4:
                raise ValueError
        except ValueError:
            sys.stderr.write("ERROR: --bbox must be 'x1,y1,x2,y2' integers.\n")
            return 1

    ocr_data = None
    if args.ocr:
        try:
            ocr_data = json.loads(Path(args.ocr).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            sys.stderr.write(f"WARN: could not read --ocr ({exc}); "
                             "cell text will come from the service only.\n")

    try:
        token = ocr_remote.resolve_token(args.token)
    except ocr_remote.RemoteOCRError as exc:
        sys.stderr.write(
            f"ERROR: {exc}\n"
            "  The remote table backend needs the same PaddleOCR token as "
            "the OCR stage.\n"
            "  Pass --table-backend local to use SLANet_plus instead.\n"
        )
        return 1

    try:
        table = recognize(image_path, token=token, model=args.model,
                          bbox=bbox, ocr_data=ocr_data,
                          timeout=args.timeout)
    except ocr_remote.RemoteOCRError as exc:
        sys.stderr.write(f"ERROR: remote table recognition failed: {exc}\n")
        return 1

    out_json = json.dumps(table, ensure_ascii=False, indent=2)
    if args.out:
        out_path = Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(out_json, encoding="utf-8")
        sys.stderr.write(
            f"[table_remote] {table.get('rows', 0)}x{table.get('cols', 0)} "
            f"table (score={table.get('score', 0):.3f}) → {out_path}\n"
        )
    else:
        print(out_json)
        sys.stderr.write(
            f"[table_remote] {table.get('rows', 0)}x{table.get('cols', 0)} "
            f"table (score={table.get('score', 0):.3f})\n"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
