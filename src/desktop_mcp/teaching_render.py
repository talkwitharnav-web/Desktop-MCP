"""Transparent Pillow annotations, independent of native windows and input."""

from __future__ import annotations

from bisect import bisect_right
import math
import time

from PIL import Image, ImageDraw, ImageFilter

from desktop_mcp.contracts import Rect
from desktop_mcp.teaching import (
    MAX_MARKS,
    Mark,
    TeachingSnapshot,
    WaitTarget,
    _color,
    _number,
    _point,
    _points,
    _rect,
)

MAX_RENDER_PIXELS = 16_777_216
MAX_RENDER_DIMENSION = 8192
_XY = tuple[float, float]
_RGBA = tuple[int, int, int, int]


def _scene(snapshot: TeachingSnapshot, now: float) -> tuple[tuple[Mark, ...], WaitTarget | None]:
    if (
        not isinstance(snapshot, TeachingSnapshot)
        or not isinstance(snapshot.marks, (tuple, list))
        or len(snapshot.marks) > MAX_MARKS
    ):
        raise ValueError("Expected a bounded TeachingSnapshot.")
    marks = []
    for mark in snapshot.marks:
        if not isinstance(mark, Mark):
            raise ValueError("The snapshot contains an invalid annotation.")
        points = _points(mark.kind, mark.points)
        color = _color(mark.color)
        width = _number(mark.width, "width", 0.5, 32.0)
        created = _number(mark.created_at, "created_at", -math.inf, math.inf)
        expiry = mark.expires_at
        if expiry is not None:
            expiry = _number(expiry, "expires_at", -math.inf, math.inf)
            if expiry <= created or (mark.kind == "laser" and not 1e-6 <= expiry - created <= 10.0):
                raise ValueError(
                    "Annotation expiry must follow creation within its lifetime limit."
                )
        elif mark.kind == "laser":
            expiry = created + 2.0
        if now >= created and (expiry is None or now < expiry):
            marks.append(
                Mark(
                    mark.identifier, mark.kind, points, color, width, created, expiry, mark.context
                )
            )
    waiting = snapshot.waiting
    if waiting is not None:
        if not isinstance(waiting, WaitTarget) or not isinstance(waiting.inside, bool):
            raise ValueError("The snapshot contains an invalid cursor target.")
        waiting = WaitTarget(
            _point(waiting.center),
            _number(waiting.radius, "radius", 0.0, 512.0),
            waiting.inside,
            _number(waiting.dwell_progress, "dwell_progress", 0.0, 1.0),
            _number(waiting.elapsed, "elapsed", 0.0, math.inf),
        )
    return tuple(marks), waiting


def visible_bounds(
    snapshot: TeachingSnapshot, *, now: float | None = None, clip: Rect | None = None
) -> Rect | None:
    """Return physical bounds including stroke/glow margins, optionally clipped."""
    now = _number(time.monotonic() if now is None else now, "now", -math.inf, math.inf)
    marks, waiting = _scene(snapshot, now)
    regions: list[tuple[float, float, float, float]] = []
    for mark in marks:
        padding = max(12.0, mark.width * 4) if mark.kind == "laser" else mark.width / 2 + 2
        xs, ys = zip(*mark.points)
        regions.append((min(xs) - padding, min(ys) - padding, max(xs) + padding, max(ys) + padding))
    if waiting is not None:
        x, y = waiting.center
        radius = max(2.0, waiting.radius) + 4
        regions.append((x - radius, y - radius, x + radius, y + radius))
    clip = _rect(clip) if clip is not None else None
    if not regions:
        return None
    result = (
        math.floor(min(rect[0] for rect in regions)),
        math.floor(min(rect[1] for rect in regions)),
        math.ceil(max(rect[2] for rect in regions)) + 1,
        math.ceil(max(rect[3] for rect in regions)) + 1,
    )
    if clip is not None:
        result = (
            max(result[0], clip[0]),
            max(result[1], clip[1]),
            min(result[2], clip[2]),
            min(result[3], clip[3]),
        )
    return result if result[0] < result[2] and result[1] < result[3] else None


