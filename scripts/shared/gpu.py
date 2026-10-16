"""Centralized GPU detection for the ONNX Runtime backend used in the
pipeline (the font classifier and RMBG).

The detection result is gated by the ``DECKWEAVER_DEVICE`` env var:

  - ``auto`` (default): use GPU when ONNX Runtime reports it as
    available at import time.
  - ``gpu``: same as auto today — the request goes through; if no
    supported provider is visible it falls back to CPU.
  - ``cpu``: force CPU regardless of what's available.

Results are cached per process — the probes import heavy frameworks, and
we never want to repeat that work mid-run.
"""
from __future__ import annotations

import os
from functools import lru_cache


def _mode() -> str:
    m = os.environ.get("DECKWEAVER_DEVICE", "auto").strip().lower()
    return m if m in {"auto", "gpu", "cpu"} else "auto"


@lru_cache(maxsize=1)
def onnx_providers() -> list[str]:
    """Preferred-first list of execution providers for
    ``onnxruntime.InferenceSession(..., providers=...)``.

    Order: CUDA → CoreML (macOS) → CPU. We filter against
    ``ort.get_available_providers()`` so unsupported entries never reach
    the session constructor (which would otherwise raise)."""
    mode = _mode()
    providers: list[str] = []
    if mode != "cpu":
        try:
            import onnxruntime as ort  # type: ignore
            available = set(ort.get_available_providers())
            if "CUDAExecutionProvider" in available:
                providers.append("CUDAExecutionProvider")
            if "CoreMLExecutionProvider" in available:
                providers.append("CoreMLExecutionProvider")
        except Exception:
            pass
    providers.append("CPUExecutionProvider")
    return providers


def describe() -> str:
    """One-line human-readable summary, useful for startup logs."""
    return (f"DECKWEAVER_DEVICE={_mode()} "
            f"onnx_providers={','.join(onnx_providers())}")
