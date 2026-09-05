"""Serial, generation-revocable desktop operations with owned-input cleanup."""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import replace
from typing import Literal, Protocol, TypeVar

from desktop_mcp.actions import Action, Button, ease_motion, key_code, motion_duration
from desktop_mcp.contracts import ControlSnapshot, Point

logger = logging.getLogger(__name__)
T = TypeVar("T")
_request_ticket: ContextVar[tuple[object, int] | None] = ContextVar(
    "desktop_mcp_request_ticket", default=None
)


class DesktopStopped(RuntimeError):
    """The local controller has revoked this operation."""


class InputNotAllowed(RuntimeError):
    """Input cannot run while an active cursor wait gives the learner a turn."""


class BatchInterrupted(RuntimeError):
    """An input batch failed after some of its actions may have completed."""

    def __init__(self, completed: int, cause: Exception) -> None:
        super().__init__(
            f"Batch stopped after {completed} completed action(s): {cause}. "
            "The current action may be partially applied. "
            "Do not blindly replay input; obtain a fresh observation."
        )
        self.completed = completed


class InputBackend(Protocol):
    def position(self) -> Point: ...
    def foreground(self) -> int: ...
    def validate_point(self, point: Point) -> None: ...
    def ensure_target(self, point: Point | None = None, window_id: int | None = None) -> None: ...
    def move(self, point: Point) -> None: ...
    def button(self, button: Button, down: bool) -> None: ...
    def key(self, code: int, down: bool) -> None: ...
    def text(self, text: str) -> None: ...
    def wheel(self, delta_x: int, delta_y: int) -> None: ...
    def release_pending(self) -> None: ...


