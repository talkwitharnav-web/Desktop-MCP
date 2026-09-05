"""Bounded, on-demand observations without an OS backend or an image-file cache."""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from hashlib import sha256
from io import BytesIO
import math
import time
from typing import Literal
from uuid import uuid4

from PIL import Image

from desktop_mcp.contracts import (
    CaptureContext,
    CaptureProvider,
    CaptureScope,
    Observation,
    Point,
    RawCapture,
    Rect,
)

_MAX_DIMENSION = 4096
_MAX_IMAGE_BYTES = 750_000
_MAX_WAIT = 5.0
_MAX_SETTLE = 1.0
_MIN_POLL = 0.02
_MAX_POLL = 0.25
_MAX_CAPTURES = 256
_MAX_ENCODING_ATTEMPTS = 12


class StaleFrameError(RuntimeError):
    """The observation no longer safely identifies the current desktop context."""


class CaptureError(RuntimeError):
    """A provider result, clock, or image encoder could not produce a valid observation."""


@dataclass(frozen=True)
class _Options:
    scope: CaptureScope
    region: Rect | None
    max_dimension: int
    encoding: Literal["auto", "png", "jpeg"]
    quality: int
    byte_budget: int


@dataclass(frozen=True)
class _Fingerprint:
    bounds: Rect
    context: tuple[object, ...]
    size: Point
    mode: str
    digest: bytes


@dataclass
class _Sample:
    image: Image.Image
    context: CaptureContext
    fingerprint: _Fingerprint
    captured_at: float
    sampled_at: float


@dataclass(frozen=True)
class _ImageDetails:
    size: Point
    requested_size: Point
    encoding: Literal["png", "jpeg"]
    quality: int | None
    byte_count: int
    alpha_flattened: bool

    @property
    def mime_type(self) -> str:
        return f"image/{self.encoding}"


@dataclass(frozen=True)
class _Frame:
    options: _Options
    context: CaptureContext
    fingerprint: _Fingerprint
    captured_at: float
    input_revision: int
    image_details: _ImageDetails
    image_frame_id: str


@dataclass
class _Stats:
    capture_count: int = 0
    context_check_count: int = 0
    changed_samples: int = 0
    encoding_attempts: int = 0
    capture_seconds: float = 0.0
    context_seconds: float = 0.0
    comparison_seconds: float = 0.0
    encoding_seconds: float = 0.0
    wait_seconds: float = 0.0


def _integer(value: object, name: str, minimum: int, maximum: int | None = None) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value < minimum
        or (maximum is not None and value > maximum)
    ):
        limit = f"{minimum}..{maximum}" if maximum is not None else f">= {minimum}"
        raise ValueError(f"{name} must be an integer {limit}.")
    return value


def _number(value: object, name: str, minimum: float, maximum: float | None = None) -> float:
    try:
        number = (
            float(value)
            if not isinstance(value, bool) and isinstance(value, (int, float))
            else math.nan
        )
    except OverflowError:
        number = math.nan
    if not math.isfinite(number) or number < minimum or (maximum is not None and number > maximum):
        limit = f"{minimum}..{maximum}" if maximum is not None else f">= {minimum}"
        raise ValueError(f"{name} must be a finite number {limit}.")
    return number


@contextmanager
def _image_errors(message: str) -> Iterator[None]:
    try:
        yield
    except (OSError, ValueError) as error:
        raise CaptureError(message) from error


def _rect(value: object, name: str) -> Rect:
    if (
        not isinstance(value, (tuple, list))
        or len(value) != 4
        or any(isinstance(part, bool) or not isinstance(part, int) for part in value)
    ):
        raise ValueError(f"{name} must contain four integer physical desktop coordinates.")
    left, top, right, bottom = value
    if left >= right or top >= bottom:
        raise ValueError(f"{name} must have positive width and height.")
    return left, top, right, bottom


def _point(value: object) -> Point:
    if (
        not isinstance(value, (tuple, list))
        or len(value) != 2
        or any(isinstance(part, bool) or not isinstance(part, int) for part in value)
    ):
        raise ValueError("point must contain two integer image-pixel coordinates.")
    return value[0], value[1]


