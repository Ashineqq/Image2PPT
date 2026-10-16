#!/usr/bin/env python
"""Single-command OCR preparation with confidence tiering.

Runs PP-OCRv5 over every supported `page_NN.*` image in --source-dir,
always through the Baidu AI Studio cloud service (one job for the whole
batch). For each page:

  1. Runs PP-OCRv5 to get baseline text + bbox + confidence
  2. Builds a review packet for entries with conf < --threshold (default 0.95)
  3. Tiers each review entry by the recognizer's own confidence:
       🟡 yellow — conf ≥ --moderate-conf (default 0.85), very likely right
       🔴 red    — below that; the only tier the crop rescue can help,
                   and the one worth reading carefully
     `--crop-rescue` lets the red entries spend one extra cloud job each
     re-reading the isolated, upscaled crop, which promotes those the crop
     pass reads confidently. suggested_text is pre-filled so the deck
     builds without agent input.
  4. Applies the 6 conservative autoclear heuristics (zero-FP) — these
     override corrected_text to "" for obvious logo/decorative misreads.
  5. Renders ONE annotated PNG per page showing:
       - the source image with every review entry's bbox outlined in
         its tier color and labeled with #N
       - a side legend listing each entry's reading and the suggested text

Outputs:

    inventory/page_NN.ocr.json                    raw OCR detections
    inventory/page_NN.ocr_review.json             review packet (with tier,
                                                  candidates, suggested,
                                                  corrected_text pre-filled)
    inventory/page_NN.ocr_review.annotated.png    full-page annotated
                                                  review image (only
                                                  written when entries
                                                  exist)

The agent step is OPTIONAL: after build_deck.py produces a first-draft
PPTX, the user is asked whether to do a manual review of the annotated
pages. If they say no, the deck ships as-is using the pre-filled
suggestions. If yes, the agent overrides specific entries by editing
corrected_text in the review JSONs, then build_deck.py reruns.

Usage:
    python scripts/ocr/prepare_ocr.py \\
        --source-dir slides/ \\
        --work-dir   output/<name>_<YYYYMMDD>/

Optional flags:
    --pages 1,4,8         only process specified page numbers
    --ocr-token TOKEN     cloud token; defaults to $PADDLE_OCR_TOKEN then
                          ~/.editppt/config.yaml
    --ocr-model PP-OCRv5  cloud model name
    --threshold 0.95      PP-OCRv5 conf threshold for queueing review
    --max-entries 50      cap on review entries per page
    --image-size WxH      slide size used by autoclear (default 1280x720)
    --moderate-conf 0.85  confidence at or above which an entry is yellow
    --crop-rescue         let the crop re-read run on red entries (one
                          cloud job per red entry)
    --skip-autoclear      skip the autoclear pass
"""
from __future__ import annotations

import argparse
import json
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
from ocr_review_autoclear import match_rule, parse_size  # noqa: E402
from ocr_tier import Prediction, classify, crop_rescue  # noqa: E402
from render_annotated_review import render_annotated_page  # noqa: E402


# Crop-rescue upscaling rule: long lines get more pixels per character,
# short ones are left alone because LANCZOS on a few glyphs only adds
# edge artifacts.
CROP_RESCUE_UPSCALE_WIDTH = 200
CROP_RESCUE_UPSCALE_FACTOR = 3

