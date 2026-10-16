#!/usr/bin/env python
"""PaddleOCR (PP-OCRv5) over the Baidu AI Studio cloud API.

Output contract: a JSON list of
`{text, x1, y1, x2, y2, confidence}` objects in SOURCE-IMAGE pixel
coordinates (plus `words`/`word_boxes`, and `chars`/`char_boxes` for
pure-CJK lines, when the service returns per-word boxes). Downstream
`erase_text.py`, `build_inventory.py` and `inventory_to_layout.py` only
see that shape.

The service runs PP-OCRv5 itself, so no model weights are downloaded and
no PaddlePaddle runtime is needed — no ~85 MB of PaddleX weights, no
per-page CPU cost.
`--model PaddleOCR-VL-1.6` is also accepted, but that model answers with
LAYOUT BLOCKS (one box per paragraph, multi-line content joined without
separators), not per-line records, and downstream layout depends on one
box per line. Keep the default.

Single-page mode (writes JSON to stdout):
    python scripts/ocr/ocr_remote.py slide.jpg > ocr.json

Batch mode (ONE job for every page — the pages are packed into a
multi-page PDF and uploaded together, which is what makes this fast):
    python scripts/ocr/ocr_remote.py --batch \
        page_01.png  out/page_01.ocr.json \
        page_02.webp out/page_02.ocr.json \
        ...

Token resolution order: --token, then $PADDLE_OCR_TOKEN, then
`PADDLE_OCR_TOKEN` in ~/.editppt/config.yaml (so a token configured for
the image-to-editable-ppt skill is picked up automatically). Apply for a
free token at https://aistudio.baidu.com/account/accessToken.
"""
from __future__ import annotations

import argparse
import email.utils
import datetime
import json
import os
import sys
import tempfile
import time
from pathlib import Path

import requests

SCRIPTS = Path(__file__).resolve().parent.parent
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

JOB_URL = "https://paddleocr.aistudio-app.com/api/v2/ocr/jobs"
DEFAULT_MODEL = "PP-OCRv5"
DEFAULT_TIMEOUT = 900
POLL_INTERVAL = 3.0
CONFIG_PATH = Path.home() / ".editppt" / "config.yaml"
MAX_HTTP_ATTEMPTS = 6


class RemoteOCRError(RuntimeError):
    """Any failure that makes the remote backend unusable for this run."""


# --------------------------------------------------------------------------
# token / HTTP plumbing
# --------------------------------------------------------------------------

def resolve_token(explicit: str | None = None) -> str:
    """--token → $PADDLE_OCR_TOKEN → ~/.editppt/config.yaml."""
    if explicit and explicit.strip():
        return explicit.strip()
    env = os.environ.get("PADDLE_OCR_TOKEN", "").strip()
    if env:
        return env
    token = read_config_token()
    if token:
        return token
    raise RemoteOCRError(
        "No PaddleOCR token. Pass --token, set PADDLE_OCR_TOKEN, or run "
        "`editppt config --paddle-ocr-token <token>`. Apply for a free "
        "token at https://aistudio.baidu.com/account/accessToken"
    )


def read_config_token(path: Path | None = None) -> str:
    """Read PADDLE_OCR_TOKEN out of the shared editppt config file.

    Deliberately a tiny parser rather than a yaml import: the file is a
    flat `KEY: value` list and this must not add a dependency.
    """
    cfg = path or CONFIG_PATH
    try:
        text = cfg.read_text(encoding="utf-8")
    except OSError:
        return ""
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("PADDLE_OCR_TOKEN"):
            _, _, value = stripped.partition(":")
            return value.strip().strip("'\"")
    return ""


def _date_header() -> dict[str, str]:
    """The result URL lives on BOS (bcebos), which rejects requests
    without an HTTP `date` header — requests never sends one by itself."""
    now = datetime.datetime.now(datetime.timezone.utc)
    return {"date": email.utils.format_datetime(now, usegmt=True)}


def _request(session: requests.Session, method: str, url: str,
             **kwargs) -> requests.Response:
    """One HTTP call with backoff on transport errors.

    The AI Studio endpoints reset connections under load often enough
    that a single un-retried poll would abort a whole run.
    """
    last: Exception | None = None
    for attempt in range(MAX_HTTP_ATTEMPTS):
        try:
            return session.request(method, url, **kwargs)
        except requests.RequestException as exc:
            last = exc
            if attempt == MAX_HTTP_ATTEMPTS - 1:
                break
            time.sleep(min(2 ** attempt, 15))
    raise RemoteOCRError(f"HTTP {method} {url} failed after "
                         f"{MAX_HTTP_ATTEMPTS} attempts: {last}")