def _segment(a: _XY, b: _XY, bounds: tuple[float, float, float, float]) -> tuple[_XY, _XY] | None:
    """Clip before rasterization so distant coordinates cannot create huge raster loops."""
    dx, dy = b[0] - a[0], b[1] - a[1]
    start, end = 0.0, 1.0
    for p, q in (
        (-dx, a[0] - bounds[0]),
        (dx, bounds[2] - a[0]),
        (-dy, a[1] - bounds[1]),
        (dy, bounds[3] - a[1]),
    ):
        if p == 0:
            if q < 0:
                return None
        elif p < 0:
            start = max(start, q / p)
        else:
            end = min(end, q / p)
        if start > end:
            return None
    return (a[0] + start * dx, a[1] + start * dy), (a[0] + end * dx, a[1] + end * dy)


def _stroke(image: Image.Image, points: tuple[_XY, ...], width: float, color: _RGBA) -> None:
    draw = ImageDraw.Draw(image)
    radius = max(0.5, width / 2)
    bounds = (-radius, -radius, image.width - 1 + radius, image.height - 1 + radius)
    for a, b in zip(points, points[1:]):
        segment = _segment(a, b, bounds)
        if segment is not None:
            draw.line(segment, fill=color, width=max(1, round(width)))
    for x, y in points:
        if bounds[0] <= x <= bounds[2] and bounds[1] <= y <= bounds[3]:
            draw.ellipse((x - radius, y - radius, x + radius, y + radius), fill=color)


def _ellipse(
    corners: tuple[_XY, _XY], start: float = 0.0, end: float = math.tau
) -> tuple[_XY, ...]:
    a, b = corners
    cx, cy = (a[0] + b[0]) / 2, (a[1] + b[1]) / 2
    rx, ry = abs(a[0] - b[0]) / 2, abs(a[1] - b[1]) / 2
    steps = min(512, max(24, math.ceil(abs(end - start) * max(rx, ry) / 6)))
    return tuple(
        (
            cx + rx * math.cos(start + (end - start) * index / steps),
            cy + ry * math.sin(start + (end - start) * index / steps),
        )
        for index in range(steps + 1)
    )


def _trail(points: tuple[_XY, ...], progress: float) -> tuple[_XY, ...]:
    cumulative = [0.0]
    for a, b in zip(points, points[1:]):
        cumulative.append(cumulative[-1] + math.dist(a, b))
    total = cumulative[-1]
    if total == 0:
        return (points[-1],)
    head = total * progress
    tail = max(0.0, head - min(120.0, max(12.0, total * 0.18)))

    def at(distance: float) -> _XY:
        index = min(len(points) - 2, bisect_right(cumulative, distance) - 1)
        length = cumulative[index + 1] - cumulative[index]
        ratio = (distance - cumulative[index]) / length if length else 0.0
        a, b = points[index], points[index + 1]
        return a[0] + ratio * (b[0] - a[0]), a[1] + ratio * (b[1] - a[1])

    return (
        at(tail),
        *(point for point, distance in zip(points, cumulative) if tail < distance < head),
        at(head),
    )


