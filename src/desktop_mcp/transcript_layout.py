"""Pure, physical-pixel geometry for the native transcript's responsive ribbon."""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Literal

from desktop_mcp.contracts import Rect
from desktop_mcp.transcript_scroll import SCROLLBAR_DIP

PIN, TOP, BOTTOM, CLEAR, STOP = range(201, 206)
SEND, EXPAND, TASKBAR, LATEST = range(206, 210)
HISTORY, STATUS, COMPOSER, HISTORY_LABEL, COMPOSER_LABEL = range(301, 306)
HISTORY_SCROLL, COMPOSER_SCROLL = 306, 307

Dock = Literal["floating", "top", "bottom", "taskbar-edge"]
COMPACT_SIZE = (1120, 184)
EXPANDED_SIZE = (1120, 440)
FONT_SIZES = (12, 14, 16)
FONT_DIP = 14

_PAD, _GAP, _HEADER, _BUTTON, _STATUS = 6, 4, 22, 28, 20
_TOOLBAR = (
    (PIN, 54),
    (TOP, 48),
    (BOTTOM, 70),
    (TASKBAR, 108),
    (CLEAR, 76),
    (EXPAND, 78),
    (STOP, 62),
)


@dataclass(frozen=True)
class TranscriptLayout:
    scale: float
    font_height: int
    split: bool
    controls: dict[int, Rect]
    scrollbar_width: int


def _validate(width: int, height: int, dpi_scale: float) -> None:
    if width <= 0 or height <= 0 or not math.isfinite(dpi_scale) or dpi_scale <= 0:
        raise ValueError("Transcript geometry requires positive dimensions and DPI.")


def _toolbar(width: float) -> tuple[bool, list[list[tuple[int, int]]], float]:
    available = width - 2 * _PAD
    total = sum(size for _, size in _TOOLBAR) + _GAP * (len(_TOOLBAR) - 1)
    if available >= total + 280 + _GAP:
        return True, [list(_TOOLBAR)], _BUTTON
    rows: list[list[tuple[int, int]]] = [[]]
    used = 0
    for identifier, size in _TOOLBAR:
        addition = size + (_GAP if rows[-1] else 0)
        if rows[-1] and used + addition > available:
            rows.append([])
            used = 0
            addition = size
        rows[-1].append((identifier, size))
        used += addition
    height = _STATUS + _GAP + len(rows) * _BUTTON + (len(rows) - 1) * _GAP
    return False, rows, height


def _minimum_height(width: float, *, compact: bool, split: bool) -> float:
    _, _, footer = _toolbar(width)
    content = _HEADER + _GAP + 52
    if not split:
        content += 6 + (42 if compact else _STATUS + _GAP + 62)
    return 2 * _PAD + content + _GAP + footer


def minimum_client_height(width: int, dpi_scale: float, *, compact: bool) -> int:
    """Smallest readable client at this width, reducing scale only on tiny displays."""
    _validate(width, 1, dpi_scale)
    scale = min(dpi_scale, width / 320)
    logical_width = width / scale
    split = compact and logical_width >= 760
    return math.ceil(_minimum_height(logical_width, compact=compact, split=split) * scale)