class Controller:
    """The only supervised path to physical input, shared with the local UI."""

    def __init__(
        self,
        backend: InputBackend,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.backend = backend
        self._clock = clock
        self._state = ControlSnapshot("stopped", "Allow desktop control in the local window.")
        self._state_lock = threading.RLock()
        self._input_lock = threading.RLock()
        self._sequence_lock = threading.RLock()
        self._local = threading.local()
        self._stopped = threading.Event()
        self._stopped.set()
        self._keys: set[int] = set()
        self._buttons: set[Button] = set()
        self._stopping = 0
        self._deferred_stops = 0
        self._release_thread: threading.Thread | None = None

    def snapshot(self) -> ControlSnapshot:
        """Read status without waiting for a running desktop sequence."""
        with self._state_lock:
            return self._state

    @property
    def input_revision(self) -> int:
        return self.snapshot().input_revision

    def set_interface_ready(self, ready: bool, error: str | None = None) -> None:
        """A missing control window or global hotkey fails closed."""
        if not ready:
            self.stop(error or "The local control interface is unavailable.")
        with self._state_lock:
            closed = self._state.state == "closed"
            self._state = replace(
                self._state,
                interface_ready=ready and not closed,
                last_error=error or self._state.last_error,
                state="error" if error and not closed else self._state.state,
            )

    def arm_local(self) -> None:
        """Local UI only; this method is deliberately not an MCP tool."""
        if not self._input_lock.acquire(blocking=False):
            raise DesktopStopped("Input is still finishing. Wait before resuming locally.")
        try:
            with self._state_lock:
                if not self._state.interface_ready or self._state.state == "closed":
                    raise DesktopStopped("The control window and stop hotkey must be available.")
                if self._stopping:
                    raise DesktopStopped("A stop is still releasing input; wait before resuming.")
                if self._state.armed:
                    return
                generation = self._state.generation
            errors = self._release_inputs()
            if errors:
                self._record_release_error(errors)
                raise DesktopStopped("Owned input could not be released; control cannot resume.")
            with self._state_lock:
                if (
                    self._state.generation != generation
                    or not self._state.interface_ready
                    or self._state.state == "closed"
                    or self._stopping
                    or self._keys
                    or self._buttons
                ):
                    raise DesktopStopped(
                        "Control was stopped while resuming. Resume again locally."
                    )
                self._state = replace(
                    self._state,
                    state="ready",
                    reason="Ready to guide and act. Ctrl+Shift+H stops the session.",
                    action=None,
                    input_active=False,
                    awaiting_user=False,
                    generation=self._state.generation + 1,
                    input_revision=self._state.input_revision + 1,
                    last_error=None,
                )
                self._stopped.clear()
        finally:
            self._input_lock.release()

    def stop(self, reason: str = "Stopped locally") -> None:
        """Revoke first, then release only inputs owned by this controller."""
        with self._state_lock:
            self._stopping += 1
            self._state = replace(
                self._state,
                state="closed" if self._state.state == "closed" else "stopped",
                reason=reason,
                action=None,
                input_active=False,
                awaiting_user=False,
                generation=self._state.generation + 1,
                input_revision=self._state.input_revision + 1,
            )
            self._stopped.set()
        # SendInput can wait for the UI thread's low-level hook. Never make that
        # same UI thread wait for an emitter's lock while handling the stop hotkey.
        if self._input_lock.acquire(blocking=False):
            try:
                self._finish_stop_release(1)
            finally:
                self._input_lock.release()
        else:
            with self._state_lock:
                self._deferred_stops += 1
                if self._release_thread is None:
                    self._release_thread = threading.Thread(
                        target=self._drain_stop_releases,
                        name="Desktop-MCP input release",
                        daemon=True,
                    )
                    self._release_thread.start()

    def _finish_stop_release(self, count: int) -> None:
        try:
            errors = self._release_inputs()
            if errors:
                self._record_release_error(errors)
        finally:
            with self._state_lock:
                self._stopping -= count

    def _drain_stop_releases(self) -> None:
        while True:
            with self._state_lock:
                count = self._deferred_stops
                self._deferred_stops = 0
                if not count:
                    self._release_thread = None
                    return
            self._finish_stop_release(count)

    def close(self) -> None:
        self.stop("Desktop-MCP has shut down.")
        with self._state_lock:
            self._state = replace(self._state, state="closed", interface_ready=False)
            release_thread = self._release_thread
        if release_thread is not None and release_thread is not threading.current_thread():
            release_thread.join(timeout=2.0)
            if release_thread.is_alive():
                raise RuntimeError("Input release did not finish during shutdown.")

    def set_human_takeover(self, enabled: bool) -> None:
        with self._state_lock:
            self._state = replace(self._state, human_takeover=enabled)

    def notify_human_input(
        self, *, kind: Literal["move", "button", "key"] = "move", position: Point | None = None
    ) -> None:
        if kind not in {"move", "button", "key"}:
            raise ValueError("Unknown physical input kind.")
        with self._state_lock:
            status = self._state
            self._state = replace(
                status,
                user_cursor=status.user_cursor if position is None else position,
                input_revision=status.input_revision + int(status.armed and kind != "move"),
            )
            interrupt = status.armed and status.input_active and status.human_takeover
        if interrupt:
            self.stop("Paused because you interrupted automated input.")

    def _check_generation(self, generation: int) -> None:
        status = self.snapshot()
        if generation != status.generation:
            raise DesktopStopped(
                "This operation was revoked. Re-arming does not resume previously queued work."
            )
        if not status.armed or not status.interface_ready:
            raise DesktopStopped(status.reason)

    def checkpoint(self) -> None:
        generation = getattr(self._local, "generation", None)
        if generation is None:
            raise RuntimeError("Desktop access requires an active controller operation.")
        self._check_generation(generation)

    @contextmanager
    def request(self) -> Iterator[None]:
        """Stamp an RPC before it can wait in the tool runner's worker queue."""
        generation = self.snapshot().generation
        self._check_generation(generation)
        token = _request_ticket.set((self, generation))
        try:
            yield
        finally:
            _request_ticket.reset(token)

    def wait(self, duration: float) -> None:
        """Wait in short, interruptible slices without throttling text."""
        deadline = self._clock() + duration
        self.checkpoint()
        while (remaining := deadline - self._clock()) > 0:
            self._stopped.wait(min(remaining, 0.02))
            self.checkpoint()

    @contextmanager
    def operation(self, label: str) -> Iterator[None]:
        """Serialize capture/input and reject commands queued before a stop."""
        if getattr(self._local, "generation", None) is not None:
            self.checkpoint()
            yield
            return
        ticket = _request_ticket.get()
        generation = (
            ticket[1] if ticket is not None and ticket[0] is self else self.snapshot().generation
        )
        self._check_generation(generation)
        while not self._sequence_lock.acquire(timeout=0.02):
            self._check_generation(generation)
        self._local.generation = generation
        try:
            self.checkpoint()
            with self._state_lock:
                self._check_generation(generation)
                self._state = replace(self._state, state="running", action=label)
            yield
            self.checkpoint()
        finally:
            try:
                errors = self._release_inputs()
                if errors:
                    self._record_release_error(errors)
                    raise RuntimeError("Failed to release owned input; desktop control is stopped.")
            finally:
                with self._state_lock:
                    if self._state.generation == generation and self._state.state == "running":
                        self._state = replace(self._state, state="ready", action=None)
                del self._local.generation
                self._sequence_lock.release()

    def _record_release_error(self, errors: list[Exception]) -> None:
        message = "Could not release all owned keys/buttons; control remains stopped."
        with self._state_lock:
            self._state = replace(
                self._state,
                state="closed" if self._state.state == "closed" else "error",
                reason=message,
                last_error=message,
                input_active=False,
                awaiting_user=False,
                generation=self._state.generation + 1,
            )
            self._stopped.set()
        for error in errors:
            logger.error("Owned-input release failed: %s", error)

    def _release_inputs(self) -> list[Exception]:
        errors: list[Exception] = []
        with self._input_lock:
            for button in tuple(self._buttons):
                try:
                    self.backend.button(button, False)
                    self._buttons.remove(button)
                    self._bump_revision()
                except OSError as error:
                    errors.append(error)
            for code in tuple(self._keys):
                try:
                    self.backend.key(code, False)
                    self._keys.remove(code)
                    self._bump_revision()
                except OSError as error:
                    errors.append(error)
            try:
                self.backend.release_pending()
            except OSError as error:
                errors.append(error)
        return errors

    def _bump_revision(self) -> None:
        with self._state_lock:
            self._state = replace(self._state, input_revision=self._state.input_revision + 1)

    @contextmanager
    def _input_activity(self) -> Iterator[None]:
        self.checkpoint()
        depth = getattr(self._local, "input_depth", 0)
        generation = self._local.generation
        with self._state_lock:
            self.checkpoint()
            if self._state.awaiting_user:
                raise InputNotAllowed("Wait for the learner's turn to finish before sending input.")
            if not depth:
                self._state = replace(self._state, input_active=True)
        self._local.input_depth = depth + 1
        try:
            yield
        finally:
            self._local.input_depth = depth
            if not depth:
                with self._state_lock:
                    if self._state.generation == generation:
                        self._state = replace(self._state, input_active=False)

    @contextmanager
    def learner_turn(self) -> Iterator[None]:
        """An automatic, bounded wait gives the real pointer to the learner."""
        self.checkpoint()
        generation = self._local.generation
        with self._input_lock, self._state_lock:
            self.checkpoint()
            if self._state.input_active or self._keys or self._buttons:
                raise InputNotAllowed("Release automated input before waiting for the learner.")
            if self._state.awaiting_user:
                raise RuntimeError("A learner turn is already active.")
            self._state = replace(self._state, awaiting_user=True)
        try:
            yield
        finally:
            with self._state_lock:
                if self._state.generation == generation:
                    self._state = replace(self._state, awaiting_user=False)

    def emit(self, callback: Callable[[], T]) -> T:
        """A short critical section closes the stop-versus-new-input race."""
        with self._input_activity(), self._input_lock:
            self.checkpoint()
            result = callback()
            with self._state_lock:
                self._state = replace(self._state, input_revision=self._state.input_revision + 1)
            return result

    def _key(self, code: int, down: bool) -> None:
        with self._input_lock:
            if down:
                self.checkpoint()
                self.backend.ensure_target(window_id=getattr(self._local, "window_id", None))
                if code in self._keys:
                    return
                chord = self._keys | {code}
                if 0x48 in chord and chord & {0x10, 0xA0, 0xA1} and chord & {0x11, 0xA2, 0xA3}:
                    self.stop("Ctrl+Shift+H emergency stop.")
                    raise DesktopStopped("Ctrl+Shift+H emergency stop.")

                def press_key() -> None:
                    self._keys.add(code)
                    self.backend.key(code, True)

                self.emit(press_key)
            elif code in self._keys:
                self.backend.key(code, False)
                self._keys.remove(code)
                with self._state_lock:
                    self._state = replace(
                        self._state, input_revision=self._state.input_revision + 1
                    )

    def _button(self, button: Button, down: bool) -> None:
        with self._input_lock:
            if down:
                self.checkpoint()
                self.backend.ensure_target(
                    self.backend.position(), getattr(self._local, "window_id", None)
                )
                if button in self._buttons:
                    raise ValueError(f"The {button} button is already held in this batch.")

                def press_button() -> None:
                    self._buttons.add(button)
                    self.backend.button(button, True)

                self.emit(press_button)
            elif button in self._buttons:
                self.backend.button(button, False)
                self._buttons.remove(button)
                with self._state_lock:
                    self._state = replace(
                        self._state, input_revision=self._state.input_revision + 1
                    )

    @contextmanager
    def _modifiers(self, keys: Sequence[str]) -> Iterator[None]:
        codes = [key_code(key) for key in keys]
        new_codes = [code for code in codes if code not in self._keys]
        try:
            for code in new_codes:
                self._key(code, True)
            yield
        finally:
            for code in reversed(new_codes):
                self._key(code, False)

    def _move(self, target: Point, duration: float | None = None) -> None:
        self.checkpoint()
        self.backend.validate_point(target)
        start = self.backend.position()
        if target == start:
            return
        elapsed_duration = motion_duration(start, target, duration)
        started = self._clock()
        last = start
        while True:
            self.checkpoint()
            expected_window = getattr(self._local, "window_id", None)
            if expected_window is not None:
                self.backend.ensure_target(window_id=expected_window)
            progress = min(1.0, (self._clock() - started) / elapsed_duration)
            eased = ease_motion(progress)
            point = (
                round(start[0] + (target[0] - start[0]) * eased),
                round(start[1] + (target[1] - start[1]) * eased),
            )
            if point != last:
                self.emit(lambda: self.backend.move(point))
                with self._state_lock:
                    self._state = replace(self._state, cursor=point)
                last = point
            if progress >= 1.0:
                break
            self.wait(min(1 / 120, elapsed_duration * (1.0 - progress)))

    def execute(
        self,
        actions: Sequence[Action],
        *,
        resolve: Callable[[str, Point], Point] | None = None,
        window_id: int | None = None,
    ) -> list[dict[str, object]]:
        """Validate the complete batch before producing its first input event."""
        self.checkpoint()
        if not 1 <= len(actions) <= 64:
            raise ValueError("A batch must contain between 1 and 64 actions.")
        if any(action.kind != "wait" for action in actions):
            with self._input_activity():
                return self._execute_batch(actions, resolve=resolve, window_id=window_id)
        return self._execute_batch(actions, resolve=resolve, window_id=window_id)

    def _execute_batch(
        self,
        actions: Sequence[Action],
        *,
        resolve: Callable[[str, Point], Point] | None,
        window_id: int | None,
    ) -> list[dict[str, object]]:
        prepared: list[Action] = []
        held_keys: set[int] = set()
        held_buttons: set[Button] = set()
        for action in actions:
            updates: dict[str, object] = {}
            if action.frame_id is not None:
                if resolve is None:
                    raise ValueError("Image coordinates require the observation service.")
                for field in ("loc", "start"):
                    point = getattr(action, field)
                    if point is not None:
                        updates[field] = resolve(action.frame_id, point)
                updates["frame_id"] = None
            normalized = action.model_copy(update=updates)
            codes = {key_code(key) for key in normalized.keys}
            if normalized.kind == "key_down":
                if codes & held_keys:
                    raise ValueError("A key_down step repeats an already held key.")
                held_keys.update(codes)
            elif normalized.kind == "key_up":
                if not codes <= held_keys:
                    raise ValueError("A key_up step must release keys held earlier in this batch.")
                held_keys.difference_update(codes)
            elif normalized.kind == "button_down":
                if normalized.button in held_buttons:
                    raise ValueError("A button_down step repeats an already held button.")
                held_buttons.add(normalized.button)
            elif normalized.kind == "button_up":
                if normalized.button not in held_buttons:
                    raise ValueError("A button_up step must release a button held in this batch.")
                held_buttons.remove(normalized.button)
            elif normalized.kind in {"click", "drag"} and normalized.button in held_buttons:
                raise ValueError("Release the held button before starting a new click or drag.")
            elif normalized.kind == "text" and held_keys:
                raise ValueError("Release batch-held keys before entering literal text.")
            elif normalized.kind == "key" and codes <= held_keys:
                raise ValueError("A key chord must press at least one key not already held.")
            for point in (normalized.loc, normalized.start):
                if point is not None:
                    self.backend.validate_point(point)
                    self.backend.ensure_target(point, window_id)
            prepared.append(normalized)
        results: list[dict[str, object]] = []
        previous_window = getattr(self._local, "window_id", None)
        self._local.window_id = window_id
        try:
            for action in prepared:
                self.checkpoint()
                self.backend.ensure_target(action.loc, window_id)
                with self._state_lock:
                    self._state = replace(
                        self._state, action=f"{action.kind} ({len(results) + 1}/{len(prepared)})"
                    )
                self._execute_one(action, window_id)
                self.checkpoint()
                results.append({"kind": action.kind, "completed": True})
                with self._state_lock:
                    self._state = replace(
                        self._state, completed_actions=self._state.completed_actions + 1
                    )
        except (OSError, RuntimeError, ValueError) as error:
            if isinstance(error, OSError):
                self.stop("Windows rejected input. Inspect the app and resume locally.")
            raise BatchInterrupted(len(results), error) from error
        finally:
            try:
                errors = self._release_inputs()
                if errors:
                    self._record_release_error(errors)
                    raise RuntimeError("Failed to release owned input at the end of the batch.")
            finally:
                self._local.window_id = previous_window
        return results

    def _execute_one(self, action: Action, window_id: int | None) -> None:
        if action.kind == "wait":
            self.wait(action.duration or 0.0)
            return
        if action.kind == "move":
            self._move(action.loc, action.duration)
            return
        if action.kind == "drag":
            if action.start is not None:
                self._move(action.start)
            with self._modifiers(action.keys):
                self._button(action.button, True)
                try:
                    self._move(action.loc, action.duration)
                finally:
                    self._button(action.button, False)
            return
        if action.loc is not None:
            self._move(action.loc, action.duration)
            self.backend.ensure_target(action.loc, window_id)
        if action.kind == "click":
            with self._modifiers(action.keys):
                for index in range(action.clicks):
                    self._button(action.button, True)
                    self._button(action.button, False)
                    if index + 1 < action.clicks:
                        self.wait(0.035)
        elif action.kind == "scroll":
            with self._modifiers(action.keys):
                self.emit(lambda: self.backend.wheel(action.delta_x, action.delta_y))
        elif action.kind == "key":
            for _ in range(action.repeat):
                with self._modifiers(action.keys):
                    self.checkpoint()
        elif action.kind == "text":
            text_window = window_id if window_id is not None else self.backend.foreground()
            if action.clear:
                self.backend.ensure_target(window_id=text_window)
                with self._modifiers(["ctrl", "a"]):
                    self.checkpoint()
                with self._modifiers(["backspace"]):
                    self.checkpoint()
            text = (action.text or "").replace("\r\n", "\n").replace("\r", "\n")
            for offset in range(0, len(text), 64):
                self.backend.ensure_target(window_id=text_window)
                chunk = text[offset : offset + 64]
                self.emit(lambda: self.backend.text(chunk))
            if action.submit:
                self.backend.ensure_target(window_id=text_window)
                with self._modifiers(["enter"]):
                    self.checkpoint()
        elif action.kind in {"key_down", "key_up"}:
            for key in action.keys:
                self._key(key_code(key), action.kind == "key_down")
        elif action.kind in {"button_down", "button_up"}:
            self._button(action.button, action.kind == "button_down")
        else:
            raise ValueError(f"Unsupported action: {action.kind}")
