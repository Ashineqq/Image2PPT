#!/usr/bin/env python
"""Pre-download every model the skill needs, so the first real run isn't
mixed up with cold-cache downloads.

Touches one cache:

1. **RMBG-1.4 ONNX FP16** (~88 MB, cached under
   `~/.cache/huggingface/hub/`). Optional — skip with `--skip-rmbg` if
   you don't plan to run `rmbg_postprocess.py`.

OCR needs no warmup: text recognition runs on the Baidu AI Studio cloud
service, so no Paddle weights are read locally.

Usage:
    python scripts/warmup.py [--skip-rmbg]

Exits non-zero on any failure so a `bootstrap.sh` script can fail fast.
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from shared.gpu import describe as describe_devices  # noqa: E402


def banner(name: str) -> None:
    print(f"\n=== {name} ===", flush=True)


def warmup_rmbg() -> None:
    banner("RMBG-1.4 ONNX")
    try:
        from huggingface_hub import hf_hub_download
        import onnxruntime  # noqa: F401 — just verify it imports.
    except ImportError:
        raise SystemExit(
            "huggingface_hub / onnxruntime not installed. Run "
            "`pip install onnxruntime huggingface_hub` first."
        )
    t0 = time.time()
    repo = os.environ.get("RMBG_REPO", "briaai/RMBG-1.4")
    path = hf_hub_download(repo, "onnx/model_fp16.onnx")
    print(f"  ok ({time.time() - t0:.1f}s)\n  cached at: {path}")


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Pre-download every model the skill uses.")
    p.add_argument("--skip-rmbg", action="store_true",
                   help="Skip RMBG warmup (optional model — only needed "
                        "for rmbg_postprocess.py).")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    print(describe_devices(), flush=True)
    if not args.skip_rmbg:
        try:
            warmup_rmbg()
        except SystemExit:
            raise
        except Exception as exc:
            print(f"  FAIL: {exc}", file=sys.stderr)
            return 1
    banner("done")
    print("All requested models are ready.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
