"""Transparent Pillow annotations, independent of native windows and input."""

from __future__ import annotations

from bisect import bisect_right
from contextlib import closing
from dataclasses import dataclass
from functools import lru_cache
import math
import time

from PIL import Image, ImageDraw, ImageFilter

from desktop_mcp.contracts import Rect
from desktop_mcp.teaching import (
    MAX_MARKS,
    MAX_POINTS,
    Mark,
    TeachingSnapshot,
    WaitTarget,
    _color,
    _laser_bounds,
    _number,
    _point,
    _points,
    _rect,
)

MAX_RENDER_PIXELS = 16_777_216
MAX_RENDER_DIMENSION = 8192
_INK_EDGE_WIDTH = 2.0
_LASER_SPEED = 360.0
_LASER_MIN_PERIOD = 1.2
_LASER_MAX_PERIOD = 3.0
_LASER_TRAIL_LENGTH = 120.0
_XY = tuple[float, float]
_RGBA = tuple[int, int, int, int]


class SceneTooLarge(ValueError):
    """The combined guidance scene cannot fit the bounded native canvas."""


def _check_size(size: tuple[int, int]) -> None:
    if max(size) > MAX_RENDER_DIMENSION or size[0] * size[1] > MAX_RENDER_PIXELS:
        raise SceneTooLarge(
            "The annotation canvas exceeds the bounded renderer size. Erase older marks first."
        )


def _sampling(size: tuple[int, int], maximum: int) -> int:
    while (
        max(size) * maximum > MAX_RENDER_DIMENSION
        or size[0] * size[1] * maximum * maximum > MAX_RENDER_PIXELS
    ):
        maximum //= 2
    return maximum


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
        laser_bounds = _laser_bounds(mark.kind, mark.laser_bounds)
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
                    mark.identifier,
                    mark.kind,
                    points,
                    color,
                    width,
                    created,
                    expiry,
                    mark.context,
                    laser_bounds,
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
        padding = (
            max(12.0, mark.width * 4)
            if mark.kind == "laser"
            else (mark.width + _INK_EDGE_WIDTH) / 2 + 2
        )
        points = (
            (mark.laser_bounds[:2], mark.laser_bounds[2:])
            if mark.laser_bounds is not None
            else mark.points
        )
        xs, ys = zip(*points)
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


def validate_scene(snapshot: TeachingSnapshot, *, now: float, clip: Rect) -> Rect | None:
    """Validate combined physical canvas geometry without allocating an image."""
    bounds = visible_bounds(snapshot, now=now, clip=clip)
    if bounds is not None:
        _check_size((bounds[2] - bounds[0], bounds[3] - bounds[1]))
    return bounds


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


def _stroke(
    image: Image.Image,
    points: tuple[_XY, ...],
    width: float,
    color: _RGBA | int,
    *,
    draw: ImageDraw.ImageDraw | None = None,
) -> None:
    draw = ImageDraw.Draw(image) if draw is None else draw
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


@dataclass(frozen=True)
class _LaserPath:
    points: tuple[_XY, ...]
    cumulative: tuple[float, ...]
    closed: bool

    @property
    def length(self) -> float:
        return self.cumulative[-1]

    @property
    def period(self) -> float:
        return max(_LASER_MIN_PERIOD, min(_LASER_MAX_PERIOD, self.length / _LASER_SPEED))

    def at(self, distance: float) -> _XY:
        if self.length == 0:
            return self.points[0]
        distance = distance % self.length if self.closed else max(0.0, min(self.length, distance))
        index = min(len(self.points) - 2, bisect_right(self.cumulative, distance) - 1)
        ratio = (distance - self.cumulative[index]) / (
            self.cumulative[index + 1] - self.cumulative[index]
        )
        a, b = self.points[index], self.points[index + 1]
        return a[0] + ratio * (b[0] - a[0]), a[1] + ratio * (b[1] - a[1])


@lru_cache(maxsize=MAX_MARKS)
def _laser_path(points: tuple[_XY, ...], bounds: Rect | None = None) -> _LaserPath:
    """Cache only bounded geometry, never a mark's lifetime, context or visibility."""
    if bounds is not None:
        left, top, right, bottom = bounds
        cx, cy = (left + right) / 2, (top + bottom) / 2
        rx, ry = (right - left) / 2, (bottom - top) / 2
        steps = min(MAX_POINTS - 1, max(96, math.ceil(math.tau * max(rx, ry) / 3)))
        points = tuple(
            (
                cx + rx * math.cos(math.tau * index / steps),
                cy + ry * math.sin(math.tau * index / steps),
            )
            for index in range(steps)
        )
        points += (points[0],)
    vertices = [points[0]]
    cumulative = [0.0]
    for point in points[1:]:
        length = math.dist(vertices[-1], point)
        if length:
            vertices.append(point)
            cumulative.append(cumulative[-1] + length)
    return _LaserPath(
        tuple(vertices),
        tuple(cumulative),
        len(vertices) > 2 and vertices[0] == vertices[-1],
    )


def _trail(path: _LaserPath, head: float, length: float) -> tuple[_XY, ...]:
    if path.length == 0 or length <= 0:
        return (path.at(head),)
    tail = head - length if path.closed else max(0.0, head - length)
    length = head - tail
    if length == 0:
        return (path.at(head),)
    # Uniform distance samples keep the fade smooth even on a two-vertex path.
    steps = max(1, min(64, math.ceil(length / 2)))
    distances = {tail + length * index / steps for index in range(steps + 1)}
    for offset in (-path.length, 0.0) if path.closed else (0.0,):
        distances.update(
            distance + offset for distance in path.cumulative if tail < distance + offset < head
        )
    return tuple(path.at(distance) for distance in sorted(distances))