def _session(token: str) -> requests.Session:
    s = requests.Session()
    s.headers.update({"Authorization": f"bearer {token}"})
    return s


def submit_job(session: requests.Session, file_path: Path, model: str,
               payload: dict) -> str:
    with file_path.open("rb") as handle:
        response = _request(
            session, "POST", JOB_URL,
            data={"model": model, "optionalPayload": json.dumps(payload)},
            files={"file": handle},
            timeout=300,
        )
    if response.status_code != 200:
        raise RemoteOCRError(
            f"OCR job submit failed ({response.status_code}): "
            f"{response.text[:300]}"
        )
    body = response.json()
    if body.get("code") != 0:
        raise RemoteOCRError(f"OCR job submit rejected: {body.get('msg')}")
    return body["data"]["jobId"]


def poll_job(session: requests.Session, job_id: str, timeout: float) -> dict:
    started = time.time()
    while True:
        status = _request(session, "GET", f"{JOB_URL}/{job_id}",
                          timeout=120).json()["data"]
        state = status["state"]
        if state == "done":
            return status
        if state == "failed":
            raise RemoteOCRError(
                f"OCR job failed: {status.get('errorMsg') or status}"
            )
        if time.time() - started > timeout:
            raise RemoteOCRError(
                f"OCR job timed out after {timeout:.0f}s (state={state})"
            )
        time.sleep(POLL_INTERVAL)


def fetch_result(session: requests.Session, url: str) -> dict:
    response = _request(session, "GET", url, headers=_date_header(),
                        timeout=300)
    if response.status_code != 200:
        raise RemoteOCRError(
            f"Result download failed ({response.status_code}): "
            f"{response.text[:200]}"
        )
    lines = [ln for ln in response.text.strip().split("\n") if ln.strip()]
    if not lines:
        raise RemoteOCRError("OCR result payload was empty")
    return json.loads(lines[0])


# --------------------------------------------------------------------------
# payload → item contract
# --------------------------------------------------------------------------

def _page_dims(data_info: dict, index: int) -> tuple[float, float]:
    """Per-page OCR-space size, for inputs whose pages get re-rendered.

    Image uploads come back at source resolution; multi-page PDF uploads
    come back at the service's own render scale (2x in practice), so
    every box has to be mapped back onto the source pixel grid.
    """
    pages = data_info.get("pages")
    if isinstance(pages, list) and index < len(pages):
        page = pages[index]
        return float(page["width"]), float(page["height"])
    return float(data_info["width"]), float(data_info["height"])


def extract_items(pruned: dict, min_conf: float,
                  scale_x: float = 1.0, scale_y: float = 1.0,
                  bounds: tuple[int, int] | None = None) -> list[dict]:
    """Build the OCR item shape the rest of the pipeline consumes.

    `scale_x`/`scale_y` map OCR-space coordinates onto source pixels;
    `bounds` clips the result to the source image size (the service may
    return boxes reaching a pixel past the edge).
    """
    items: list[dict] = []
    boxes = pruned.get("rec_boxes")
    polys = pruned.get("rec_polys") or pruned.get("dt_polys") or []
    texts = pruned.get("rec_texts") or []
    scores = pruned.get("rec_scores") or [1.0] * len(texts)
    text_words = pruned.get("text_word") or []
    text_word_boxes = pruned.get("text_word_boxes") or []

    for i, (text, score) in enumerate(zip(texts, scores)):
        if float(score) < min_conf or not text or not text.strip():
            continue
        if boxes is not None and i < len(boxes):
            bx = boxes[i]
            x1, y1, x2, y2 = (int(bx[0]), int(bx[1]), int(bx[2]), int(bx[3]))
        elif i < len(polys):
            xs = [int(p[0]) for p in polys[i]]
            ys = [int(p[1]) for p in polys[i]]
            x1, y1, x2, y2 = min(xs), min(ys), max(xs), max(ys)
        else:
            continue

        if scale_x != 1.0 or scale_y != 1.0:
            x1 = int(round(x1 * scale_x))
            y1 = int(round(y1 * scale_y))
            x2 = int(round(x2 * scale_x))
            y2 = int(round(y2 * scale_y))
        if bounds is not None:
            width, height = bounds
            x1, y1 = max(0, x1), max(0, y1)
            x2, y2 = min(width, x2), min(height, y2)
        if x2 <= x1 or y2 <= y1:
            continue

        item = {
            "text": text,
            "x1": x1, "y1": y1, "x2": x2, "y2": y2,
            "confidence": float(score),
        }
        # Per-segment data, only attached when the words join back into
        # the line text — same guard the local backend uses, so
        # downstream word-aligned reflow can trust the alignment.
        if i < len(text_words) and i < len(text_word_boxes):
            words = list(text_words[i])
            word_boxes_i = text_word_boxes[i]
            if len(words) == len(word_boxes_i) and "".join(words) == text:
                word_boxes = [
                    [int(b[0]), int(b[1]), int(b[2]), int(b[3])]
                    for b in word_boxes_i
                ]
                if scale_x != 1.0 or scale_y != 1.0:
                    word_boxes = [
                        [int(round(b[0] * scale_x)), int(round(b[1] * scale_y)),
                         int(round(b[2] * scale_x)), int(round(b[3] * scale_y))]
                        for b in word_boxes
                    ]
                item["words"] = words
                item["word_boxes"] = word_boxes
                if all(len(w) == 1 for w in words):
                    item["chars"] = words
                    item["char_boxes"] = word_boxes
        items.append(item)

    items.sort(key=lambda it: (it["y1"], it["x1"]))
    return items


