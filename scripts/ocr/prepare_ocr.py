#!/usr/bin/env python
"""Single-command OCR preparation with 3-engine cross-verification.

Runs PP-OCRv5 over every supported `page_NN.*` image in --source-dir.
The recognizer is either the Baidu AI Studio cloud service (`--ocr-backend
remote`, no local Paddle install, all pages in one job) or the local
paddleocr package (`--ocr-backend local`); both emit the same JSON shape,
so nothing downstream changes. `auto` (the default) picks remote whenever
a PaddleOCR token is available. For
each page:

  1. Runs PP-OCRv5 to get baseline text + bbox + confidence
  2. Builds a review packet for entries with conf < --threshold (default 0.95)
  3. For each review entry, runs EasyOCR + Tesseract 5 on the cropped
     region and assigns a 3-engine consensus tier (the optional 4th-engine
     Paddle crop rescue needs a local model, or --remote-crop-rescue to
     spend one cloud job per red entry):
       🟢 green  — strong (3/3 or 2/3 agree)
       🟡 yellow — weak  (small edit-distance differences, OR one
                          engine confidence ≥ 0.95, OR strongest
                          engine conf ≥ 0.85 with disagreement)
       🔴 red    — no consensus, no even-moderately-confident engine
     suggested_text is the script-aware best guess. corrected_text is
     pre-filled with suggested_text so the deck builds without agent
     input.
  4. Applies the 6 conservative autoclear heuristics (zero-FP) — these
     override corrected_text to "" for obvious logo/decorative misreads.
  5. Renders ONE annotated PNG per page showing:
       - the source image with every review entry's bbox outlined in
         its tier color and labeled with #N
       - a side legend listing each entry's 3 engine candidates and
         the suggested text

Outputs:

    inventory/page_NN.ocr.json                    raw OCR detections
    inventory/page_NN.ocr_review.json             review packet (with tier,
                                                  candidates, suggested,
                                                  corrected_text pre-filled)
    inventory/page_NN.ocr_review.annotated.png    full-page annotated
                                                  review image (only
                                                  written when entries
                                                  exist)

The agent step is OPTIONAL now: after build_deck.py produces a first-
draft PPTX, the user is asked whether to do a manual review of the
annotated pages. If they say no, the deck ships as-is using consensus
picks. If yes, the agent overrides specific entries by editing
corrected_text in the review JSONs, then build_deck.py reruns.

Usage:
    python scripts/ocr/prepare_ocr.py \\
        --source-dir slides/ \\
        --work-dir   output/<name>_<YYYYMMDD>/

Optional flags:
    --pages 1,4,8         only process specified page numbers
    --ocr-backend auto    auto | remote (AI Studio cloud) | local (paddleocr)
    --ocr-token TOKEN     cloud token; defaults to $PADDLE_OCR_TOKEN then
                          ~/.editppt/config.yaml
    --ocr-model PP-OCRv5  cloud model name
    --threshold 0.95      PP-OCRv5 conf threshold for queueing review
    --max-entries 50      cap on review entries per page
    --image-size WxH      slide size used by autoclear (default 1280x720)
    --high-conf 0.95      single-engine threshold for D' rule
    --moderate-conf 0.85  single-engine threshold for G fallback rule
    --skip-cross-verify   skip 3-engine pass (review tier always "red")
    --skip-autoclear      skip the autoclear pass
    --remote-crop-rescue  allow the 4th-engine crop rescue to spend one
                          cloud job per red entry (remote backend only)
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path

# Sibling-script imports — same directory as this file.
SCRIPTS = Path(__file__).resolve().parent
SCRIPTS_ROOT = SCRIPTS.parent
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(SCRIPTS_ROOT))

from image_sources import (  # noqa: E402
    discover_page_numbers, find_page_image, supported_image_formats,
)
import ocr_remote  # noqa: E402
from ocr_paddle import extract_items, quiet_paddle  # noqa: E402
from ocr_review_autoclear import match_rule, parse_size  # noqa: E402
from ocr_cross_verify import (  # noqa: E402
    EnginePred, cross_verify, paddle_crop_rescue,
    run_easyocr, run_paddle_crop, run_tesseract,
)
from render_annotated_review import render_annotated_page  # noqa: E402
from shared.gpu import paddle_device  # noqa: E402


# Crop-rescue upscaling rule, kept in step with
# `ocr_cross_verify.run_paddle_crop`'s defaults.
REMOTE_RESCUE_UPSCALE_WIDTH = 200
REMOTE_RESCUE_UPSCALE_FACTOR = 3

REVIEW_INSTRUCTIONS = (
    "Each entry's `corrected_text` has been pre-filled with the 3-engine "
    "consensus suggestion. Open the matching "
    "`inventory/page_NN.ocr_review.annotated.png` to see every entry "
    "highlighted on the source slide (color: 🟢 green = strong consensus, "
    "🟡 yellow = weak consensus, 🔴 red = no consensus). For each entry "
    "you disagree with, edit `corrected_text` here. Leave it as-is to "
    "accept the suggestion. Set `corrected_text` to \"\" if the detection "
    "is not real editable text (icon glyph, decorative mark)."
)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Batch OCR + 3-engine cross-verify + autoclear + "
                    "annotated review images in one process."
    )
    p.add_argument("--source-dir", required=True,
                   help=f"Directory containing page_NN images "
                        f"({supported_image_formats()}).")
    p.add_argument("--work-dir", required=True,
                   help="Run dir; receives ocr/ outputs.")
    p.add_argument("--pages",
                   help="Comma-separated page numbers (e.g. 1,4,8). "
                        "Default: every supported page_* image in "
                        "--source-dir.")
    p.add_argument("--lang", default="ch", help="PaddleOCR lang (default ch).")
    p.add_argument("--ocr-backend", choices=["auto", "remote", "local"],
                   default="auto",
                   help="Recognizer to use. auto (default) = the AI Studio "
                        "cloud service when a token is available, else the "
                        "local paddleocr package. remote = cloud only; "
                        "local = paddleocr only.")
    p.add_argument("--ocr-token", default=None,
                   help="PaddleOCR cloud token; defaults to "
                        "$PADDLE_OCR_TOKEN, then ~/.editppt/config.yaml.")
    p.add_argument("--ocr-model", default=ocr_remote.DEFAULT_MODEL,
                   help=f"Cloud model name (default {ocr_remote.DEFAULT_MODEL}).")
    p.add_argument("--ocr-timeout", type=float,
                   default=ocr_remote.DEFAULT_TIMEOUT,
                   help="Cloud job polling timeout in seconds.")
    p.add_argument("--remote-crop-rescue", action="store_true",
                   help="Let the 4th-engine crop rescue run on the cloud "
                        "backend (one job per red entry — slow). Off by "
                        "default; the local backend always rescues "
                        "in-process.")
    p.add_argument("--min-conf", type=float, default=0.5,
                   help="Minimum Paddle confidence to emit (default 0.5).")
    p.add_argument("--threshold", type=float, default=0.95,
                   help="Review queueing threshold; Paddle entries below "
                        "this go through 3-engine cross-verify (default 0.95).")
    p.add_argument("--padding", type=int, default=6,
                   help="Padding around OCR bbox when cropping for "
                        "cross-verify (default 6).")
    p.add_argument("--max-entries", type=int, default=50,
                   help="Cap on review entries per page (default 50).")
    p.add_argument("--image-size", default="1280x720",
                   help="Source slide size WxH used by autoclear "
                        "(default 1280x720).")
    p.add_argument("--high-conf", type=float, default=0.95,
                   help="Single-engine confidence to trigger D' "
                        "(yellow tier, default 0.95).")
    p.add_argument("--moderate-conf", type=float, default=0.85,
                   help="Strongest-engine confidence to trigger G "
                        "fallback (yellow tier, default 0.85).")
    p.add_argument("--skip-cross-verify", action="store_true",
                   help="Skip the 3-engine pass; all review entries will "
                        "be classified as red. Use for quick local checks.")
    p.add_argument("--skip-autoclear", action="store_true",
                   help="Skip the autoclear heuristics pass.")
    return p.parse_args()


def discover_pages(src_dir: Path, pages_arg: str | None) -> list[str]:
    if pages_arg:
        return [n.strip().zfill(2) for n in pages_arg.split(",")]
    return discover_page_numbers(src_dir)


def resolve_backend(args: argparse.Namespace) -> str:
    """Pick the recognizer: auto prefers the cloud when a token exists."""
    if args.ocr_backend == "local":
        return "local"
    if args.ocr_backend == "remote":
        return "remote"
    if (args.ocr_token or os.environ.get("PADDLE_OCR_TOKEN")
            or ocr_remote.read_config_token()):
        return "remote"
    return "local"


def remote_token(args: argparse.Namespace) -> str:
    try:
        return ocr_remote.resolve_token(args.ocr_token)
    except ocr_remote.RemoteOCRError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)


def run_ocr_remote(pairs: list[tuple[Path, Path]], args: argparse.Namespace,
                   token: str) -> list[int]:
    """OCR every page through ONE cloud job; return per-page item counts.

    Pages are packed into a multi-page PDF and uploaded together, so the
    per-page cost is upload-plus-poll once, not once per page.
    """
    counts = [0] * len(pairs)
    todo = [(i, img, out) for i, (img, out) in enumerate(pairs)
            if img.exists()]
    for img, _ in pairs:
        if not img.exists():
            print(f"  SKIP missing: {img}", file=sys.stderr)
    if not todo:
        return counts
    print(f"  submitting {len(todo)} page(s) to {args.ocr_model} "
          f"in one job", flush=True)
    try:
        results = ocr_remote.run_pages(
            [img for _, img, _ in todo], token=token, model=args.ocr_model,
            min_conf=args.min_conf, timeout=args.ocr_timeout,
        )
    except ocr_remote.RemoteOCRError as exc:
        print(f"ERROR: remote OCR failed: {exc}", file=sys.stderr)
        raise SystemExit(1)
    for (i, img, out), items in zip(todo, results):
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(items, ensure_ascii=False, indent=2),
                       encoding="utf-8")
        counts[i] = len(items)
        print(f"  {img.name} -> {len(items)} items", flush=True)
    return counts


def run_ocr_batch(ocr, pairs: list[tuple[Path, Path]],
                  min_conf: float) -> list[int]:
    """Run PaddleOCR.predict on each (src, out_json) pair; return per-page
    item counts. The OCR model is reused across pages — this is where
    the warm-model speedup happens (~3 s startup, ~9 s/page recognition).
    """
    counts = []
    for img_path, out_path in pairs:
        if not img_path.exists():
            print(f"  SKIP missing: {img_path}", file=sys.stderr)
            counts.append(0)
            continue
        try:
            result = ocr.predict(str(img_path))
        except Exception as exc:
            print(f"  FAIL {img_path}: {exc}", file=sys.stderr)
            counts.append(0)
            continue
        items = extract_items(result, min_conf)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(
            json.dumps(items, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        counts.append(len(items))
        print(f"  {img_path.name} -> {len(items)} items", flush=True)
    return counts


def make_remote_rescuer(args: argparse.Namespace, token: str):
    """Build the 4th-engine crop-rescue callable for the cloud backend.

    Mirrors `ocr_cross_verify.run_paddle_crop` — including its
    width-based upscaling rule, so both backends ask for more pixels per
    character on the same crops. Each call is one cloud job, which is
    why this stays behind --remote-crop-rescue.
    """
    from PIL import Image  # noqa: PLC0415

    from ocr_cross_verify import EnginePred  # noqa: PLC0415

    def rescuer(crop_image) -> EnginePred:
        try:
            if crop_image.width >= REMOTE_RESCUE_UPSCALE_WIDTH:
                img = crop_image.resize(
                    (crop_image.width * REMOTE_RESCUE_UPSCALE_FACTOR,
                     crop_image.height * REMOTE_RESCUE_UPSCALE_FACTOR),
                    Image.LANCZOS,
                )
            else:
                img = crop_image
            text, conf = ocr_remote.run_crop(
                img, token=token, model=args.ocr_model,
                timeout=args.ocr_timeout,
            )
            return EnginePred(text, conf)
        except Exception:
            return EnginePred("", 0.0)

    return rescuer


def build_review_packet(ocr_path: Path, image_path: Path, *,
                        out_path: Path, threshold: float,
                        padding: int, max_entries: int,
                        high_conf: float, moderate_conf: float,
                        do_cross_verify: bool,
                        paddle_model=None,
                        remote_rescuer=None) -> dict:
    """Run 3-engine cross-verify on low-confidence Paddle entries and
    write the review packet.

    The review JSON's `entries` look like:
        {
          "idx": int,
          "original_text": str,           # PaddleOCR text
          "confidence": float,            # PaddleOCR conf
          "bbox": [x1, y1, x2, y2],
          "tier": "green" | "yellow" | "red",
          "reason": str,                  # cross_verify rule that fired
          "candidates": {
            "paddle":    {"text": ..., "conf": ...},
            "easyocr":   {"text": ..., "conf": ...},
            "tesseract": {"text": ..., "conf": ...},
          },
          "suggested_text": str,          # consensus pick
          "corrected_text": str,          # pre-filled = suggested_text
          "notes": str | None,
        }
    """
    from PIL import Image
    import tempfile

    ocr_data = json.loads(ocr_path.read_text(encoding="utf-8"))
    img = Image.open(image_path).convert("RGB")
    W, H = img.size

    candidates_idx = [
        (idx, item) for idx, item in enumerate(ocr_data)
        if float(item.get("confidence", 1.0)) < threshold
    ]
    candidates_idx.sort(key=lambda p: float(p[1].get("confidence", 1.0)))
    candidates_idx = candidates_idx[:max_entries]

    out_path.parent.mkdir(parents=True, exist_ok=True)

    entries = []
    tier_count = {"green": 0, "yellow": 0, "red": 0}
    rescue_stats = {"attempted": 0, "upgraded": 0}
    with tempfile.TemporaryDirectory(prefix="xv_") as td:
        td = Path(td)
        for idx, item in candidates_idx:
            x1 = max(0, int(item["x1"]) - padding)
            y1 = max(0, int(item["y1"]) - padding)
            x2 = min(W, int(item["x2"]) + padding)
            y2 = min(H, int(item["y2"]) + padding)
            if x2 <= x1 or y2 <= y1:
                continue
            crop_path = td / f"crop_{idx:04d}.png"
            crop_img = img.crop((x1, y1, x2, y2))
            crop_img.save(crop_path)

            paddle = EnginePred(item["text"],
                                float(item.get("confidence", 0.0)))
            if do_cross_verify:
                easy = run_easyocr(crop_path)
                tess = run_tesseract(crop_path)
                xv = cross_verify(paddle, easy, tess,
                                  high_conf=high_conf,
                                  moderate_conf=moderate_conf)
                # 4th-engine rescue: only on RED entries (the only
                # case where it can change the outcome), and only if
                # the caller supplied a recognizer — a warm local
                # Paddle model, or `remote_rescuer` for the cloud
                # backend. Re-runs the recognizer on the isolated crop
                # with width-based upscaling — empirically this
                # corrects ~30-50% of dense-CJK RED entries.
                if xv.tier == "red" and (paddle_model is not None
                                         or remote_rescuer is not None):
                    rescue_stats["attempted"] += 1
                    if paddle_model is not None:
                        paddle_crop_pred = run_paddle_crop(crop_img,
                                                           paddle_model)
                    else:
                        paddle_crop_pred = remote_rescuer(crop_img)
                    xv_after = paddle_crop_rescue(
                        xv, paddle_crop_pred, moderate_conf=moderate_conf,
                    )
                    if xv_after.tier != xv.tier:
                        rescue_stats["upgraded"] += 1
                    xv = xv_after
            else:
                # Skipping cross-verify: tag everything red with Paddle's
                # text as the suggestion. Useful for fast iteration.
                from ocr_cross_verify import CrossVerifyResult
                xv = CrossVerifyResult(
                    tier="red", suggested_text=paddle.text,
                    reason="cross_verify_skipped",
                    candidates={
                        "paddle": {"text": paddle.text,
                                   "conf": round(paddle.conf, 4)},
                        "easyocr": {"text": "", "conf": 0.0},
                        "tesseract": {"text": "", "conf": 0.0},
                    },
                )

            tier_count[xv.tier] += 1
            entries.append({
                "idx": idx,
                "original_text": item["text"],
                "confidence": float(item.get("confidence", 0.0)),
                "bbox": [int(item["x1"]), int(item["y1"]),
                         int(item["x2"]), int(item["y2"])],
                "tier": xv.tier,
                "reason": xv.reason,
                "candidates": xv.candidates,
                "suggested_text": xv.suggested_text,
                "corrected_text": xv.suggested_text,
                "notes": None,
            })

    review = {
        "_instructions": REVIEW_INSTRUCTIONS,
        "source_ocr": str(ocr_path),
        "source_image": str(image_path),
        "threshold": threshold,
        "high_conf": high_conf,
        "moderate_conf": moderate_conf,
        "padding": padding,
        "tier_counts": tier_count,
        "rescue_stats": rescue_stats,
        "entries": entries,
    }
    out_path.write_text(
        json.dumps(review, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return review


def apply_autoclear(review_path: Path, image_w: int,
                    image_h: int) -> tuple[int, dict[str, int]]:
    """Run the 6 conservative autoclear heuristics on the review packet.

    Overrides corrected_text to "" for any entry where a rule fires —
    these are decorative/logo OCRs that should be treated as
    preserved-visual (not editable text), regardless of cross-verify's
    suggestion.
    """
    data = json.loads(review_path.read_text(encoding="utf-8"))
    cleared = 0
    by_rule: dict[str, int] = {}
    for e in data.get("entries", []):
        # Skip entries already overridden manually (corrected_text differs
        # from suggested_text → user/agent already touched it).
        if e.get("corrected_text") != e.get("suggested_text"):
            continue
        rule = match_rule(
            e["original_text"], float(e["confidence"]),
            tuple(e["bbox"]), image_w, image_h,
        )
        if rule:
            e["corrected_text"] = ""
            e["notes"] = f"auto-cleared by {rule}"
            cleared += 1
            by_rule[rule] = by_rule.get(rule, 0) + 1
    review_path.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                           encoding="utf-8")
    return cleared, by_rule


def main() -> int:
    args = parse_args()
    src_dir = Path(args.source_dir)
    work = Path(args.work_dir)
    ocr_dir = work / "ocr"
    ocr_dir.mkdir(parents=True, exist_ok=True)
    try:
        nums = discover_pages(src_dir, args.pages)
        image_paths = {n: find_page_image(src_dir, n) for n in nums}
    except (OSError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    if not nums:
        print(f"ERROR: no supported page_* images found in {src_dir} "
              f"({supported_image_formats()})", file=sys.stderr)
        return 1

    image_w, image_h = parse_size(args.image_size)

    # ---- Stage 1: OCR (cloud job, or a warm local model) ----
    backend = resolve_backend(args)
    token = remote_token(args) if backend == "remote" else ""
    ocr = None
    device = None
    if backend == "local":
        quiet_paddle()
        try:
            from paddleocr import PaddleOCR
        except ImportError:
            print("ERROR: paddleocr is not installed and no cloud token is "
                  "configured. Run `pip install 'paddleocr>=3' "
                  "'paddlex[ocr]'`, or use the remote backend by passing "
                  "--ocr-token / setting PADDLE_OCR_TOKEN.",
                  file=sys.stderr)
            return 1
        device = paddle_device()

    print(f"\n=== Stage 1: OCR ({len(nums)} pages, backend={backend}"
          f"{f', device={device}' if device else ''}) ===", flush=True)
    t0 = time.time()
    pairs = [(image_paths[n], ocr_dir / f"page_{n}.ocr.json")
             for n in nums]
    if backend == "remote":
        counts = run_ocr_remote(pairs, args, token)
    else:
        try:
            ocr = PaddleOCR(
                lang=args.lang,
                device=device,
                use_doc_orientation_classify=False,
                use_doc_unwarping=False,
                use_textline_orientation=False,
                # Emit per-character bboxes so inventory_to_layout.py can
                # do per-character color sampling (in-bbox color changes).
                return_word_box=True,
            )
        except Exception as exc:
            print(f"ERROR: could not start the local PaddleOCR engine: {exc}\n"
                  f"  The local backend needs the `paddlepaddle` runtime "
                  f"(pip install paddlepaddle) or a GPU build.\n"
                  f"  Easiest fix: run the cloud backend instead — pass "
                  f"--ocr-backend remote (or just configure a token with "
                  f"--ocr-token / PADDLE_OCR_TOKEN / `editppt config "
                  f"--paddle-ocr-token`).",
                  file=sys.stderr)
            return 1
        counts = run_ocr_batch(ocr, pairs, args.min_conf)
    print(f"  {backend} OCR done in {time.time() - t0:.1f}s "
          f"({sum(counts)} total detections)", flush=True)

    # ---- Stage 2: cross-verify + review-packet build ----
    print(f"\n=== Stage 2: 3-engine cross-verify ({len(nums)} pages) ===",
          flush=True)
    if args.skip_cross_verify:
        print("  --skip-cross-verify set: all review entries → red",
              flush=True)
    t1 = time.time()
    total_tier = {"green": 0, "yellow": 0, "red": 0}
    total_rescue = {"attempted": 0, "upgraded": 0}
    review_entry_counts = []
    # The 4th-engine rescue needs a recognizer: the warm local model from
    # stage 1, or the cloud service when explicitly allowed (it costs one
    # job per red entry).
    use_rescue = not args.skip_cross_verify
    rescuer = None
    if use_rescue and backend == "remote" and args.remote_crop_rescue:
        rescuer = make_remote_rescuer(args, token)
    elif use_rescue and backend == "remote":
        print("  note: 4th-engine crop rescue disabled on the remote "
              "backend (pass --remote-crop-rescue to enable)", flush=True)
    for n in nums:
        ocr_path = ocr_dir / f"page_{n}.ocr.json"
        image_path = image_paths[n]
        if not ocr_path.exists() or not image_path.exists():
            print(f"  SKIP page {n}: missing inputs", file=sys.stderr)
            review_entry_counts.append(0)
            continue
        review_path = ocr_dir / f"page_{n}.ocr_review.json"
        review = build_review_packet(
            ocr_path, image_path,
            out_path=review_path,
            threshold=args.threshold,
            padding=args.padding,
            max_entries=args.max_entries,
            high_conf=args.high_conf,
            moderate_conf=args.moderate_conf,
            do_cross_verify=not args.skip_cross_verify,
            # Reuse the warm Paddle model from stage 1 for the 4th-engine
            # rescue pass on RED entries; the remote backend uses the
            # cloud rescuer instead.
            paddle_model=ocr if use_rescue else None,
            remote_rescuer=rescuer,
        )
        tc = review.get("tier_counts", {})
        rs = review.get("rescue_stats", {})
        review_entry_counts.append(len(review.get("entries", [])))
        for k in total_tier:
            total_tier[k] += tc.get(k, 0)
        for k in total_rescue:
            total_rescue[k] += rs.get(k, 0)
        rescue_note = (f", paddle-crop rescue: {rs.get('upgraded',0)}/"
                       f"{rs.get('attempted',0)}"
                       if rs.get('attempted', 0) else "")
        print(f"  page {n}: {len(review.get('entries', []))} review entries "
              f"({tc.get('green',0)} 🟢, {tc.get('yellow',0)} 🟡, "
              f"{tc.get('red',0)} 🔴{rescue_note})", flush=True)
    print(f"  cross-verify done in {time.time() - t1:.1f}s "
          f"({sum(total_tier.values())} total entries: "
          f"{total_tier['green']} 🟢, {total_tier['yellow']} 🟡, "
          f"{total_tier['red']} 🔴)", flush=True)
    if total_rescue["attempted"]:
        print(f"  paddle-crop rescue: "
              f"{total_rescue['upgraded']}/{total_rescue['attempted']} "
              f"RED entries upgraded to YELLOW", flush=True)

    # ---- Stage 3: autoclear ----
    total_cleared = 0
    if args.skip_autoclear:
        print("\n=== Stage 3: autoclear skipped (--skip-autoclear) ===")
    else:
        print(f"\n=== Stage 3: autoclear heuristics ({len(nums)} pages) ===",
              flush=True)
        t2 = time.time()
        rule_totals: dict[str, int] = {}
        for n in nums:
            review_path = ocr_dir / f"page_{n}.ocr_review.json"
            if not review_path.exists():
                continue
            cleared, by_rule = apply_autoclear(review_path, image_w, image_h)
            total_cleared += cleared
            for k, v in by_rule.items():
                rule_totals[k] = rule_totals.get(k, 0) + v
            print(f"  page {n}: {cleared} entries auto-cleared", flush=True)
        print(f"  autoclear done in {time.time() - t2:.1f}s "
              f"({total_cleared} entries cleared)", flush=True)
        for rule, c in sorted(rule_totals.items()):
            print(f"    {rule}: {c}")

    # ---- Stage 4: render annotated review images ----
    # The annotated page shows only entries where the agent might still
    # want to act: auto-cleared decoration is hidden (the H1-H6 rules
    # have zero calibration FPs), and entries with corrected_text
    # already empty are decision-complete.
    print(f"\n=== Stage 4: annotated review images ({len(nums)} pages) ===",
          flush=True)
    t3 = time.time()
    rendered = 0
    for n in nums:
        review_path = ocr_dir / f"page_{n}.ocr_review.json"
        image_path = image_paths[n]
        if not review_path.exists() or not image_path.exists():
            continue
        data = json.loads(review_path.read_text(encoding="utf-8"))
        all_entries = data.get("entries", [])
        visible = [
            e for e in all_entries
            if not (e.get("notes") or "").startswith("auto-cleared")
        ]
        if not visible:
            print(f"  page {n}: nothing to annotate "
                  f"({len(all_entries)} auto-cleared, 0 to review)")
            continue
        out_path = ocr_dir / f"page_{n}.ocr_review.annotated.png"
        if render_annotated_page(image_path, visible, out_path):
            rendered += 1
            print(f"  page {n}: wrote {out_path.name} "
                  f"({len(visible)} review entries; "
                  f"{len(all_entries) - len(visible)} auto-cleared hidden)")
    print(f"  rendered {rendered} annotated pages in "
          f"{time.time() - t3:.1f}s", flush=True)

    # ---- Done ----
    total_entries = sum(review_entry_counts)
    print("\n=== Summary ===")
    print(f"  {sum(counts)} OCR detections across {len(nums)} pages")
    print(f"  {total_entries} entries went through 3-engine cross-verify")
    print(f"    {total_tier['green']} 🟢 strong consensus (accept as-is)")
    print(f"    {total_tier['yellow']} 🟡 weak consensus (glance to verify)")
    print(f"    {total_tier['red']} 🔴 no consensus (review carefully)")
    print(f"  {total_cleared} auto-cleared (decorative/logo)")
    remaining = max(0, total_entries - total_cleared)
    print(f"  {remaining} entries have corrected_text pre-filled and ready "
          f"to build")
    print()
    print("=== Next steps ===")
    print(f"  1. scripts/ocr/ocr_review_apply.py --work-dir {work}")
    print(f"     (merges suggested_text → ocr.json, no agent input needed)")
    print(f"  2. scripts/build_deck.py --source-dir {src_dir} "
          f"--work-dir {work}")
    print(f"     (builds the first-draft PPTX)")
    print(f"  3. OPTIONAL: open each ocr/page_NN.ocr_review.annotated.png,")
    print(f"     override any entries you disagree with by editing the")
    print(f"     matching ocr_review.json's corrected_text field, then")
    print(f"     rerun steps 1+2 to rebuild with the corrections.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