def _ease(value: float) -> float:
    value = max(0.0, min(1.0, value))
    return value * value * (3.0 - 2.0 * value)


def _laser_frame(mark: Mark, now: float) -> tuple[tuple[_XY, ...], float]:
    assert mark.expires_at is not None
    lifetime = mark.expires_at - mark.created_at
    elapsed = max(0.0, now - mark.created_at)
    opacity = _ease(elapsed / min(0.14, lifetime * 0.2)) * _ease(
        (mark.expires_at - now) / min(0.3, lifetime * 0.25)
    )
    path = _laser_path(mark.points, mark.laser_bounds)
    length = min(_LASER_TRAIL_LENGTH, path.length * 0.22)
    if path.closed:
        head = (elapsed / path.period % 1.0) * path.length
    else:
        travel = min(path.period, lifetime * 0.75)
        head = _ease(elapsed / travel) * path.length
        length *= 1.0 - _ease((elapsed - travel) / min(0.2, lifetime * 0.25))
    return _trail(path, head, length), opacity


def _laser(
    canvas: Image.Image,
    mark: Mark,
    bounds: Rect,
    factor: float,
    now: float,
    *,
    sampling: int,
) -> None:
    physical, opacity = _laser_frame(mark, now)
    if opacity == 0:
        return
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
    size = right - left, bottom - top
    sampling = _sampling(size, sampling)
    halo_sampling = max(1, sampling // 2)
    halo_scale = halo_sampling / sampling
    local = tuple(((x - left) * sampling, (y - top) * sampling) for x, y in points)
    halo_points = tuple((x * halo_scale, y * halo_scale) for x, y in local)
    factor *= sampling
    width *= sampling
    cumulative = [0.0]
    for a, b in zip(local, local[1:]):
        cumulative.append(cumulative[-1] + math.dist(a, b))
    rgb = tuple(int(mark.color[index : index + 2], 16) for index in (1, 3, 5))
    with (
        closing(Image.new("L", (size[0] * halo_sampling, size[1] * halo_sampling))) as halo,
        closing(Image.new("RGBA", (size[0] * sampling, size[1] * sampling))) as core,
    ):
        halo_draw, core_draw = ImageDraw.Draw(halo), ImageDraw.Draw(core)
        for index in range(1, len(local)):
            strength = _ease(cumulative[index] / cumulative[-1]) if cumulative[-1] else 1.0
            segment = local[index - 1 : index + 1]
            _stroke(
                halo,
                halo_points[index - 1 : index + 1],
                max(width * 1.8, 4 * factor) * halo_scale,
                round(60 * opacity * strength),
                draw=halo_draw,
            )
            _stroke(
                core,
                segment,
                width * (0.35 + 0.65 * strength),
                (*rgb, round(235 * opacity * strength)),
                draw=core_draw,
            )
        head = local[-1:]
        diameter = max(3.6 * factor, width * 1.4)
        _stroke(
            halo,
            halo_points[-1:],
            diameter * 1.5 * halo_scale,
            round(65 * opacity),
            draw=halo_draw,
        )
        _stroke(core, head, diameter, (*rgb, round(245 * opacity)), draw=core_draw)
        _stroke(
            core,
            head,
            diameter * 0.42,
            (255, 250, 238, round(255 * opacity)),
            draw=core_draw,
        )
        # A lower-resolution alpha-only halo avoids blurring four channels at 4x.
        with (
            closing(
                halo.filter(ImageFilter.GaussianBlur(max(0.7 * factor, width * 0.65) * halo_scale))
            ) as soft,
            closing(Image.new("RGBA", size, (*rgb, 0))) as glow,
        ):
            if halo_sampling > 1:
                with closing(soft.resize(size, Image.Resampling.LANCZOS)) as alpha:
                    glow.putalpha(alpha)
            else:
                glow.putalpha(soft)
            if sampling > 1:
                with closing(core.resize(size, Image.Resampling.LANCZOS)) as sharp:
                    glow.alpha_composite(sharp)
            else:
                glow.alpha_composite(core)
            canvas.alpha_composite(glow, dest=(left, top))


def render_marks(
    snapshot: TeachingSnapshot,
    bounds: Rect,
    *,
    now: float | None = None,
    scale: float = 1.0,
) -> Image.Image:
    """Render an RGBA overlay with a transparent background and bounded allocation.

    Output is limited to 8192 pixels per side and 16,777,216 pixels total.
    Laser patches are supersampled locally; ink uses a bounded supersampled canvas.
    The caller owns the returned image.
    """
    bounds = _rect(bounds)
    scale = _number(scale, "scale", 0.1, 4.0)
    now = _number(time.monotonic() if now is None else now, "now", -math.inf, math.inf)
    size = math.ceil((bounds[2] - bounds[0]) * scale), math.ceil((bounds[3] - bounds[1]) * scale)
    _check_size(size)
    marks, waiting = _scene(snapshot, now)
    sampling = _sampling(
        size, 2 if waiting is not None or any(mark.kind != "laser" for mark in marks) else 1
    )
    factor = scale * sampling
    canvas = Image.new("RGBA", (size[0] * sampling, size[1] * sampling))
    result: Image.Image | None = None
    try:
        for mark in marks:
            if mark.kind == "laser":
                _laser(canvas, mark, bounds, factor, now, sampling=4 // sampling)
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
            _stroke(canvas, points, (mark.width + _INK_EDGE_WIDTH) * factor, (35, 35, 35, 220))
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
