#!/usr/bin/env python
"""Assign a review tier to each low-confidence OCR entry.

OCR runs on a single recognizer — PP-OCRv5 on the Baidu AI Studio cloud
service — so there is no second engine to vote against. A tier therefore
comes from the recognizer's own confidence, plus one optional second pass:

  🟡 YELLOW — the recognizer is at least moderately confident
                (`--moderate-conf`, default 0.85). The reading is very
                likely right; worth a glance.

  🔴 RED    — below that threshold. Worth a careful look, and the only
                tier the crop rescue can help.

Tiers the recognizer read nothing for come back GREEN with an empty
suggestion — a detected box holding no text is decoration, so the deck
builds with it preserved as a picture instead of an empty text box.

For RED entries an optional second pass runs via `crop_rescue()`: the same
recognizer applied to the isolated crop with width-based upscaling. The
full-page and crop passes disagree on dense CJK lines more often than one
would expect, and the crop pass is usually the better of the two, so a
confident crop reading promotes the entry to YELLOW.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, asdict, field


# ---------- text normalization ----------

_PUNCT_MAP = str.maketrans({
    "，": ",", "。": ".", "：": ":", "；": ";",
    "！": "!", "？": "?", "（": "(", "）": ")",
    "【": "[", "】": "]", "「": "[", "」": "]",
    "“": '"', "”": '"', "‘": "'", "’": "'",
    "／": "/", "、": ",", "～": "~",
})


def _normalize(s: str) -> str:
    """NFKC + drop whitespace + unify common CJK/ASCII punctuation + lowercase ASCII."""
    if not s:
        return ""
    s = unicodedata.normalize("NFKC", s)
    s = re.sub(r"\s+", "", s)
    s = s.translate(_PUNCT_MAP)
    # Lowercase if the (now whitespace-free) string is pure ASCII letters/digits/punct.
    if s.isascii():
        s = s.lower()
    return s


@dataclass
class Prediction:
    """One recognizer reading: the text and its reported confidence."""
    text: str
    conf: float


@dataclass
class TierResult:
    tier: str                              # "green" | "yellow" | "red"
    suggested_text: str                    # pre-filled corrected_text
    reason: str                            # short rule name
    candidates: dict[str, dict[str, float | str]] = field(default_factory=dict)
    # candidates: {"primary": {"text": "...", "conf": 0.91},
    #              "crop":    {"text": "...", "conf": 0.88}}   # crop only after a rescue


def classify(primary: Prediction, *, moderate_conf: float = 0.85) -> TierResult:
    """Tier a single recognizer reading.

    `moderate_conf` — confidence at or above which the reading is taken
    to be right (yellow). Below it the entry is red and reviewable.
    """
    candidates = {"primary": {"text": primary.text,
                              "conf": round(primary.conf, 4)}}

    if not (primary.text or "").strip():
        # Detector found a box the recognizer read nothing in — decoration.
        return TierResult(
            tier="green", suggested_text="", reason="no_text",
            candidates=candidates,
        )

    if primary.conf >= moderate_conf:
        return TierResult(
            tier="yellow", suggested_text=primary.text,
            reason="moderate_conf", candidates=candidates,
        )

    return TierResult(
        tier="red", suggested_text=primary.text,
        reason="low_conf", candidates=candidates,
    )


def crop_rescue(initial: TierResult, crop: Prediction,
                *, moderate_conf: float = 0.85) -> TierResult:
    """Second pass on an isolated crop, ONLY for RED-tier results.

    The crop pass sees the same line with more pixels per character and no
    neighbours to suppress against, so it is the better of the two on dense
    CJK text. Rules:

    [C-A] the crop read nothing, or normalizes the same as the full-page
          pass — no new information; keep the tier, record the candidate.
    [C-B] the crop read something else with conf ≥ `moderate_conf` — trust
          it, upgrade RED → YELLOW.
    otherwise keep RED, still recording the crop reading so the annotated
    page surfaces it as a second option for the reviewer.
    """
    if initial.tier != "red":
        return initial  # Only rescue red entries

    new_candidates = dict(initial.candidates)
    new_candidates["crop"] = {"text": crop.text,
                              "conf": round(crop.conf, 4)}

    def _keep_red(reason: str = "") -> TierResult:
        return TierResult(
            tier="red",
            suggested_text=initial.suggested_text,
            reason=reason or initial.reason,
            candidates=new_candidates,
        )

    crop_norm = _normalize(crop.text)
    if not crop_norm:
        return _keep_red()

    # [C-A] same reading as the full-page pass → nothing new.
    primary_norm = _normalize(
        initial.candidates.get("primary", {}).get("text", ""))
    if crop_norm == primary_norm:
        return _keep_red()

    # [C-B] a different, confident reading wins.
    if crop.conf >= moderate_conf:
        return TierResult(
            tier="yellow", suggested_text=crop.text,
            reason="crop_rescue_moderate_conf", candidates=new_candidates,
        )

    return _keep_red()