def _frame_id(value: object) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 128:
        raise ValueError("frame_id must be a nonempty observation identifier.")
    return value


def _context(value: object) -> CaptureContext:
    if not isinstance(value, CaptureContext):
        raise CaptureError("The capture provider returned an invalid context.")
    try:
        window_id = _integer(value.window_id, "window_id", 0)
        bounds = _rect(value.bounds, "context bounds")
        desktop = _rect(value.desktop_bounds, "desktop bounds")
        if not isinstance(value.title, str) or not isinstance(value.display_bounds, (tuple, list)):
            raise ValueError("Invalid context fields.")
        if value.scope not in ("active", "desktop"):
            raise ValueError("Invalid context scope.")
        displays = tuple(_rect(rect, "display bounds") for rect in value.display_bounds)
        if any(not _contains(desktop, rect) for rect in displays):
            raise ValueError("Display bounds are outside the virtual desktop.")
    except ValueError as error:
        raise CaptureError("The capture provider returned malformed context geometry.") from error
    return CaptureContext(window_id, bounds, desktop, value.title, displays, value.scope)


def _contains(outer: Rect, inner: Rect) -> bool:
    return (
        outer[0] <= inner[0] < inner[2] <= outer[2] and outer[1] <= inner[1] < inner[3] <= outer[3]
    )


def _context_key(context: CaptureContext) -> tuple[object, ...]:
    # A title edit or a different monitor enumeration order is not a geometry change.
    return (
        context.window_id,
        context.bounds,
        context.desktop_bounds,
        tuple(sorted(context.display_bounds)),
        context.scope,
    )


def _same_context(frame: _Frame, scope: CaptureScope, context: CaptureContext) -> bool:
    original = frame.context
    return (
        original.scope == frame.options.scope
        and context.scope == scope
        and original.window_id == context.window_id
        and original.desktop_bounds == context.desktop_bounds
        and sorted(original.display_bounds) == sorted(context.display_bounds)
        and (frame.options.scope != scope or original.bounds == context.bounds)
    )