REVIEW_INSTRUCTIONS = (
    "Each entry's `corrected_text` has been pre-filled with the "
    "recognizer's reading. Open the matching "
    "`inventory/page_NN.ocr_review.annotated.png` to see every entry "
    "highlighted on the source slide (color: 🟡 yellow = confident, "
    "🔴 red = low confidence, read it carefully). For each entry you "
    "disagree with, edit `corrected_text` here. Leave it as-is to accept "
    "the suggestion. Set `corrected_text` to \"\" if the detection is not "
    "real editable text (icon glyph, decorative mark)."
)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Batch OCR + confidence tiering + autoclear + "
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
    p.add_argument("--ocr-token", default=None,
                   help="PaddleOCR cloud token; defaults to "
                        "$PADDLE_OCR_TOKEN, then ~/.editppt/config.yaml.")
    p.add_argument("--ocr-model", default=ocr_remote.DEFAULT_MODEL,
                   help=f"Cloud model name (default {ocr_remote.DEFAULT_MODEL}).")
    p.add_argument("--ocr-timeout", type=float,
                   default=ocr_remote.DEFAULT_TIMEOUT,
                   help="Cloud job polling timeout in seconds.")
    p.add_argument("--crop-rescue", action="store_true",
                   help="Re-read red entries from their isolated crop "
                        "(one extra cloud job per red entry — slow).")
    p.add_argument("--min-conf", type=float, default=0.5,
                   help="Minimum recognizer confidence to emit (default 0.5).")
    p.add_argument("--threshold", type=float, default=0.95,
                   help="Review queueing threshold; entries below this are "
                        "tiered for review (default 0.95).")
    p.add_argument("--padding", type=int, default=6,
                   help="Padding around an OCR bbox when cropping for the "
                        "crop rescue (default 6).")
    p.add_argument("--max-entries", type=int, default=50,
                   help="Cap on review entries per page (default 50).")
    p.add_argument("--image-size", default="1280x720",
                   help="Source slide size WxH used by autoclear "
                        "(default 1280x720).")
    p.add_argument("--moderate-conf", type=float, default=0.85,
                   help="Confidence at or above which an entry is tiered "
                        "yellow (default 0.85). Below it, red.")
    p.add_argument("--skip-autoclear", action="store_true",
                   help="Skip the autoclear heuristics pass.")
    return p.parse_args()


def discover_pages(src_dir: Path, pages_arg: str | None) -> list[str]:
    if pages_arg:
        return [n.strip().zfill(2) for n in pages_arg.split(",")]
    return discover_page_numbers(src_dir)


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


def make_crop_rescuer(args: argparse.Namespace, token: str):
    """Build the crop-rescue callable.

    Mirrors the upscaling rule above so every crop is re-read the same
    way. Each call is one cloud job, which is why this stays behind
    --crop-rescue.
    """
    from PIL import Image  # noqa: PLC0415

    def rescuer(crop_image) -> Prediction:
        try:
            if crop_image.width >= CROP_RESCUE_UPSCALE_WIDTH:
                img = crop_image.resize(
                    (crop_image.width * CROP_RESCUE_UPSCALE_FACTOR,
                     crop_image.height * CROP_RESCUE_UPSCALE_FACTOR),
                    Image.LANCZOS,
                )
            else:
                img = crop_image
            text, conf = ocr_remote.run_crop(
                img, token=token, model=args.ocr_model,
                timeout=args.ocr_timeout,
            )
            return Prediction(text, conf)
        except Exception:
            return Prediction("", 0.0)

    return rescuer


