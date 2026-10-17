"""Snap label stacks whose OCR right edges form a column to right alignment.

A label column next to a bar chart (category names on the left, values on
the right) reads its right edges as the alignment axis: the OCR bboxes all
end at nearly the same x. The layout otherwise emits every box left-aligned
from the OCR left edge, so a font substitution that changes rendered width
fans the right edges out. Detecting the column from the OCR source bboxes
and pinning each box's right edge to the column x keeps the stack flush
regardless of the render font.

Members of one column are also one visual style: same gray, same size.
OCR-height jitter and anti-alias sampling scatter both across members
(`Manufacturing` at 10 pt vs `E-commerce` at 8 pt, a dozen near-gray
hexes), so the column snaps its members to the dominant size and color.
"""
from __future__ import annotations

from collections import Counter


def _has_cjk(text: str) -> bool:
    for ch in text:
        o = ord(ch)
        if 0x4E00 <= o <= 0x9FFF:
            return True
        if 0x3400 <= o <= 0x4DBF:
            return True
        if 0xF900 <= o <= 0xFAFF:
            return True
    return False


def apply_right_aligned_columns(
    text_records: list[dict],
    *,
    min_members: int = 3,
    edge_tol_px: int = 8,
    overlap_frac: float = 0.3,
) -> list[dict]:
    """Right-align stacks of short labels sharing one OCR right edge.

    Only fires on stacks of short single-line labels (<= 16 visible chars,
    no sentence punctuation) whose OCR right edges agree within `edge_tol_px`
    and whose vertical spans overlap the column's band. Each member's box is
    re-anchored so its right edge sits at the column x and its paragraph
    alignment becomes `right`. Everything else is untouched.
    """
    if len(text_records) < min_members:
        return text_records

    def source_right(rec: dict) -> float | None:
        src = rec.get("source_bbox")
        if src and len(src) == 4:
            return float(src[2])
        return None

    def eligible(rec: dict) -> bool:
        if (rec.get("type") or "text") != "text":
            return False
        if str(rec.get("align") or "left").lower() != "left":
            return False
        text = str(rec.get("text") or "").strip()
        visible = [c for c in text if not c.isspace()]
        if not visible or len(visible) > 16:
            return False
        if any(c in text for c in "，。！？；;,:：、/\\()（）[]{}<>《》!?"):
            return False
        # Multi-word phrases are labels; spaced sentences are prose. A
        # single space (e.g. "Retail &") still fits a label column.
        if text.count(" ") > 1:
            return False
        box = rec.get("box")
        if not box or len(box) != 4 or float(box[2]) <= 0:
            return False
        return source_right(rec) is not None

    idx = [i for i, r in enumerate(text_records) if eligible(r)]
    if len(idx) < min_members:
        return text_records

    assigned = [False] * len(text_records)
    out = [dict(r) for r in text_records]
    for pos, i in enumerate(idx):
        if assigned[i]:
            continue
        seed_x = float(source_right(text_records[i]))  # type: ignore[arg-type]
        members = [i]
        for j in idx[pos + 1:]:
            if assigned[j]:
                continue
            rj = float(source_right(text_records[j]))  # type: ignore[arg-type]
            if abs(rj - seed_x) <= edge_tol_px:
                members.append(j)
        if len(members) < min_members:
            continue
        # Vertical spans must overlap into one band, not scatter across the
        # page (e.g. a footer sharing the same right margin).
        tops = sorted(float(out[m]["box"][1]) for m in members)
        bottoms = sorted(float(out[m]["box"][1] + out[m]["box"][3])
                         for m in members)
        band_top, band_bottom = tops[0], bottoms[-1]
        band_h = max(1.0, band_bottom - band_top)
        inside = [
            m for m in members
            if float(out[m]["box"][1]) >= band_top - 0.5 * band_h
            and float(out[m]["box"][1] + out[m]["box"][3]) <= band_bottom + 0.5 * band_h
        ]
        if len(inside) < min_members:
            continue
        col_x = sum(float(source_right(text_records[m]))  # type: ignore[arg-type]
                    for m in inside) / len(inside)
        # One column reads as one style. Snap CJK and Latin members
        # separately to their own median size and dominant color: the two
        # scripts measure differently at the same point size, so a shared
        # snap would shrink one of them. Median (not mode) so a crowded
        # majority cannot drag the column down — sizes converge to the
        # middle, outliers on both ends move inward. Then shrink-wrap:
        # OCR boxes pad ~30 % around the ink, so a size derived from box
        # height overshoots; scale the column size by ink/box width so
        # the rendered ink fits the source ink.
        groups: dict[bool, list[int]] = {True: [], False: []}
        for m in inside:
            groups[_has_cjk(str(out[m].get("text") or ""))].append(m)
        for members in groups.values():
            if not members:
                continue
            sizes = sorted(int(out[m].get("size") or 0) for m in members)
            sizes = [s for s in sizes if s > 0]
            med_size = sizes[len(sizes) // 2] if sizes else None
            ratios = []
            for m in members:
                ink = out[m].get("target_ink")
                box = out[m].get("box")
                if ink and len(ink) == 4 and box and len(box) == 4:
                    ink_w = float(ink[2]) - float(ink[0])
                    box_w = float(box[2])
                    if ink_w > 0 and box_w > 0:
                        ratios.append(ink_w / box_w)
            if ratios and med_size is not None:
                shrink = sum(ratios) / len(ratios)
                med_size = max(6, int(round(med_size * shrink)))
            colors = Counter(str(out[m].get("color") or "") for m in members)
            colors.pop("", None)
            dom_color = colors.most_common(1)[0][0] if colors else None
            for m in members:
                box = list(out[m]["box"])
                new_x = col_x - float(box[2])
                box[0] = round(new_x, 3)
                out[m]["box"] = box
                out[m]["align"] = "right"
                if med_size is not None:
                    out[m]["size"] = med_size
                if dom_color is not None:
                    out[m]["color"] = dom_color
                assigned[m] = True
    return out
