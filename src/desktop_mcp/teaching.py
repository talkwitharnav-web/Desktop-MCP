"""Bounded, in-memory teaching state; never injects input or captures pixels."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
import math
import re
import threading
import time
from typing import TYPE_CHECKING, Literal
from uuid import uuid4

from desktop_mcp.contracts import CaptureContext, ControlSnapshot, Point, Rect
from desktop_mcp.conversation import Conversation, TranscriptEntry
from desktop_mcp.runtime import DesktopStopped

if TYPE_CHECKING:
    from desktop_mcp.runtime import Controller

MAX_MARKS = 64
MAX_POINTS = 512
_CONTEXT_INTERVAL = 0.1
_POLL_INTERVAL = 0.05
_COLOR = re.compile(r"#[0-9a-fA-F]{6}\Z")


@dataclass(frozen=True)
class Mark:
    identifier: str
    kind: str
    points: tuple[Point, ...]
    color: str
    width: float
    created_at: float
    expires_at: float | None
    context: CaptureContext | None
    laser_bounds: Rect | None = None


@dataclass(frozen=True)
class WaitTarget:
    center: Point
    radius: float
    inside: bool
    dwell_progress: float
    elapsed: float


@dataclass(frozen=True)
class TeachingSnapshot:
    revision: int
    entries: tuple[TranscriptEntry, ...]
    marks: tuple[Mark, ...]
    waiting: WaitTarget | None
    cursor: Point | None


@dataclass(frozen=True)
class _MarkState:
    mark: Mark
    generation: int
    input_revision: int
    checked_at: float


@dataclass(frozen=True)
class _WaitState:
    token: object
    generation: int
    input_revision: int
    context: CaptureContext | None = None
    target: WaitTarget | None = None
    checked_at: float = -math.inf
    invalid: str | None = None


def _number(value: object, name: str, low: float, high: float) -> float:
    try:
        result = (
            float(value)
            if isinstance(value, (int, float)) and not isinstance(value, bool)
            else math.nan
        )
    except OverflowError:
        result = math.nan
    if not math.isfinite(result) or not low <= result <= high:
        raise ValueError(f"{name} must be a finite number in {low}..{high}.")
    return result


def _point(value: object) -> Point:
    if (
        not isinstance(value, (tuple, list))
        or len(value) != 2
        or any(
            isinstance(part, bool) or not isinstance(part, int) or not -(2**31) <= part < 2**31
            for part in value
        )
    ):
        raise ValueError("Points must contain two integer physical desktop coordinates.")
    return value[0], value[1]


def _rect(value: object) -> Rect:
    if not isinstance(value, (tuple, list)) or len(value) != 4:
        raise ValueError("Bounds must contain four integer physical desktop coordinates.")
    left, top = _point(value[:2])
    right, bottom = _point(value[2:])
    if left >= right or top >= bottom:
        raise ValueError("Bounds must have positive width and height.")
    return left, top, right, bottom


def _color(value: object) -> str:
    if not isinstance(value, str) or _COLOR.fullmatch(value) is None:
        raise ValueError("color must be a six-digit hexadecimal color such as '#ffb454'.")
    return value.lower()


def _points(kind: object, points: object) -> tuple[Point, ...]:
    if not isinstance(kind, str) or kind not in ("path", "ellipse", "rectangle", "laser"):
        raise ValueError("kind must be path, ellipse, rectangle, or laser.")
    if (
        not isinstance(points, Sequence)
        or isinstance(points, (str, bytes))
        or not 1 <= len(points) <= MAX_POINTS
    ):
        raise ValueError(f"An annotation must contain 1..{MAX_POINTS} points.")
    result = tuple(_point(point) for point in points)
    if kind == "path" and len(result) < 2:
        raise ValueError("A path requires at least two points.")
    if kind in ("ellipse", "rectangle"):
        if len(result) != 2:
            raise ValueError("An ellipse or rectangle requires exactly two corners.")
        if result[0][0] == result[1][0] or result[0][1] == result[1][1]:
            raise ValueError("The two corners must define a positive width and height.")
    return result


def _laser_bounds(kind: str, bounds: object) -> Rect | None:
    if bounds is None:
        return None
    if kind != "laser":
        raise ValueError("Only a laser can carry ellipse bounds.")
    return _rect(bounds)


def _context(value: object) -> CaptureContext:
    if not isinstance(value, CaptureContext):
        raise ValueError("Expected a CaptureContext.")
    if (
        isinstance(value.window_id, bool)
        or not isinstance(value.window_id, int)
        or value.window_id < 0
    ):
        raise ValueError("Context window identifiers must be integers.")
    if not isinstance(value.display_bounds, (tuple, list)):
        raise ValueError("Context display bounds must be a sequence.")
    return replace(
        value,
        bounds=_rect(value.bounds),
        desktop_bounds=_rect(value.desktop_bounds),
        display_bounds=tuple(_rect(rect) for rect in value.display_bounds),
    )


def _context_key(value: CaptureContext) -> tuple[object, ...]:
    return (
        getattr(value, "scope", None),
        value.window_id,
        value.bounds,
        value.desktop_bounds,
        tuple(sorted(value.display_bounds)),
    )


def _in_bounds(points: Sequence[Point], context: CaptureContext) -> None:
    left, top, right, bottom = context.desktop_bounds
    if any(not (left <= x < right and top <= y < bottom) for x, y in points):
        raise ValueError("A target or annotation point is outside the current virtual desktop.")


def _expected_revision(state: ControlSnapshot, revision: int | None) -> None:
    if revision is not None:
        if isinstance(revision, bool) or not isinstance(revision, int) or revision < 0:
            raise ValueError("expected_input_revision must be a nonnegative integer.")
        if state.input_revision != revision:
            raise RuntimeError(
                "Input changed after coordinate mapping. Obtain a fresh observation."
            )


class TeachingSession:
    """Presentation state shared by serialized agent operations and read-only UI frames.

    Mutators require an active controller operation. Snapshot and local clears do
    not. External callbacks are always invoked without the model lock held.
    Annotation contexts are rechecked at most once per context per 100 ms during
    snapshot reads; revocation and input revisions are checked on every read.
    """

    def __init__(
        self,
        controller: Controller,
        *,
        position: Callable[[], Point],
        context: Callable[[CaptureContext | None], CaptureContext | None],
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if not all(callable(callback) for callback in (position, context, clock)):
            raise ValueError("position, context, and clock must be callable.")
        self._controller = controller
        self._position = position
        self._context = context
        self._clock = clock
        self._lock = threading.RLock()
        self.conversation = Conversation(
            is_closed=lambda: self._controller.snapshot().state == "closed", clock=clock
        )
        self._marks: dict[str, _MarkState] = {}
        self._waiting: _WaitState | None = None
        self._cursor: Point | None = None
        self._revision = self._visual_epoch = 0
        self._generation = self._input_revision = -1
        self._last_time = -math.inf

    @staticmethod
    def _check_scene(
        marks: tuple[Mark, ...], waiting: WaitTarget | None, context: CaptureContext, now: float
    ) -> None:
        from desktop_mcp.teaching_render import validate_scene

        validate_scene(
            TeachingSnapshot(0, (), marks, waiting, None), now=now, clip=context.desktop_bounds
        )

    def publish(self, text: str, *, title: str = "Instructions") -> TranscriptEntry:
        """Text conversation remains available while desktop control is paused."""
        return self.conversation.reply(text, title=title)

    def draw(
        self,
        kind: Literal["path", "ellipse", "rectangle", "laser"],
        points: Sequence[Point],
        *,
        color: str = "#ffb454",
        width: float = 3.0,
        lifetime: float | None = None,
        expected_context: CaptureContext | None = None,
        expected_input_revision: int | None = None,
        laser_bounds: Rect | None = None,
    ) -> str:
        """Add an outlined annotation anchored to a currently available context."""
        points = _points(kind, points)
        laser_bounds = _laser_bounds(kind, laser_bounds)
        color = _color(color)
        width = _number(width, "width", 0.5, 32.0)
        if kind == "laser" and lifetime is None:
            lifetime = 2.0
        if lifetime is not None:
            lifetime = _number(lifetime, "lifetime", 0.01, 10.0 if kind == "laser" else 3600.0)
        expected = _context(expected_context) if expected_context is not None else None
        before = self._authorize()
        _expected_revision(before, expected_input_revision)
        now = self._now()
        prepared = self._authorize(generation=before.generation)
        with self._lock:
            self._require_sync(prepared, now)
            epoch = self._visual_epoch
        current = self._read_context(expected)
        after = self._authorize(generation=before.generation)
        if after.input_revision != before.input_revision:
            raise RuntimeError(
                "Physical input changed while preparing the annotation. Observe again."
            )
        if current is None or (
            expected is not None and _context_key(current) != _context_key(expected)
        ):
            raise RuntimeError("The annotation's target context is unavailable or changed.")
        _in_bounds(points, current)
        if laser_bounds is not None:
            _in_bounds((laser_bounds[:2], laser_bounds[2:]), current)
        now, identifier = self._now(), uuid4().hex
        after = self._authorize(generation=before.generation)
        if after.input_revision != before.input_revision:
            raise RuntimeError("Physical input changed before committing the annotation.")
        with self._lock:
            self._require_sync(after, now)
            if epoch != self._visual_epoch:
                raise RuntimeError("Teaching visuals were cleared locally during the update.")
            if len(self._marks) >= MAX_MARKS:
                raise ValueError(f"At most {MAX_MARKS} annotations may exist; erase some first.")
            mark = Mark(
                identifier,
                kind,
                points,
                color,
                width,
                self._last_time,
                self._last_time + lifetime if lifetime is not None else None,
                current,
                laser_bounds,
            )
            self._check_scene(
                (*tuple(item.mark for item in self._marks.values()), mark),
                self._waiting.target if self._waiting is not None else None,
                current,
                self._last_time,
            )
            self._marks[identifier] = _MarkState(
                mark, after.generation, after.input_revision, self._last_time
            )
            self._revision += 1
        accepted = False
        try:
            final = self._authorize(generation=after.generation)
            if final.input_revision != after.input_revision:
                raise RuntimeError("Physical input changed while publishing the annotation.")
            accepted = True
            return identifier
        finally:
            if not accepted:
                with self._lock:
                    if self._marks.pop(identifier, None) is not None:
                        self._revision += 1

    def erase(self, identifier: str | None = None) -> int:
        """Remove only our annotations; an unknown identifier is an explicit error."""
        if identifier is not None and (
            not isinstance(identifier, str) or not identifier.strip() or len(identifier) > 128
        ):
            raise ValueError("identifier must be a nonempty annotation identifier.")
        state, now = self._authorize(), self._now()
        state = self._authorize(generation=state.generation)
        with self._lock:
            self._require_sync(state, now)
            if identifier is None:
                removed = len(self._marks)
                self._marks.clear()
            elif self._marks.pop(identifier, None) is not None:
                removed = 1
            else:
                raise ValueError("The annotation identifier was not found.")
            if removed:
                self._revision += 1
        self._authorize(generation=state.generation)
        return removed

    def cursor_position(self) -> Point:
        """Read the actual injected position getter, never a laser position."""
        state = self._authorize()
        point = self._read_position()
        now = self._now()
        state = self._authorize(generation=state.generation)
        with self._lock:
            self._require_sync(state, now)
            self._set_cursor(point)
        self._authorize(generation=state.generation)
        return point

    def wait_for_cursor(
        self,
        target: Point,
        *,
        radius: float = 28.0,
        dwell: float = 0.25,
        timeout: float = 15.0,
        expected_context: CaptureContext | None = None,
        expected_input_revision: int | None = None,
    ) -> dict[str, object]:
        """Wait for sampled continuous cursor vicinity, never claim a click.

        Polling is bounded. Position/context callbacks must themselves return
        promptly; they cannot be preempted by this pure state model.
        """
        with self._controller.learner_turn():
            return self._wait_for_cursor(
                target,
                radius=radius,
                dwell=dwell,
                timeout=timeout,
                expected_context=expected_context,
                expected_input_revision=expected_input_revision,
            )

    def _wait_for_cursor(
        self,
        target: Point,
        *,
        radius: float,
        dwell: float,
        timeout: float,
        expected_context: CaptureContext | None,
        expected_input_revision: int | None,
    ) -> dict[str, object]:
        target = _point(target)
        radius = _number(radius, "radius", 0.0, 512.0)
        dwell = _number(dwell, "dwell", 0.0, 30.0)
        timeout = _number(timeout, "timeout", 0.0, 30.0)
        expected = _context(expected_context) if expected_context is not None else None
        initial = self._authorize()
        _expected_revision(initial, expected_input_revision)
        started = previous_time = self._now()
        prepared = self._authorize(generation=initial.generation)
        token = object()
        with self._lock:
            self._require_sync(prepared, started)
            if self._waiting is not None:
                raise RuntimeError("A cursor wait is already active.")
            self._waiting = _WaitState(token, initial.generation, initial.input_revision)
        inside_since: float | None = None
        anchor = expected
        checked_at = -math.inf
        try:
            for _ in range(1024):
                self._authorize(generation=initial.generation)
                cursor = self._read_position()
                now = self._wait_time(previous_time)
                previous_time = now
                state = self._authorize(generation=initial.generation)
                status = self._wait_status(token, state, now)
                distance = math.dist(cursor, target)
                inside = distance <= radius
                if not inside:
                    inside_since = None
                elif inside_since is None:
                    inside_since = now
                candidate = inside_since is not None and now >= inside_since + dwell
                if status is None and (
                    anchor is None or now >= checked_at + _CONTEXT_INTERVAL or candidate
                ):
                    current = self._read_context(anchor)
                    checked_at = self._wait_time(previous_time)
                    previous_time = checked_at
                    if current is None or (
                        anchor is not None and _context_key(current) != _context_key(anchor)
                    ):
                        status = "context_changed"
                    else:
                        anchor = current
                        _in_bounds((target,), current)
                candidate = inside_since is not None and previous_time >= inside_since + dwell
                if candidate and status is None:
                    cursor = self._read_position()
                    distance = math.dist(cursor, target)
                    inside = distance <= radius
                    if not inside:
                        inside_since = None
                now = self._wait_time(previous_time)
                previous_time = now
                state = self._authorize(generation=initial.generation)
                status = self._wait_status(token, state, now) or status
                if inside_since is None:
                    progress = 0.0
                elif now >= inside_since + dwell:
                    progress = 1.0
                else:
                    progress = (now - inside_since) / dwell
                elapsed = now - started
                with self._lock:
                    active = self._active_wait(token)
                    status = active.invalid or status
                    self._set_cursor(cursor)
                    if status is None:
                        waiting_target = WaitTarget(target, radius, inside, progress, elapsed)
                        self._check_scene(
                            tuple(item.mark for item in self._marks.values()),
                            waiting_target,
                            anchor,
                            now,
                        )
                        self._waiting = replace(
                            active,
                            context=anchor,
                            checked_at=max(checked_at, active.checked_at),
                            target=waiting_target,
                        )
                        self._revision += 1
                if status is None:
                    if inside and progress >= 1.0 and now <= started + timeout:
                        status = "reached"
                    elif now >= started + timeout:
                        status = "timeout"
                if status is not None:
                    final = self._authorize(generation=initial.generation)
                    now = self._wait_time(previous_time)
                    status = self._wait_status(token, final, now) or status
                    if status == "reached" and now > started + timeout:
                        status = "timeout"
                    return {
                        "status": status,
                        "cursor": list(cursor),
                        "distance": distance,
                        "elapsed": now - started,
                        "radius": radius,
                        "dwell": dwell,
                        "evidence": "cursor_vicinity",
                    }
                delay = min(_POLL_INTERVAL, started + timeout - now)
                if inside_since is not None and inside_since + dwell > now:
                    delay = min(delay, inside_since + dwell - now)
                self._controller.wait(delay)
                after_wait = self._wait_time(now)
                if after_wait <= now:
                    raise RuntimeError("The controller wait did not advance the monotonic clock.")
                previous_time = after_wait
            raise RuntimeError("Cursor polling exceeded its bounded iteration limit.")
        finally:
            with self._lock:
                if self._waiting is not None and self._waiting.token is token:
                    self._waiting = None
                    self._revision += 1

    def snapshot(self) -> TeachingSnapshot:
        """Read safely from a UI thread without an active controller operation."""
        for _ in range(3):
            state = self._controller.snapshot()
            if not state.armed or not state.interface_ready:
                with self._lock:
                    if self._sync(state, self._last_time):
                        return self._snapshot_locked()
                continue
            now = self._now()
            with self._lock:
                if not self._sync(state, now):
                    continue
                marks, waiting = dict(self._marks), self._waiting
                contexts = {
                    _context_key(item.mark.context): item.mark.context
                    for item in marks.values()
                    if item.mark.context is not None and now >= item.checked_at + _CONTEXT_INTERVAL
                }
                if (
                    waiting is not None
                    and waiting.context is not None
                    and waiting.invalid is None
                    and now >= waiting.checked_at + _CONTEXT_INTERVAL
                ):
                    contexts[_context_key(waiting.context)] = waiting.context
            checked: dict[tuple[object, ...], bool] = {}
            for key, expected in contexts.items():
                fresh = self._controller.snapshot()
                if (
                    not fresh.armed
                    or not fresh.interface_ready
                    or fresh.generation != state.generation
                    or fresh.input_revision != state.input_revision
                ):
                    break
                current = self._read_context(expected)
                checked[key] = current is not None and _context_key(current) == key
            fresh = self._controller.snapshot()
            if fresh.armed and fresh.interface_ready:
                now = self._now()
                fresh = self._controller.snapshot()
            with self._lock:
                if not self._sync(fresh, now):
                    continue
                for identifier, old in marks.items():
                    if self._marks.get(identifier) is not old or old.mark.context is None:
                        continue
                    valid = checked.get(_context_key(old.mark.context))
                    if valid is True:
                        self._marks[identifier] = replace(old, checked_at=self._last_time)
                    elif valid is False:
                        del self._marks[identifier]
                        self._revision += 1
                active = self._waiting
                if (
                    waiting is not None
                    and waiting.context is not None
                    and active is not None
                    and active.token is waiting.token
                    and active.invalid is None
                    and active.checked_at <= waiting.checked_at
                ):
                    valid = checked.get(_context_key(waiting.context))
                    if valid is False:
                        self._waiting = replace(active, target=None, invalid="context_changed")
                        self._revision += 1
                    elif valid is True:
                        self._waiting = replace(active, checked_at=self._last_time)
                return self._snapshot_locked()
        raise RuntimeError("Controller state changed repeatedly while reading teaching state.")

    def clear_local(self) -> None:
        """Local UI only: clear annotations and cancel any active vicinity wait."""
        with self._lock:
            self._marks.clear()
            self._waiting = None
            self._visual_epoch += 1
            self._revision += 1

    def clear_transcript_local(self) -> None:
        """Local UI only: erase retained transcript text without requiring arming."""
        self.conversation.clear_local()

    def _authorize(self, *, generation: int | None = None) -> ControlSnapshot:
        ticket = self._controller.snapshot().generation
        self._controller.checkpoint()
        state = self._controller.snapshot()
        if (
            not state.armed
            or not state.interface_ready
            or ticket != state.generation
            or (generation is not None and generation != state.generation)
        ):
            raise DesktopStopped("The teaching operation was revoked.")
        return state

    def _now(self) -> float:
        value = self._clock()
        try:
            return _number(value, "clock", -math.inf, math.inf)
        except ValueError as error:
            raise RuntimeError(
                "The teaching clock must return finite monotonic seconds."
            ) from error

    def _wait_time(self, previous: float) -> float:
        now = self._now()
        if now < previous:
            raise RuntimeError("The teaching clock moved backwards during a cursor wait.")
        return now

    def _read_context(self, expected: CaptureContext | None) -> CaptureContext | None:
        result = self._context(expected)
        if result is None:
            return None
        try:
            return _context(result)
        except ValueError as error:
            raise RuntimeError("The context provider returned invalid geometry.") from error

    def _read_position(self) -> Point:
        result = self._position()
        try:
            return _point(result)
        except ValueError as error:
            raise RuntimeError(
                "The position provider returned invalid physical coordinates."
            ) from error

    def _snapshot_locked(self) -> TeachingSnapshot:
        return TeachingSnapshot(
            self._revision,
            self.conversation.entries(),
            tuple(item.mark for item in self._marks.values()),
            self._waiting.target if self._waiting is not None else None,
            self._cursor,
        )

    def _set_cursor(self, cursor: Point) -> None:
        if cursor != self._cursor:
            self._cursor = cursor
            self._revision += 1

    def _sync(self, state: ControlSnapshot, now: float) -> bool:
        if (state.generation, state.input_revision) < (self._generation, self._input_revision):
            return False
        self._last_time = max(self._last_time, now)
        if state.generation != self._generation or not state.armed or not state.interface_ready:
            if self._marks or self._waiting is not None or state.generation != self._generation:
                self._marks.clear()
                self._waiting = None
                self._visual_epoch += 1
                self._revision += 1
        self._generation, self._input_revision = state.generation, state.input_revision
        for identifier, item in list(self._marks.items()):
            if (item.mark.expires_at is not None and self._last_time >= item.mark.expires_at) or (
                item.mark.context is not None and item.input_revision != state.input_revision
            ):
                del self._marks[identifier]
                self._revision += 1
        if self._waiting is not None and self._waiting.input_revision != state.input_revision:
            if self._waiting.invalid != "input_changed":
                self._waiting = replace(self._waiting, target=None, invalid="input_changed")
                self._revision += 1
        if state.user_cursor is not None:
            self._set_cursor(_point(state.user_cursor))
        return True

    def _require_sync(self, state: ControlSnapshot, now: float) -> None:
        if not self._sync(state, now):
            raise DesktopStopped("A newer controller state revoked this teaching update.")

    def _active_wait(self, token: object) -> _WaitState:
        if self._waiting is None or self._waiting.token is not token:
            raise RuntimeError("The cursor wait was cleared locally.")
        return self._waiting

    def _wait_status(self, token: object, state: ControlSnapshot, now: float) -> str | None:
        with self._lock:
            self._require_sync(state, now)
            return self._active_wait(token).invalid