def build_review_packet(ocr_path: Path, image_path: Path, *,
                        out_path: Path, threshold: float,
                        padding: int, max_entries: int,
                        moderate_conf: float,
                        rescuer=None) -> dict:
    """Tier the low-confidence entries and write the review packet.

    The review JSON's `entries` look like:
        {
          "idx": int,
          "original_text": str,           # recognizer text
          "confidence": float,            # recognizer conf
          "bbox": [x1, y1, x2, y2],
          "tier": "green" | "yellow" | "red",
          "reason": str,                  # the rule that fired
          "candidates": {
            "primary": {"text": ..., "conf": ...},
            "crop":    {"text": ..., "conf": ...},   # only after a rescue
          },
          "suggested_text": str,
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
    with tempfile.TemporaryDirectory(prefix="tier_") as td:
        td = Path(td)
        for idx, item in candidates_idx:
            x1 = max(0, int(item["x1"]) - padding)
            y1 = max(0, int(item["y1"]) - padding)
            x2 = min(W, int(item["x2"]) + padding)
            y2 = min(H, int(item["y2"]) + padding)
            if x2 <= x1 or y2 <= y1:
                continue

            primary = Prediction(item["text"],
                                 float(item.get("confidence", 0.0)))
            tier = classify(primary, moderate_conf=moderate_conf)

            # The crop re-read only runs on RED entries (the only case
            # where it can change the outcome) and only when the caller
            # supplied a rescuer.
            if tier.tier == "red" and rescuer is not None:
                crop_path = td / f"crop_{idx:04d}.png"
                crop_img = img.crop((x1, y1, x2, y2))
                crop_img.save(crop_path)
                rescue_stats["attempted"] += 1
                tier_after = crop_rescue(
                    tier, rescuer(crop_img), moderate_conf=moderate_conf,
                )
                if tier_after.tier != tier.tier:
                    rescue_stats["upgraded"] += 1
                tier = tier_after

            tier_count[tier.tier] += 1
            entries.append({
                "idx": idx,
                "original_text": item["text"],
                "confidence": float(item.get("confidence", 0.0)),
                "bbox": [int(item["x1"]), int(item["y1"]),
                         int(item["x2"]), int(item["y2"])],
                "tier": tier.tier,
                "reason": tier.reason,
                "candidates": tier.candidates,
                "suggested_text": tier.suggested_text,
                "corrected_text": tier.suggested_text,
                "notes": None,
            })

    review = {
        "_instructions": REVIEW_INSTRUCTIONS,
        "source_ocr": str(ocr_path),
        "source_image": str(image_path),
        "threshold": threshold,
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
    preserved-visual (not editable text), regardless of the suggestion.
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

    # ---- Stage 1: OCR (one cloud job for the whole batch) ----
    token = remote_token(args)
    print(f"\n=== Stage 1: OCR ({len(nums)} pages, backend=remote) ===",
          flush=True)
    t0 = time.time()
    pairs = [(image_paths[n], ocr_dir / f"page_{n}.ocr.json")
             for n in nums]
    counts = run_ocr_remote(pairs, args, token)
    print(f"  remote OCR done in {time.time() - t0:.1f}s "
          f"({sum(counts)} total detections)", flush=True)

    # ---- Stage 2: tier + review-packet build ----
    print(f"\n=== Stage 2: confidence tiering ({len(nums)} pages) ===",
          flush=True)
    t1 = time.time()
    total_tier = {"green": 0, "yellow": 0, "red": 0}
    total_rescue = {"attempted": 0, "upgraded": 0}
    review_entry_counts = []
    rescuer = make_crop_rescuer(args, token) if args.crop_rescue else None
    if not args.crop_rescue:
        print("  note: crop rescue disabled (pass --crop-rescue to enable)",
              flush=True)
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
            moderate_conf=args.moderate_conf,
            rescuer=rescuer,
        )
        tc = review.get("tier_counts", {})
        rs = review.get("rescue_stats", {})
        review_entry_counts.append(len(review.get("entries", [])))
        for k in total_tier:
            total_tier[k] += tc.get(k, 0)
        for k in total_rescue:
            total_rescue[k] += rs.get(k, 0)
        rescue_note = (f", crop rescue: {rs.get('upgraded',0)}/"
                       f"{rs.get('attempted',0)}"
                       if rs.get('attempted', 0) else "")
        print(f"  page {n}: {len(review.get('entries', []))} review entries "
              f"({tc.get('green',0)} 🟢, {tc.get('yellow',0)} 🟡, "
              f"{tc.get('red',0)} 🔴{rescue_note})", flush=True)
    print(f"  tiering done in {time.time() - t1:.1f}s "
          f"({sum(total_tier.values())} total entries: "
          f"{total_tier['green']} 🟢, {total_tier['yellow']} 🟡, "
          f"{total_tier['red']} 🔴)", flush=True)
    if total_rescue["attempted"]:
        print(f"  crop rescue: "
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
    print(f"  {total_entries} entries were tiered for review")
    print(f"    {total_tier['green']} 🟢 unreadable (left as picture)")
    print(f"    {total_tier['yellow']} 🟡 confident (glance to verify)")
    print(f"    {total_tier['red']} 🔴 low confidence (review carefully)")
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