def ocr_results(result: dict, key: str = "ocrResults") -> list[dict]:
    """The `prunedResult` for each page, in submitted page order.

    `ocrResults` is what the line-level PP-OCRv5 model answers with;
    `PP-StructureV3` answers with `layoutParsingResults` instead, which
    the table backend reads.
    """
    pages = result.get("result", {}).get(key) or []
    if not pages:
        raise RemoteOCRError(
            f"OCR response carried no {key} (wrong model? response keys: "
            f"{list(result.get('result', {}).keys())})"
        )
    return [page["prunedResult"] for page in pages]


# --------------------------------------------------------------------------
# page packing
# --------------------------------------------------------------------------

def build_pdf(page_paths: list[Path], out_path: Path) -> None:
    """Pack page images into one lossless multi-page PDF.

    Images are embedded as their original PNG/JPEG streams and each page
    rect is the image's pixel size, so nothing is resampled or
    re-encoded on the way to the service.
    """
    import fitz  # PyMuPDF

    doc = fitz.open()
    try:
        for path in page_paths:
            with fitz.open(path) as image_doc:
                rect = image_doc[0].rect
                page = doc.new_page(width=rect.width, height=rect.height)
                page.insert_image(rect, stream=path.read_bytes())
        doc.save(str(out_path), deflate=True, garbage=3)
    finally:
        doc.close()


def source_size(path: Path) -> tuple[int, int]:
    from PIL import Image

    with Image.open(path) as img:
        return img.size


DEFAULT_PAYLOAD = {
    # Same switch set the local backend pins: no document
    # orientation/unwarping (they deskew and report boxes in a
    # transformed grid that no longer matches the source pixels) and no
    # text-line rotation, but DO return per-word boxes so downstream can
    # do per-character colour sampling and mixed-size runs.
    "useDocOrientationClassify": False,
    "useDocUnwarping": False,
    "useTextlineOrientation": False,
    "returnWordBox": True,
}


def run_pages(page_paths: list[Path], *, token: str, model: str = DEFAULT_MODEL,
              min_conf: float = 0.5, timeout: float = DEFAULT_TIMEOUT,
              payload: dict | None = None) -> list[list[dict]]:
    """OCR every page with ONE job; return one item list per page.

    A single page is uploaded as an image; two or more are packed into a
    multi-page PDF so the job count stays at one regardless of page
    count.
    """
    if not page_paths:
        return []
    session = _session(token)
    options = dict(payload or DEFAULT_PAYLOAD)

    upload = page_paths[0]
    temp_pdf: Path | None = None
    try:
        if len(page_paths) > 1:
            fd, name = tempfile.mkstemp(prefix="deckweaver_ocr_", suffix=".pdf")
            os.close(fd)
            temp_pdf = Path(name)
            build_pdf(page_paths, temp_pdf)
            upload = temp_pdf

        job_id = submit_job(session, upload, model, options)
        print(f"  remote job {job_id} ({len(page_paths)} page(s), "
              f"model={model})", flush=True)
        status = poll_job(session, job_id, timeout)
        result = fetch_result(session, status["resultUrl"]["jsonUrl"])
    finally:
        if temp_pdf is not None:
            temp_pdf.unlink(missing_ok=True)

    payloads = ocr_results(result)
    if len(payloads) != len(page_paths):
        raise RemoteOCRError(
            f"OCR returned {len(payloads)} pages for {len(page_paths)} "
            f"submitted pages; refusing to guess the mapping"
        )

    data_info = result.get("result", {}).get("dataInfo", {})
    out: list[list[dict]] = []
    for index, (path, pruned) in enumerate(zip(page_paths, payloads)):
        width, height = source_size(path)
        ocr_w, ocr_h = _page_dims(data_info, index)
        items = extract_items(
            pruned, min_conf,
            scale_x=width / ocr_w if ocr_w else 1.0,
            scale_y=height / ocr_h if ocr_h else 1.0,
            bounds=(width, height),
        )
        out.append(items)
    return out


