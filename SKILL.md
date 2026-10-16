---
name: ppt-image-to-editable-ppt
description: Convert PPT slide screenshots or exported slide images into editable PowerPoint decks. Use when Codex needs to extract image/icon/material assets from one or many slide images as separate PNGs, rebuild the slides with editable text boxes, native shapes, and movable picture objects, batch-process multiple page images, and merge the reconstructed pages into a complete .pptx file.
---

# PPT Image To Editable PPT

## Overview

Turn slide images into a reconstructed, editable PowerPoint deck. Text
stays as real editable PowerPoint text. Icons, logos, photos and
decorative bands come out as separate movable PNG picture objects.
Cards, frames, circles and rules are rebuilt as native shapes/lines
where the source geometry is simple enough.

This skill is for slide screenshots or exported slide images. If a
native `.pptx` already exists, edit that directly.

Supported input formats use `page_NN.<ext>` naming and include PNG,
JPG/JPEG, WebP, BMP, and TIF/TIFF. Extracted assets and previews are
written as PNG.

## Dependencies

Run the bootstrap script from the repository root:

```bash
bash scripts/bootstrap.sh
```

It installs the Python packages in `requirements.txt`, local OCR tools,
LibreOffice, Poppler, and model caches where the platform supports
automatic installation. On managed systems, install these manually and
use `bash scripts/bootstrap.sh --no-system`.

## Run Directory

Every job goes under:

```text
output/<name>_<YYYYMMDD>/
├── slides.pptx
├── qa.json
├── previews/
├── ocr/
├── inventory/
├── manifests/
├── layouts/
├── assets/
└── debug/
```

## Workflow

Prepare source images:

```text
slides/
├── page_01.png
├── page_02.jpg
└── page_03.webp
```

Run the one-command pipeline:

```bash
RUN="output/demo_$(date +%Y%m%d)"
SRC="slides"

python scripts/convert.py --source "$SRC" --work-dir "$RUN"
```

For debugging, the same flow can be split into the three lower-level
steps:

```bash
python scripts/ocr/prepare_ocr.py \
  --source-dir "$SRC" \
  --work-dir "$RUN"

python scripts/ocr/ocr_review_apply.py --work-dir "$RUN"

python scripts/build_deck.py \
  --source-dir "$SRC" \
  --work-dir "$RUN"
```

`convert.py` creates or uses a run directory, normalizes single-image or
non-`page_NN` sources when needed, then calls the three lower-level
steps. `prepare_ocr.py` runs PP-OCRv5 across all pages (cloud service or
in-process — see "OCR Backend" below), builds
review packets for uncertain entries, and pre-fills `corrected_text`
with local consensus picks. `ocr_review_apply.py` merges those picks into
the OCR JSON files. `build_deck.py` runs erase, inventory extraction,
layout generation, deck assembly, preview-based font-size calibration,
preview-based text position calibration, QA inspection, and preview rendering.

## OCR Backend

Text recognition has two interchangeable backends; both emit the same JSON,
so nothing downstream distinguishes them.

- `remote` — submits the pages to the Baidu AI Studio PaddleOCR service
  (`scripts/ocr/ocr_remote.py`). Same PP-OCRv5 model, no local weights, no
  local CPU. All pages travel as ONE job (multi-page inputs are packed
  into a single PDF), so the cost does not grow per page the way a local
  loop does.
- `local` — runs the `paddleocr` package in-process
  (`scripts/ocr/ocr_paddle.py`). Needs a working `paddlepaddle` runtime;
  fully offline.

`--ocr-backend` defaults to `auto`: remote when a PaddleOCR token is
configured, local otherwise. The token comes from `--ocr-token`, then
`$PADDLE_OCR_TOKEN`, then `PADDLE_OCR_TOKEN` in `~/.editppt/config.yaml`.

Do not set `--ocr-model` to a `PaddleOCR-VL-*` name: that model answers
with layout blocks (one box per paragraph, multi-line text joined without
separators) while the layout stage needs one box per text line.

A user request to convert slide images authorizes the cloud OCR call
required for the conversion, unless the user asks for local-only or
confidential handling — then pass `--ocr-backend local`.

## Table Backend

`--detect-tables` verifies candidate table regions with the same two-way
choice:

- `remote` — the AI Studio **PP-StructureV3** service
  (`scripts/tables/table_remote.py`). It detects the table, returns its
  structure as HTML, and recognizes the cell text itself, so cells need
  no OCR-to-cell matching and merges come back as rowspan/colspan.
- `local` — PaddleX **SLANet_plus** (`scripts/tables/table_recognize.py`),
  structure only; cell text is filled from the upstream OCR by bbox
  containment.

`--table-backend` defaults to `auto` and follows the same rule as OCR:
remote when a PaddleOCR token is configured, local otherwise.

`--table-score-threshold` (default 0.85) was calibrated against SLANet's
structure score. The cloud backend has no equivalent structure score and
reports the table block's layout confidence instead, so the two numbers
are not the same measurement even though the gate still works.

## Optional Review

After `build_deck.py`, check whether it reports OCR entries that may
benefit from manual review. If the user wants the extra pass:

1. Open `ocr/page_NN.ocr_review.annotated.png`.
2. Edit `ocr/page_NN.ocr_review.json`.
3. Set `corrected_text` to the desired text, or to `""` when the OCR
   detection is not real editable text.
4. Rerun:

```bash
python scripts/ocr/ocr_review_apply.py --work-dir "$RUN"
python scripts/build_deck.py --source-dir "$SRC" --work-dir "$RUN"
```

## Useful Flags

- `--pages 1,4,8`: process only selected pages.
- `--skip-render`: skip LibreOffice preview generation and default text
  calibration.
- `--skip-calibration`: skip preview-based text size/position calibration.
- `--font-calibration-iterations 1`: set font-size calibration passes.
- `--calibration-iterations 4`: set text position calibration passes.
- `--detect-tables`: enable optional native table reconstruction.
- `--icon-review`: emit icon-vs-text review packets.
- `--icon-decisions`: apply filled icon review decisions on a rerun.

These flags can be passed to `scripts/convert.py`.

## Validation Standard

A job is done when:

- meaningful visual assets are present as PNG picture objects or native
  shapes;
- ordinary text is editable in the PPTX;
- extracted PNG assets are independent objects, not a flattened full-page
  background;
- `inspect_pptx.py` reports no failures;
- previews have been compared with the source images, or the final
  response states why preview rendering was skipped or unavailable.

Final responses should include the final `.pptx` path, the preview
directory, the QA report, and any known fidelity differences.