def layout_client(
    width: int, height: int, dpi_scale: float, *, compact: bool, font_dip: int = FONT_DIP
) -> TranscriptLayout:
    """Lay out every visible HWND without minima extending beyond the actual client."""
    _validate(width, height, dpi_scale)
    if isinstance(font_dip, bool) or font_dip not in FONT_SIZES:
        raise ValueError("Transcript text size must be Small12, Medium14 or Large16 DIP.")
    scale = min(dpi_scale, width / 320)
    split = compact and width / scale >= 760 and height / scale <= 220
    scale = min(scale, height / _minimum_height(width / scale, compact=compact, split=split))
    logical_width, logical_height = width / scale, height / scale
    inner = logical_width - 2 * _PAD
    inline, rows, footer_height = _toolbar(logical_width)
    footer_top = logical_height - _PAD - footer_height
    controls: dict[int, Rect] = {}

    def box(identifier: int, x: float, y: float, w: float, h: float) -> None:
        left = min(width - 1, max(0, round(x * scale)))
        top = min(height - 1, max(0, round(y * scale)))
        right = max(left + 1, min(width, round((x + w) * scale)))
        bottom = max(top + 1, min(height, round((y + h) * scale)))
        controls[identifier] = left, top, right, bottom

    row_y = footer_top if inline else footer_top + _STATUS + _GAP
    for row in rows:
        row_width = sum(size for _, size in row) + _GAP * (len(row) - 1)
        x = logical_width - _PAD - row_width
        if inline:
            box(STATUS, _PAD, footer_top, x - _PAD - _GAP, _BUTTON)
        for identifier, size in row:
            box(identifier, x, row_y, size, _BUTTON)
            x += size + _GAP
        row_y += _BUTTON + _GAP
    if not inline:
        box(STATUS, _PAD, footer_top, inner, _STATUS)

    content_bottom = footer_top - _GAP
    history_top = _PAD + _HEADER + _GAP
    send_width, latest_width = 62, 76
    if split:
        reply_width = min(420, max(280, inner * 0.36))
        history_width = inner - reply_width - 12
        reply_x = _PAD + history_width + 12
        body_height = content_bottom - history_top
        box(HISTORY, _PAD, history_top, history_width, body_height)
        box(COMPOSER_LABEL, reply_x, _PAD, reply_width, _HEADER)
        box(COMPOSER, reply_x, history_top, reply_width - send_width - _GAP, body_height)
        box(SEND, logical_width - _PAD - send_width, history_top, send_width, body_height)
    else:
        history_width = inner
        composer_height = 42 if compact else 62
        composer_top = content_bottom - composer_height
        history_bottom = composer_top - 6
        if not compact:
            history_bottom -= _STATUS + _GAP
            box(COMPOSER_LABEL, _PAD, composer_top - _STATUS - _GAP, inner, _STATUS)
        box(HISTORY, _PAD, history_top, inner, history_bottom - history_top)
        box(COMPOSER, _PAD, composer_top, inner - send_width - _GAP, composer_height)
        box(SEND, logical_width - _PAD - send_width, composer_top, send_width, composer_height)
    box(HISTORY_LABEL, _PAD, _PAD, history_width - latest_width - _GAP, _HEADER)
    box(LATEST, _PAD + history_width - latest_width, _PAD, latest_width, _HEADER)
    scrollbar_width = max(1, round(SCROLLBAR_DIP * scale))
    for editor, scrollbar in ((HISTORY, HISTORY_SCROLL), (COMPOSER, COMPOSER_SCROLL)):
        left, top, right, bottom = controls[editor]
        reserved = min(scrollbar_width, max(1, (right - left - 1) // 2))
        controls[editor] = left, top, right - reserved, bottom
        controls[scrollbar] = right - reserved, top, right, bottom
    return TranscriptLayout(
        scale, max(1, round(font_dip * scale)), split, controls, scrollbar_width
    )


def usable_area(area: Rect, dpi_scale: float, dock: Dock) -> Rect:
    """Normal docks leave a small work-area inset; taskbar-edge explicitly does not."""
    left, top, right, bottom = area
    _validate(right - left, bottom - top, dpi_scale)
    margin = (
        0
        if dock == "taskbar-edge"
        else min(round(8 * dpi_scale), (right - left - 1) // 4, (bottom - top - 1) // 4)
    )
    return left + margin, top + margin, right - margin, bottom - margin


def preferred_size(
    area: Rect, dpi_scale: float, chrome: tuple[int, int], *, compact: bool, dock: Dock
) -> tuple[int, int]:
    left, top, right, bottom = usable_area(area, dpi_scale, dock)
    default = COMPACT_SIZE if compact else EXPANDED_SIZE
    width = min(round(default[0] * dpi_scale), right - left)
    client_width = max(1, width - chrome[0])
    height_dip = default[1]
    if compact and client_width < 760 * dpi_scale:
        height_dip = 216
    height = max(
        round(height_dip * dpi_scale),
        minimum_client_height(client_width, dpi_scale, compact=compact) + chrome[1],
    )
    return width, min(height, bottom - top)


def fit_window(
    rectangle: Rect,
    area: Rect,
    dpi_scale: float,
    *,
    dock: Dock,
    minimum: tuple[int, int] = (1, 1),
) -> Rect:
    """Clamp an outer window rectangle, including fixed minima, to the chosen monitor area."""
    left, top, right, bottom = usable_area(area, dpi_scale, dock)
    x, y, old_right, old_bottom = rectangle
    width = min(right - left, max(1, minimum[0], old_right - x))
    height = min(bottom - top, max(1, minimum[1], old_bottom - y))
    x = max(left, min(x, right - width))
    if dock == "top":
        y = top
    elif dock in {"bottom", "taskbar-edge"}:
        y = bottom - height
    else:
        y = max(top, min(y, bottom - height))
    return x, y, x + width, y + height