def run_crop(image, *, token: str, model: str = DEFAULT_MODEL,
             min_conf: float = 0.0, timeout: float = DEFAULT_TIMEOUT) -> tuple[str, float]:
    """OCR a single in-memory crop (PIL image) → (text, confidence).

    One job per crop, so callers should keep this to a small number of
    regions.
    """
    fd, name = tempfile.mkstemp(prefix="deckweaver_crop_", suffix=".png")
    os.close(fd)
    path = Path(name)
    try:
        image.save(path)
        items = run_pages([path], token=token, model=model,
                          min_conf=min_conf, timeout=timeout)[0]
    finally:
        path.unlink(missing_ok=True)
    if not items:
        return "", 0.0
    return ("".join(i["text"] for i in items),
            max(float(i["confidence"]) for i in items))


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Remote PP-OCRv5 (Baidu AI Studio) → JSON.")
    parser.add_argument("inputs", nargs="*",
                        help="Single mode: one image path. Batch mode "
                             "(with --batch): pairs of image_path "
                             "out_json_path.")
    parser.add_argument("--batch", action="store_true",
                        help="Batch mode — `inputs` is read as "
                             "(image, out_json) pairs, and all pages are "
                             "submitted as one job.")
    parser.add_argument("--min-conf", type=float, default=0.5,
                        help="Drop detections below this confidence "
                             "(default 0.5, matches prepare_ocr.py).")
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--token", default=None,
                        help="API token; defaults to $PADDLE_OCR_TOKEN "
                             "then ~/.editppt/config.yaml.")
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT,
                        help="Job polling timeout in seconds.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        token = resolve_token(args.token)
    except RemoteOCRError as exc:
        sys.stderr.write(f"ERROR: {exc}\n")
        return 1

    if args.batch:
        if len(args.inputs) % 2 != 0:
            sys.stderr.write("ERROR: --batch expects an even number of "
                             "arguments (image, out_json pairs).\n")
            return 2
        pairs = [(Path(args.inputs[i]), Path(args.inputs[i + 1]))
                 for i in range(0, len(args.inputs), 2)]
        todo = [(img, out) for img, out in pairs if img.exists()]
        for img, _ in pairs:
            if not img.exists():
                sys.stderr.write(f"SKIP missing: {img}\n")
        if not todo:
            return 0
        t0 = time.time()
        try:
            results = run_pages([img for img, _ in todo], token=token,
                                model=args.model, min_conf=args.min_conf,
                                timeout=args.timeout)
        except RemoteOCRError as exc:
            sys.stderr.write(f"FAIL remote OCR: {exc}\n")
            return 1
        for (img, out), items in zip(todo, results):
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(json.dumps(items, ensure_ascii=False, indent=2),
                           encoding="utf-8")
            print(f"ok {img.name} -> {out.name} ({len(items)} items)",
                  flush=True)
        print(f"remote OCR done in {time.time() - t0:.1f}s", flush=True)
        return 0

    if len(args.inputs) != 1:
        sys.stderr.write("ERROR: single mode expects exactly one image "
                         "path; use --batch for multi-page input.\n")
        return 2
    image_path = Path(args.inputs[0])
    if not image_path.exists():
        sys.stderr.write(f"ERROR: image not found: {image_path}\n")
        return 1
    try:
        items = run_pages([image_path], token=token, model=args.model,
                          min_conf=args.min_conf, timeout=args.timeout)[0]
    except RemoteOCRError as exc:
        sys.stderr.write(f"FAIL remote OCR: {exc}\n")
        return 1
    print(json.dumps(items, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