def _fit(size: Point, maximum: int) -> Point:
    longest = max(size)
    if longest <= maximum:
        return size
    return (
        max(1, (size[0] * maximum + longest // 2) // longest),
        max(1, (size[1] * maximum + longest // 2) // longest),
    )


def _fingerprint(image: Image.Image, bounds: Rect, context: CaptureContext) -> _Fingerprint:
    digest = sha256(image.tobytes())
    if image.palette is not None:
        mode, palette = image.palette.getdata()
        palette = bytes(palette)
        digest.update(mode.encode("ascii"))
        digest.update(len(palette).to_bytes(8, "big"))
        digest.update(palette)
    if "transparency" in image.info:
        digest.update(repr(image.info["transparency"]).encode("utf-8"))
    return _Fingerprint(bounds, _context_key(context), image.size, image.mode, digest.digest())


class VisionService:
    """Capture, encode, and resolve bounded-lifetime observations.

    The caller serializes this service with input operations. Checkpoints and
    revision checks also reject revocation or input races during expensive work.
    Only full-resolution fingerprints and geometry are cached, never images.

    ``since`` is an image-reuse hint, not an action authorization. Input revision
    changes invalidate actions but retain image fingerprints until expiry,
    eviction, or context invalidation. A returned frame that reuses an image has
    the current revision and is independently actionable, even after the
    original image's frame has been evicted.
    """

    def __init__(
        self,
        source: CaptureProvider,
        *,
        revision: Callable[[], int],
        checkpoint: Callable[[], None],
        wait: Callable[[float], None],
        max_frames: int = 8,
        max_age: float = 60.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._max_frames = _integer(max_frames, "max_frames", 1, 256)
        self._max_age = _number(max_age, "max_age", 0.0)
        if self._max_age == 0:
            raise ValueError("max_age must be greater than zero.")
        for name, callback in (
            ("revision", revision),
            ("checkpoint", checkpoint),
            ("wait", wait),
            ("clock", clock),
            ("source.context", getattr(source, "context", None)),
            ("source.capture", getattr(source, "capture", None)),
        ):
            if not callable(callback):
                raise ValueError(f"{name} must be callable.")
        self._source = source
        self._revision = revision
        self._checkpoint = checkpoint
        self._wait = wait
        self._clock = clock
        self._last_clock: float | None = None
        self._epoch = 0
        self._frames: OrderedDict[str, _Frame] = OrderedDict()

    def observe(
        self,
        *,
        scope: CaptureScope = "active",
        region: Rect | None = None,
        max_dimension: int = 1440,
        encoding: Literal["auto", "png", "jpeg"] = "auto",
        quality: int = 85,
        since: str | None = None,
        wait_for_change: float = 0.0,
        settle: float = 0.06,
    ) -> Observation:
        """Return encoded image bytes or explicitly reuse a compatible prior image.

        ``settle=0`` always takes exactly one capture, even with a change wait.
        Otherwise change waits are limited to five seconds, including settling.
        With no change wait, settling has a budget of four stability intervals,
        capped at one second. Stability is sampled, not a guarantee about every
        intervening pixel. Deadlines limit polling, not blocking provider calls
        or an individual Pillow operation.

        Images fit ``max_dimension`` (1..4096), then a 750,000-byte budget.
        Budget-driven JPEG quality reductions and resizing are explicit in the
        returned metadata; coordinate conversion always uses the actual output.
        """
        if not isinstance(scope, str) or scope not in ("active", "desktop"):
            raise ValueError("scope must be 'active' or 'desktop'.")
        if not isinstance(encoding, str) or encoding not in ("auto", "png", "jpeg"):
            raise ValueError("encoding must be 'auto', 'png', or 'jpeg'.")
        options = _Options(
            scope,
            _rect(region, "region") if region is not None else None,
            _integer(max_dimension, "max_dimension", 1, _MAX_DIMENSION),
            encoding,
            _integer(quality, "quality", 1, 100),
            _MAX_IMAGE_BYTES,
        )
        if since is not None:
            since = _frame_id(since)
        wait_for_change = _number(wait_for_change, "wait_for_change", 0.0, _MAX_WAIT)
        settle = _number(settle, "settle", 0.0, _MAX_SETTLE)
        self._checkpoint()
        revision, epoch = self._read_revision(), self._epoch
        started = self._now()
        prior = self._frames.get(since) if since is not None else None
        self._prune(started)
        stats = _Stats()
        sample: _Sample | None = None
        try:
            sample = self._capture(options, revision, epoch, stats)
            assert sample is not None
            status = self._since_status(since, prior, options, sample)
            baseline = (
                (prior.options.scope, prior.fingerprint)
                if prior is not None and status == "valid"
                else (scope, sample.fingerprint)
            )
            changed = (scope, sample.fingerprint) != baseline
            settled: bool | None = None
            settle_timed_out = False
            timed_out = False
            deadline = started
            active_deadline = started
            if settle > 0:
                settle_budget = min(_MAX_SETTLE, 4 * settle)
                deadline = started + (wait_for_change if wait_for_change > 0 else settle_budget)
                settle_deadline = (
                    min(deadline, sample.sampled_at + settle_budget)
                    if changed or wait_for_change == 0
                    else deadline
                )
                stable_since = sample.sampled_at
                stable_comparisons = 0
                interval = _MIN_POLL if wait_for_change > 0 else min(_MAX_POLL, settle)
                while True:
                    settled = stable_comparisons > 0 and sample.sampled_at >= stable_since + settle
                    waiting_for_change = wait_for_change > 0 and not changed
                    active_deadline = deadline if waiting_for_change else settle_deadline
                    now = self._now()
                    if not waiting_for_change and settled:
                        break
                    if now >= active_deadline:
                        timed_out = waiting_for_change
                        settle_timed_out = not waiting_for_change and not settled
                        break
                    if stats.capture_count >= _MAX_CAPTURES:
                        raise CaptureError("Observation polling exceeded its capture limit.")
                    delay = min(interval, active_deadline - now)
                    if not waiting_for_change:
                        stability_remaining = stable_since + settle - sample.sampled_at
                        if stability_remaining > 0:
                            delay = min(delay, stability_remaining)
                    self._pause(delay, revision, epoch, stats)
                    next_sample = self._capture(
                        options,
                        revision,
                        epoch,
                        stats,
                        expected_context=sample.context,
                        deadline=active_deadline,
                    )
                    if next_sample is None:
                        timed_out = waiting_for_change
                        settle_timed_out = not waiting_for_change and not settled
                        break
                    previous = sample
                    sample = next_sample
                    same_pixels = sample.fingerprint == previous.fingerprint
                    previous.image.close()
                    if same_pixels:
                        stable_comparisons += 1
                        interval = min(_MAX_POLL, max(_MIN_POLL, interval) * 1.8)
                    else:
                        stats.changed_samples += 1
                        stable_since = sample.sampled_at
                        stable_comparisons = 0
                        interval = _MIN_POLL
                    if not changed and (scope, sample.fingerprint) != baseline:
                        changed = True
                        settle_deadline = min(deadline, sample.sampled_at + settle_budget)

            polling_finished = self._now()
            status = self._since_status(since, prior, options, sample)
            reusable = (
                prior is not None
                and status == "valid"
                and prior.options == options
                and prior.fingerprint == sample.fingerprint
            )
            if reusable:
                assert prior is not None
                payload, details = None, prior.image_details
            else:
                payload, details = self._encode(sample.image, options, revision, epoch, stats)
            self._validate_sample(sample, scope, revision, epoch, stats)
            status = self._since_status(since, prior, options, sample)
            if payload is None and status != "valid":
                payload, details = self._encode(sample.image, options, revision, epoch, stats)
                self._validate_sample(sample, scope, revision, epoch, stats)
                status = self._since_status(since, prior, options, sample)

            identifier = uuid4().hex
            image_frame_id = prior.image_frame_id if payload is None and prior else identifier
            frame = _Frame(
                options,
                sample.context,
                sample.fingerprint,
                sample.captured_at,
                revision,
                details,
                image_frame_id,
            )
            now = self._now()
            self._prune(now)
            self._frames[identifier] = frame
            while len(self._frames) > self._max_frames:
                self._frames.popitem(last=False)
            try:
                self._guard(revision, epoch)
                self._check_age(sample.captured_at, self._now())
            except BaseException:
                self._frames.pop(identifier, None)
                raise
            width, height = details.size
            source_width, source_height = sample.fingerprint.size
            wait_status = (
                "single_capture"
                if settle == 0
                else "timeout_no_change"
                if timed_out
                else "change_detected"
                if wait_for_change > 0 and changed
                else "not_requested"
            )
            metadata: dict[str, object] = {
                "frame_id": identifier,
                "scope": scope,
                "window_id": sample.context.window_id,
                "title": sample.context.title,
                "capture_bounds": list(sample.fingerprint.bounds),
                "requested_region": list(options.region) if options.region is not None else None,
                "context_bounds": list(sample.context.bounds),
                "desktop_bounds": list(sample.context.desktop_bounds),
                "display_bounds": [list(rect) for rect in sample.context.display_bounds],
                "original_dimensions": [source_width, source_height],
                "image_dimensions": [width, height],
                "image_width": width,
                "image_height": height,
                "scale_x": source_width / width,
                "scale_y": source_height / height,
                "input_revision": revision,
                "captured_at": sample.captured_at,
                "expires_at": sample.captured_at + self._max_age,
                "image_changed": payload is not None,
                "pixels_changed": (
                    (scope, sample.fingerprint) != (prior.options.scope, prior.fingerprint)
                    if prior is not None and status == "valid"
                    else None
                ),
                "image_frame_id": image_frame_id,
                "reused_from": since if payload is None else None,
                "since_status": status,
                "since_input_revision": prior.input_revision if prior is not None else None,
                "encoding": details.encoding,
                "requested_encoding": encoding,
                "quality": details.quality,
                "requested_quality": quality,
                "max_dimension": max_dimension,
                "encoded_bytes": len(payload) if payload is not None else 0,
                "image_encoded_bytes": details.byte_count,
                "byte_budget": options.byte_budget,
                "budget_downscaled": details.size != details.requested_size,
                "alpha_flattened": details.alpha_flattened,
                "wait_status": wait_status,
                "change_detected": changed,
                "timed_out": timed_out,
                "settled": settled,
                "settle_timed_out": settle_timed_out,
                "wait_for_change": wait_for_change,
                "settle": settle,
                "capture_count": stats.capture_count,
                "poll_count": stats.capture_count - 1,
                "context_check_count": stats.context_check_count,
                "changed_samples": stats.changed_samples,
                "encoding_attempts": stats.encoding_attempts,
                "poll_deadline_overrun_seconds": (
                    max(0.0, polling_finished - active_deadline) if settle > 0 else 0.0
                ),
                "timings": {
                    "capture_seconds": stats.capture_seconds,
                    "context_seconds": stats.context_seconds,
                    "comparison_seconds": stats.comparison_seconds,
                    "encoding_seconds": stats.encoding_seconds,
                    "wait_seconds": stats.wait_seconds,
                    "polling_seconds": polling_finished - started,
                    "total_seconds": self._now() - started,
                },
            }
            return Observation(identifier, metadata, payload, details.mime_type)
        finally:
            if sample is not None:
                sample.image.close()

    def resolve(self, frame_id: str, point: Point) -> Point:
        """Map an in-image integer point to physical pixels after freshness checks."""
        return self.resolve_many(frame_id, (point,))[0]

    def resolve_many(self, frame_id: str, points: Sequence[Point]) -> list[Point]:
        """Validate one frame context for a whole path instead of once per vertex."""
        if not 1 <= len(points) <= 512:
            raise ValueError("Provide between 1 and 512 image points.")
        points = [_point(point) for point in points]
        epoch = self._epoch
        frame = self._require_frame(_frame_id(frame_id))
        mapped = [self._physical_point(frame, point) for point in points]
        self._guard(frame.input_revision, epoch)
        return mapped

    @staticmethod
    def _physical_point(frame: _Frame, point: Point) -> Point:
        width, height = frame.image_details.size
        x, y = point
        if not (0 <= x < width and 0 <= y < height):
            raise ValueError("point is outside the observation image.")
        left, top, right, bottom = frame.fingerprint.bounds
        # Integer arithmetic floors the positive offset, even for negative desktop origins.
        physical = left + x * (right - left) // width, top + y * (bottom - top) // height
        if frame.context.display_bounds and not any(
            rect[0] <= physical[0] < rect[2] and rect[1] <= physical[1] < rect[3]
            for rect in frame.context.display_bounds
        ):
            raise ValueError("point maps to a gap between connected monitors.")
        return physical

    def context_for(self, frame_id: str) -> CaptureContext:
        """Return the original full context, never the crop, after freshness checks."""
        return self._require_frame(_frame_id(frame_id)).context

    def invalidate(self) -> None:
        """Invalidate cached and in-flight references without accessing the desktop."""
        self._epoch += 1
        self._frames.clear()

    def _read_revision(self) -> int:
        revision = self._revision()
        if isinstance(revision, bool) or not isinstance(revision, int) or revision < 0:
            raise RuntimeError("The input revision callback must return a nonnegative integer.")
        return revision

    def _guard(self, revision: int, epoch: int) -> None:
        self._checkpoint()
        if self._read_revision() != revision:
            raise StaleFrameError("Input changed during observation. Request a fresh frame.")
        if self._epoch != epoch:
            raise StaleFrameError("The observation was invalidated. Request a fresh frame.")

    def _now(self) -> float:
        try:
            value = _number(self._clock(), "clock", -math.inf)
        except ValueError as error:
            raise CaptureError(
                "The observation clock must return finite monotonic seconds."
            ) from error
        if self._last_clock is not None and value < self._last_clock:
            raise CaptureError("The observation clock must return finite monotonic seconds.")
        self._last_clock = value
        return value

    def _check_age(self, captured_at: float, now: float) -> None:
        if now >= captured_at + self._max_age:
            raise StaleFrameError("The observation expired. Request a fresh frame.")

    def _prune(self, now: float) -> None:
        for identifier, frame in list(self._frames.items()):
            if now >= frame.captured_at + self._max_age:
                del self._frames[identifier]

    def _prune_context(self, scope: CaptureScope, context: CaptureContext) -> None:
        for identifier, frame in list(self._frames.items()):
            if not _same_context(frame, scope, context):
                del self._frames[identifier]

    def _read_context(self, scope: CaptureScope, stats: _Stats | None = None) -> CaptureContext:
        started = self._now()
        context = _context(self._source.context(scope))
        if context.scope != scope:
            raise CaptureError("The capture provider returned the wrong context scope.")
        if stats is not None:
            stats.context_check_count += 1
            stats.context_seconds += self._now() - started
        return context

    def _since_status(
        self,
        since: str | None,
        frame: _Frame | None,
        options: _Options,
        sample: _Sample,
    ) -> str:
        if since is None:
            return "not_provided"
        if frame is None:
            return "unknown_or_evicted"
        if self._now() >= frame.captured_at + self._max_age:
            return "expired"
        if not _same_context(frame, options.scope, sample.context):
            return "context_changed"
        if self._frames.get(since) is not frame:
            return "unknown_or_evicted"
        return "valid"

    def _require_frame(self, identifier: str) -> _Frame:
        self._checkpoint()
        revision, epoch = self._read_revision(), self._epoch
        frame = self._frames.get(identifier)
        if frame is None:
            raise StaleFrameError("The frame is unknown, invalidated, or evicted. Observe again.")
        now = self._now()
        self._prune(now)
        if frame.input_revision != revision:
            raise StaleFrameError("Input changed after this observation. Request a fresh frame.")
        self._check_age(frame.captured_at, now)
        context = self._read_context(frame.options.scope)
        self._guard(revision, epoch)
        self._prune_context(frame.options.scope, context)
        if not _same_context(frame, frame.options.scope, context):
            raise StaleFrameError("The window or display layout changed. Request a fresh frame.")
        self._check_age(frame.captured_at, self._now())
        return frame

    def _pause(self, seconds: float, revision: int, epoch: int, stats: _Stats) -> None:
        self._guard(revision, epoch)
        started = self._now()
        self._wait(seconds)
        self._guard(revision, epoch)
        elapsed = self._now() - started
        if elapsed <= 0:
            raise CaptureError("The wait callback did not advance the monotonic clock.")
        stats.wait_seconds += elapsed

    def _capture(
        self,
        options: _Options,
        revision: int,
        epoch: int,
        stats: _Stats,
        *,
        expected_context: CaptureContext | None = None,
        deadline: float | None = None,
    ) -> _Sample | None:
        self._guard(revision, epoch)
        if deadline is not None and self._now() > deadline:
            return None
        before = self._read_context(options.scope, stats)
        self._guard(revision, epoch)
        self._prune_context(options.scope, before)
        if expected_context is not None and _context_key(before) != _context_key(expected_context):
            raise StaleFrameError(
                "The window or display layout changed while waiting. Observe again."
            )
        started = self._now()
        if deadline is not None and started > deadline:
            return None
        stats.capture_count += 1
        raw = self._source.capture(scope=options.scope, region=options.region)
        stats.capture_seconds += self._now() - started
        self._guard(revision, epoch)
        if not isinstance(raw, RawCapture) or not isinstance(raw.image, Image.Image):
            raise CaptureError("The capture provider did not return a Pillow image.")
        context = _context(raw.context)
        try:
            bounds = _rect(raw.bounds, "capture bounds")
            captured_at = _number(raw.captured_at, "captured_at", -math.inf)
        except ValueError as error:
            raise CaptureError(
                "The capture provider returned malformed capture metadata."
            ) from error
        if not _contains(context.desktop_bounds, bounds) or raw.image.size != (
            bounds[2] - bounds[0],
            bounds[3] - bounds[1],
        ):
            raise CaptureError(
                "The captured image dimensions do not match physical capture bounds."
            )
        after = self._read_context(options.scope, stats)
        self._guard(revision, epoch)
        self._prune_context(options.scope, after)
        if _context_key(before) != _context_key(context) or _context_key(after) != _context_key(
            context
        ):
            raise StaleFrameError(
                "The window or display layout changed during capture. Observe again."
            )
        now = self._now()
        if captured_at > now:
            raise CaptureError("The capture timestamp is ahead of the observation clock.")
        self._check_age(captured_at, now)
        image: Image.Image | None = None
        try:
            started = self._now()
            with _image_errors("The capture provider returned an unreadable image."):
                image = raw.image.copy()
                fingerprint = _fingerprint(image, bounds, context)
            stats.comparison_seconds += self._now() - started
            self._guard(revision, epoch)
            return _Sample(image, context, fingerprint, captured_at, self._now())
        except BaseException:
            if image is not None:
                image.close()
            raise

    def _validate_sample(
        self, sample: _Sample, scope: CaptureScope, revision: int, epoch: int, stats: _Stats
    ) -> None:
        self._guard(revision, epoch)
        current = self._read_context(scope, stats)
        self._guard(revision, epoch)
        self._prune_context(scope, current)
        if _context_key(current) != _context_key(sample.context):
            raise StaleFrameError(
                "The window or display layout changed before delivery. Observe again."
            )
        self._check_age(sample.captured_at, self._now())

    def _encode(
        self,
        image: Image.Image,
        options: _Options,
        revision: int,
        epoch: int,
        stats: _Stats,
    ) -> tuple[bytes, _ImageDetails]:
        started = self._now()
        base: Image.Image | None = None
        try:
            self._guard(revision, epoch)
            with _image_errors("The captured image could not be encoded."):
                has_alpha = (
                    "A" in image.getbands()
                    or "transparency" in image.info
                    or (image.palette is not None and image.palette.mode == "RGBA")
                )
                base = image.convert("RGBA" if has_alpha else "RGB")
                base.info.clear()
                requested_size = _fit(image.size, options.max_dimension)
                if base.size != requested_size:
                    resized = base.resize(
                        requested_size, Image.Resampling.LANCZOS, reducing_gap=3.0
                    )
                    base.close()
                    base = resized
                if has_alpha:
                    with base.getchannel("A") as alpha:
                        opaque = alpha.getextrema() == (255, 255)
                    if opaque:
                        converted = base.convert("RGB")
                        base.close()
                        base = converted
                        has_alpha = False
                codec = options.encoding
                if codec == "auto":
                    codec = (
                        "png" if has_alpha or base.getcolors(maxcolors=256) is not None else "jpeg"
                    )
                alpha_flattened = has_alpha and codec == "jpeg"
                if alpha_flattened:
                    converted = Image.new("RGB", base.size, "white")
                    with base.getchannel("A") as alpha:
                        converted.paste(base, mask=alpha)
                    base.close()
                    base = converted
                    has_alpha = False
            size = base.size
            quality = options.quality
            for _ in range(_MAX_ENCODING_ATTEMPTS):
                self._guard(revision, epoch)
                with _image_errors("The captured image could not be encoded."):
                    working = (
                        base
                        if size == base.size
                        else base.resize(size, Image.Resampling.LANCZOS, reducing_gap=3.0)
                    )
                try:
                    with (
                        _image_errors("The captured image could not be encoded."),
                        BytesIO() as buffer,
                    ):
                        stats.encoding_attempts += 1
                        if codec == "png":
                            working.save(buffer, format="PNG", compress_level=3, optimize=False)
                        else:
                            # No chroma subsampling: keep colored UI text and thin edges legible.
                            working.save(
                                buffer,
                                format="JPEG",
                                quality=quality,
                                subsampling=0,
                                optimize=False,
                            )
                        payload = buffer.getvalue()
                finally:
                    if working is not base:
                        working.close()
                self._guard(revision, epoch)
                if len(payload) <= options.byte_budget:
                    return payload, _ImageDetails(
                        size,
                        requested_size,
                        codec,
                        quality if codec == "jpeg" else None,
                        len(payload),
                        alpha_flattened,
                    )
                if codec == "png" and options.encoding == "auto" and not has_alpha:
                    codec = "jpeg"
                    continue
                if codec == "jpeg" and quality > min(options.quality, 70):
                    quality = min(options.quality, 70)
                    continue
                if max(size) == 1:
                    break
                factor = min(0.9, math.sqrt(options.byte_budget / len(payload)) * 0.9)
                size = _fit(base.size, max(1, int(max(size) * factor)))
            raise CaptureError(
                "The image cannot fit the encoded byte budget. Request a smaller crop."
            )
        finally:
            if base is not None:
                base.close()
            stats.encoding_seconds += self._now() - started
