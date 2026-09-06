"""Line-based scrolling and thumb geometry, independent of HWNDs and desktop input."""

from __future__ import annotations

from dataclasses import dataclass

SCROLLBAR_DIP = 8
MIN_THUMB_DIP = 24
WHEEL_DELTA = 120
WHEEL_PAGESCROLL = 0xFFFFFFFF


@dataclass(frozen=True)
class ScrollState:
    lines: int
    page: int
    position: int

    @classmethod
    def from_edit(
        cls, lines: int, first_line: int, format_height: int, line_height: int
    ) -> ScrollState:
        lines = max(1, lines)
        page = max(1, max(0, format_height) // max(1, line_height))
        return cls(lines, page, max(0, min(first_line, max(0, lines - page))))

    @property
    def maximum(self) -> int:
        return max(0, self.lines - self.page)

    @property
    def at_end(self) -> bool:
        return self.position >= self.maximum

    @property
    def page_step(self) -> int:
        return max(1, self.page - 1)

    def clamp(self, position: int) -> int:
        return max(0, min(position, self.maximum))


@dataclass(frozen=True)
class Thumb:
    track_top: int
    track_length: int
    top: int
    length: int

    @property
    def bottom(self) -> int:
        return self.top + self.length

    @property
    def travel(self) -> int:
        return self.track_length - self.length

    def grab_fraction(self, y: int) -> float:
        return max(0.0, min(1.0, (y - self.top) / max(1, self.length)))


def thumb_geometry(state: ScrollState, height: int, scale: float) -> Thumb:
    height = max(1, height)
    inset = min(max(0, round(2 * scale)), (height - 1) // 2)
    track = height - 2 * inset
    length = track
    if state.maximum:
        # Keep at least one pixel of travel even when the minimum thumb cannot fit.
        length = min(
            max(1, track - 1),
            max(1, round(MIN_THUMB_DIP * scale), round(track * state.page / state.lines)),
        )
    top = inset + (
        round((track - length) * state.clamp(state.position) / state.maximum)
        if state.maximum
        else 0
    )
    return Thumb(inset, track, top, length)


def dragged_position(
    state: ScrollState,
    thumb: Thumb,
    y: int,
    grab_fraction: float,
    *,
    origin: tuple[int, int] | None = None,
) -> int:
    if not thumb.travel or not state.maximum:
        return state.clamp(state.position)
    top = y - max(0.0, min(1.0, grab_fraction)) * thumb.length
    if origin is not None:
        start_y, start_position = origin
        if y == start_y:
            return state.clamp(start_position)
        if top <= thumb.track_top:
            return 0
        if top >= thumb.track_top + thumb.travel:
            return state.maximum
        # Preserve the exact reading offset instead of inverting a rounded thumb position.
        return state.clamp(start_position + round((y - start_y) * state.maximum / thumb.travel))
    return state.clamp(round((top - thumb.track_top) * state.maximum / thumb.travel))


def wheel_movement(remainder: int, delta: int, lines_per_notch: int, page: int) -> tuple[int, int]:
    total = remainder + delta
    notches = (abs(total) // WHEEL_DELTA) * (1 if total >= 0 else -1)
    step = max(1, page - 1) if lines_per_notch == WHEEL_PAGESCROLL else max(0, lines_per_notch)
    return -notches * step, total - notches * WHEEL_DELTA