def _laser(canvas: Image.Image, mark: Mark, bounds: Rect, factor: float, now: float) -> None:
    assert mark.expires_at is not None
    lifetime = mark.expires_at - mark.created_at
    progress = min(1.0, (now - mark.created_at) / (lifetime * 0.75))
    opacity = min(1.0, (mark.expires_at - now) / min(0.5, lifetime * 0.3))
    physical = _trail(mark.points, progress)
    points = tuple(((x - bounds[0]) * factor, (y - bounds[1]) * factor) for x, y in physical)
    width = mark.width * factor
    padding = max(12 * factor, width * 4)
    xs, ys = zip(*points)
    left = max(0, math.floor(min(xs) - padding))
    top = max(0, math.floor(min(ys) - padding))
    right = min(canvas.width, math.ceil(max(xs) + padding) + 1)
    bottom = min(canvas.height, math.ceil(max(ys) + padding) + 1)
    if left >= right or top >= bottom:
        return
    local = tuple((x - left, y - top) for x, y in points)
    rgb = tuple(int(mark.color[index : index + 2], 16) for index in (1, 3, 5))
    with Image.new("RGBA", (right - left, bottom - top)) as glow:
        _stroke(glow, local, max(width * 3, 7 * factor), (*rgb, round(100 * opacity)))
        with glow.filter(ImageFilter.GaussianBlur(max(1.0, width * 0.65))) as soft:
            with Image.new("RGBA", glow.size) as core:
                for index in range(1, len(local)):
                    alpha = round(235 * opacity * (0.2 + 0.8 * index / (len(local) - 1)))
                    _stroke(core, local[index - 1 : index + 1], width, (*rgb, alpha))
                x, y = local[-1]
                radius = max(2 * factor, width * 0.8)
                draw = ImageDraw.Draw(core)
                draw.ellipse(
                    (x - radius, y - radius, x + radius, y + radius),
                    fill=(*rgb, round(235 * opacity)),
                )
                radius *= 0.5
                draw.ellipse(
                    (x - radius, y - radius, x + radius, y + radius),
                    fill=(255, 255, 255, round(255 * opacity)),
                )
                soft.alpha_composite(core)
            canvas.alpha_composite(soft, dest=(left, top))


def render_marks(
    snapshot: TeachingSnapshot,
    bounds: Rect,
    *,
    now: float | None = None,
    scale: float = 1.0,
) -> Image.Image:
    """Render an RGBA overlay with a transparent background and bounded allocation.

    Output is limited to 8192 pixels per side and 16,777,216 pixels total.
    Small overlays are supersampled; large overlays keep the same memory cap.
    The caller owns the returned image.
    """
    bounds = _rect(bounds)
    scale = _number(scale, "scale", 0.1, 4.0)
    now = _number(time.monotonic() if now is None else now, "now", -math.inf, math.inf)
    size = math.ceil((bounds[2] - bounds[0]) * scale), math.ceil((bounds[3] - bounds[1]) * scale)
    if max(size) > MAX_RENDER_DIMENSION or size[0] * size[1] > MAX_RENDER_PIXELS:
        raise ValueError("The annotation canvas exceeds the bounded renderer size.")
    marks, waiting = _scene(snapshot, now)
    sampling = 2 if size[0] * size[1] <= MAX_RENDER_PIXELS // 4 else 1
    factor = scale * sampling
    canvas = Image.new("RGBA", (size[0] * sampling, size[1] * sampling))
    result: Image.Image | None = None
    try:
        for mark in marks:
            if mark.kind == "laser":
                _laser(canvas, mark, bounds, factor, now)
                continue
            points = tuple(
                ((x - bounds[0]) * factor, (y - bounds[1]) * factor) for x, y in mark.points
            )
            if mark.kind == "ellipse":
                points = _ellipse(points)
            elif mark.kind == "rectangle":
                a, b = points
                points = (a, (b[0], a[1]), b, (a[0], b[1]), a)
            rgb = tuple(int(mark.color[index : index + 2], 16) for index in (1, 3, 5))
            _stroke(canvas, points, mark.width * factor, (*rgb, 255))
        if waiting is not None:
            x = (waiting.center[0] - bounds[0]) * factor
            y = (waiting.center[1] - bounds[1]) * factor
            radius = max(2.0, waiting.radius) * factor
            corners = ((x - radius, y - radius), (x + radius, y + radius))
            ring = _ellipse(corners)
            _stroke(canvas, ring, 4 * factor, (35, 35, 35, 220))
            _stroke(canvas, ring, 2 * factor, (255, 180, 84, 235))
            if waiting.inside and waiting.dwell_progress > 0:
                arc = _ellipse(
                    corners, -math.pi / 2, -math.pi / 2 + math.tau * waiting.dwell_progress
                )
                _stroke(canvas, arc, 3 * factor, (255, 255, 255, 255))
        result = canvas.resize(size, Image.Resampling.LANCZOS) if sampling > 1 else canvas
        return result
    finally:
        if result is not canvas:
            canvas.close()
