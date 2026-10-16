#!/usr/bin/env python
"""EasyOCR -> ocr/page_NN.ocr.json in DeckWeaver's schema.

Same output contract as scripts/ocr/ocr_paddle.py's extract_items():
    {"text", "x1", "y1", "x2", "y2", "confidence"}

Used when the Paddle backend is unavailable (e.g. no paddlepaddle wheel for
the running Python), so the downstream erase / inventory / layout stages get
identical input. Also emits per-character boxes for pure-CJK lines so
inventory_to_layout can do per-character colour sampling.

Usage:
    python scripts/ocr/easyocr_ocr.py --source-dir slides --work-dir RUN
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import easyocr
import numpy as np
from PIL import Image

MIN_CONF_DEFAULT = 0.30


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--source-dir", required=True)
    p.add_argument("--work-dir", required=True)
    p.add_argument("--min-conf", type=float, default=MIN_CONF_DEFAULT)
    p.add_argument("--langs", default="ch_sim,en")
    return p.parse_args()


def _bbox_from_quad(quad) -> tuple[int, int, int, int]:
    xs = [float(pt[0]) for pt in quad]
    ys = [float(pt[1]) for pt in quad]
    return int(round(min(xs))), int(round(min(ys))), \
        int(round(max(xs))), int(round(max(ys)))


def _is_pure_cjk(text: str) -> bool:
    if not text:
        return False
    for ch in text:
        o = ord(ch)
        if not (0x3000 <= o <= 0x303F or 0x4E00 <= o <= 0x9FFF
                or 0xFF00 <= o <= 0xFFEF):
            return False
    return True


def items_from_result(result, min_conf: float) -> list[dict]:
    items: list[dict] = []
    for quad, text, conf in result:
        if float(conf) < min_conf:
            continue
        text = (text or "").strip()
        if not text:
            continue
        x1, y1, x2, y2 = _bbox_from_quad(quad)
        if x2 <= x1 or y2 <= y1:
            continue
        item = {
            "text": text,
            "x1": x1, "y1": y1, "x2": x2, "y2": y2,
            "confidence": float(conf),
        }
        # Even-width per-character split for pure-CJK lines: keeps the
        # legacy chars/char_boxes interface alive for colour sampling.
        if _is_pure_cjk(text) and len(text) > 1:
            step = (x2 - x1) / len(text)
            item["chars"] = list(text)
            item["char_boxes"] = [
                [int(round(x1 + i * step)), y1,
                 int(round(x1 + (i + 1) * step)), y2]
                for i in range(len(text))
            ]
        items.append(item)
    return items


def main() -> int:
    args = parse_args()
    src_dir = Path(args.source_dir)
    work = Path(args.work_dir)
    ocr_dir = work / "ocr"
    ocr_dir.mkdir(parents=True, exist_ok=True)

    pages = sorted(p for p in src_dir.iterdir()
                   if p.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp",
                                           ".bmp", ".tif", ".tiff"})
    if not pages:
        print(f"ERROR: no images in {src_dir}", file=sys.stderr)
        return 1

    langs = [s for s in args.langs.split(",") if s]
    print(f"=== EasyOCR ({len(pages)} page(s), langs={langs}) ===", flush=True)
    t0 = time.time()
    reader = easyocr.Reader(langs, gpu=False, verbose=False)
    print(f"  reader ready in {time.time() - t0:.1f}s", flush=True)

    for page in pages:
        n = page.stem.split("_")[-1]
        t1 = time.time()
        arr = np.array(Image.open(page).convert("RGB"))
        raw = reader.readtext(arr, detail=1, paragraph=False)
        items = items_from_result(raw, args.min_conf)
        out = ocr_dir / f"page_{n}.ocr.json"
        out.write_text(json.dumps(items, ensure_ascii=False, indent=2),
                       encoding="utf-8")
        print(f"  {page.name} -> {len(items)} items "
              f"({time.time() - t1:.1f}s)", flush=True)
    print(f"  OCR total {time.time() - t0:.1f}s", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
