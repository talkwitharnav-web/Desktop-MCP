import threading
import time
import ctypes
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from desktop_mcp.actions import Action, ease_motion, key_code, parse_shortcut
from desktop_mcp.runtime import BatchInterrupted, Controller, DesktopStopped, InputNotAllowed


class FakeInput:
    def __init__(self):
        self.events = []
        self.point = (10, 10)
        self.window = 1
        self.fail_release = False
        self.on_event = lambda event: None

    def _record(self, *event):
        self.events.append(event)
        self.on_event(event)

    def position(self):
        return self.point

    def foreground(self):
        return self.window

    def validate_point(self, point):
        if not (0 <= point[0] < 1000 and 0 <= point[1] < 1000):
            raise ValueError("Invalid test coordinate")

    def ensure_target(self, point=None, window_id=None):
        if window_id is not None and self.window != window_id:
            raise RuntimeError("Foreground changed")

    def move(self, point):
        self.point = point
        self._record("move", point)

    def key(self, code, down):
        if not down and self.fail_release:
            raise OSError("Fixture key release failed")
        self._record("key", code, down)

    def button(self, button, down):
        if not down and self.fail_release:
            raise OSError("Fixture button release failed")
        self._record("button", button, down)

    def text(self, text):
        self._record("text", text)

    def wheel(self, x, y):
        self._record("wheel", x, y)

    def release_pending(self):
        pass


@pytest.fixture
def armed():
    backend = FakeInput()
    controller = Controller(backend)
    controller.set_interface_ready(True)
    controller.arm_local()
    return controller, backend


def test_controller_starts_stopped_and_requires_live_interface():
    controller = Controller(FakeInput())
    with pytest.raises(DesktopStopped):
        controller.arm_local()
    with pytest.raises(DesktopStopped):
        with controller.operation("capture"):
            pytest.fail("Stopped controller admitted a capture")
    controller.set_interface_ready(True)
    controller.arm_local()
    assert controller.snapshot().armed
    controller.set_interface_ready(False, "Hotkey unavailable")
    assert not controller.snapshot().armed
    assert controller.snapshot().last_error == "Hotkey unavailable"


def test_human_takeover_latches_stop_and_never_auto_resumes(armed):
    controller, _ = armed
    revision = controller.input_revision
    controller.notify_human_input()
    assert not controller.snapshot().armed
    assert controller.input_revision > revision
    with pytest.raises(DesktopStopped):
        with controller.operation("typing"):
            pass
    controller.arm_local()
    controller.set_human_takeover(False)
    controller.notify_human_input()
    assert controller.snapshot().armed


def test_validate_entire_batch_before_any_input(armed):
    controller, backend = armed
    with controller.operation("batch"), pytest.raises(ValueError):
        controller.execute(
            [
                Action(kind="click", loc=(10, 10)),
                Action(kind="click", loc=(2000, 10)),
            ]
        )
    assert backend.events == []


def test_text_is_batched_without_typing_delays_and_preserves_unicode(armed, monkeypatch):
    controller, backend = armed
    monkeypatch.setattr(controller, "wait", lambda _: pytest.fail("Text was artificially delayed"))
    text = "A" * 130 + "\U0001f369\r\nnext\tline"
    with controller.operation("text"):
        controller.execute([Action(kind="text", text=text)])
    assert "".join(event[1] for event in backend.events) == text.replace("\r\n", "\n")
    assert max(len(event[1]) for event in backend.events) <= 64


def test_keys_and_buttons_are_released_before_batch_returns(armed):
    controller, backend = armed
    with controller.operation("batch"):
        controller.execute(
            [
                Action(kind="key_down", keys=["shift"]),
                Action(kind="button_down", button="middle"),
            ]
        )
        assert ("key", key_code("shift"), False) in backend.events
        assert ("button", "middle", False) in backend.events
        assert controller.snapshot().state == "running"
    assert controller.snapshot().state == "ready"


def test_stop_during_middle_drag_releases_button_and_modifier(armed):
    controller, backend = armed
    backend.on_event = lambda event: controller.stop("Ctrl+Shift+H") if event[0] == "move" else None
    with pytest.raises(BatchInterrupted):
        with controller.operation("drag"):
            controller.execute(
                [
                    Action(kind="drag", loc=(200, 200), button="middle", keys=["shift"]),
                ]
            )
    assert ("key", key_code("shift"), False) in backend.events
    assert ("button", "middle", False) in backend.events
    assert not controller.snapshot().armed
    stop_release = backend.events.index(("button", "middle", False))
    assert not any(event[0] == "move" for event in backend.events[stop_release + 1 :])


def test_release_failure_cannot_be_cleared_by_rearming(armed):
    controller, backend = armed
    with pytest.raises(RuntimeError, match="release"):
        with controller.operation("batch"):
            backend.fail_release = True
            controller.execute([Action(kind="key_down", keys=["ctrl"])])
    assert controller.snapshot().state == "error"
    with pytest.raises(DesktopStopped):
        controller.arm_local()
    backend.fail_release = False
    controller.stop()
    controller.arm_local()
    assert controller.snapshot().armed


def test_long_text_can_be_stopped_between_chunks(armed):
    controller, backend = armed
    backend.on_event = lambda event: controller.stop("stop") if event[0] == "text" else None
    with pytest.raises(BatchInterrupted):
        with controller.operation("text"):
            controller.execute([Action(kind="text", text="x" * 10000)])
    assert len([event for event in backend.events if event[0] == "text"]) == 1


def test_text_rechecks_focus_between_chunks(armed):
    controller, backend = armed
    backend.on_event = lambda event: setattr(backend, "window", 2) if event[0] == "text" else None
    with pytest.raises(BatchInterrupted, match="Foreground changed"):
        with controller.operation("text"):
            controller.execute([Action(kind="text", text="x" * 200)])
    assert len([event for event in backend.events if event[0] == "text"]) == 1


def test_chords_repeat_and_horizontal_scroll_are_real_operations(armed):
    controller, backend = armed
    with controller.operation("keys"):
        controller.execute(
            [
                Action(kind="key", keys=["ctrl", "c"], repeat=2),
                Action(kind="scroll", delta_x=120, delta_y=-240, keys=["shift"]),
            ]
        )
    assert backend.events.count(("key", key_code("c"), True)) == 2
    assert ("wheel", 120, -240) in backend.events


def test_image_coordinates_are_resolved_before_execution(armed):
    controller, backend = armed
    resolved = []

    def resolve(frame, point):
        resolved.append((frame, point))
        return 10, 10

    with controller.operation("image click"):
        controller.execute([Action(kind="click", loc=(1, 2), frame_id="frame")], resolve=resolve)
    assert resolved == [("frame", (1, 2))]
    assert ("button", "left", True) in backend.events


def test_motion_has_acceleration_and_deceleration(armed, monkeypatch):
    controller, backend = armed
    clock = [0.0]
    controller._clock = lambda: clock[0]

    def advance(duration):
        clock[0] += duration
        controller.checkpoint()

    monkeypatch.setattr(controller, "wait", advance)
    with controller.operation("move"):
        controller.execute([Action(kind="move", loc=(900, 10), duration=0.5)])
    xs = [event[1][0] for event in backend.events if event[0] == "move"]
    assert len(xs) > 20
    assert xs[-1] == 900
    assert xs == sorted(xs)
    deltas = [right - left for left, right in zip(xs, xs[1:])]
    assert max(deltas) > deltas[0]
    assert max(deltas) > deltas[-1]
    assert ease_motion(0) == 0
    assert ease_motion(1) == 1
    assert ease_motion(0.5) == pytest.approx(0.5)


def test_pending_operations_do_not_revive_after_stop_and_resume(armed):
    controller, backend = armed
    queued = threading.Event()
    results = []
    with pytest.raises(DesktopStopped), controller.operation("hold lock"):

        def worker():
            queued.set()
            try:
                with controller.operation("old queued work"):
                    controller.execute([Action(kind="text", text="must not type")])
            except DesktopStopped:
                results.append("revoked")

        thread = threading.Thread(target=worker)
        thread.start()
        queued.wait(1)
        time.sleep(0.04)
        controller.stop()
        controller.arm_local()
        thread.join(1)
        assert results == ["revoked"]
        # The old outer operation was revoked too.
        with pytest.raises(DesktopStopped):
            controller.checkpoint()
    assert backend.events == []


@pytest.mark.parametrize(
    "payload",
    [
        {"kind": "move", "loc": [True, 1]},
        {"kind": "move", "loc": [1, 2], "duration": 0},
        {"kind": "wait", "duration": float("nan")},
        {"kind": "key", "keys": ["ctrl", "control"]},
        {"kind": "key", "keys": ["not_a_key"]},
        {"kind": "text", "text": "\ud800"},
        {"kind": "text", "text": "hello", "unknown": True},
    ],
)
def test_malformed_operations_fail_before_execution(payload):
    with pytest.raises(ValidationError):
        Action.model_validate(payload)


def test_shortcuts_support_windows_media_numpad_and_plus():
    assert parse_shortcut("ctrl++") == ["ctrl", "plus"]
    assert key_code("F24") == 0x87
    assert key_code("numpad0") == 0x60
    assert key_code("win") == 0x5B
    assert key_code("volume-up") == 0xAF


@pytest.mark.parametrize(
    "actions",
    [
        [Action(kind="click"), Action(kind="key_up", keys=["ctrl"])],
        [Action(kind="button_down"), Action(kind="button_down")],
        [Action(kind="key_down", keys=["ctrl"]), Action(kind="text", text="unsafe")],
    ],
)
def test_invalid_held_input_sequences_emit_nothing(armed, actions):
    controller, backend = armed
    with controller.operation("invalid batch"), pytest.raises(ValueError):
        controller.execute(actions)
    assert backend.events == []


def test_reserved_stop_chord_cannot_continue_typing(armed):
    controller, backend = armed
    with pytest.raises(BatchInterrupted):
        with controller.operation("shortcut"):
            controller.execute(
                [
                    Action(kind="key", keys=["ctrl", "shift", "h"]),
                    Action(kind="text", text="must not type"),
                ]
            )
    assert not controller.snapshot().armed
    assert not any(event[0] == "text" for event in backend.events)
    assert ("key", key_code("ctrl"), False) in backend.events
    assert ("key", key_code("shift"), False) in backend.events


def test_empty_clear_actually_removes_selected_text(armed):
    controller, backend = armed
    with controller.operation("clear"):
        controller.execute([Action(kind="text", text="", clear=True)])
    assert ("key", key_code("backspace"), True) in backend.events


def test_redundant_local_arm_does_not_interrupt_an_active_operation(armed):
    controller, backend = armed
    generation = controller.snapshot().generation
    with controller.operation("running"):
        controller._key(key_code("shift"), True)
        controller.arm_local()
        controller.checkpoint()
        assert ("key", key_code("shift"), False) not in backend.events
    assert controller.snapshot().generation == generation
    assert ("key", key_code("shift"), False) in backend.events


def test_stop_does_not_deadlock_the_ui_thread_behind_a_sendinput_hook(armed):
    controller, backend = armed
    awaiting_ui = threading.Event()
    ui_returned = threading.Event()
    finished = threading.Event()

    def simulate_sendinput(event):
        if event[0] == "move":
            awaiting_ui.set()
            assert ui_returned.wait(2), "Stop blocked the UI thread needed by SendInput"

    backend.on_event = simulate_sendinput

    def worker():
        try:
            with controller.operation("native hook simulation"):
                controller.execute([Action(kind="drag", loc=(500, 500), button="middle")])
        except BatchInterrupted:
            finished.set()

    thread = threading.Thread(target=worker)
    thread.start()
    assert awaiting_ui.wait(2)
    try:
        started = time.monotonic()
        controller.stop("hotkey")
        assert time.monotonic() - started < 0.2
        assert not controller.snapshot().armed
        with pytest.raises(DesktopStopped):
            controller.arm_local()
    finally:
        ui_returned.set()
        thread.join(2)
        controller.close()
    assert finished.is_set()
    assert ("button", "middle", False) in backend.events


def test_keyboard_location_cannot_bypass_own_foreground_window_guard(armed):
    from ctypes import wintypes
    from desktop_mcp.native import WindowsInput

    controller, backend = armed
    backend.window = 99
    backend._control_windows = lambda: (99,)

    def rectangle(handle, pointer):
        value = ctypes.cast(pointer, ctypes.POINTER(wintypes.RECT)).contents
        value.left, value.top, value.right, value.bottom = 0, 0, 100, 100
        return True

    backend._user32 = SimpleNamespace(
        IsWindowVisible=lambda handle: True,
        IsIconic=lambda handle: False,
        GetWindowLongW=lambda handle, index: 0,
        GetWindowRect=rectangle,
    )
    backend.ensure_target = WindowsInput.ensure_target.__get__(backend)
    with pytest.raises(BatchInterrupted, match="own control window"):
        with controller.operation("keyboard location"):
            controller.execute([Action(kind="key", keys=["enter"], loc=(200, 200))])
    assert not any(event[0] == "key" for event in backend.events)


def test_targeted_drag_aborts_if_focus_changes_while_approaching_start(armed):
    controller, backend = armed
    backend.on_event = lambda event: setattr(backend, "window", 2) if event[0] == "move" else None
    with pytest.raises(BatchInterrupted, match="Foreground changed"):
        with controller.operation("targeted drag"):
            controller.execute(
                [
                    Action(kind="drag", start=(50, 50), loc=(100, 100), duration=0.05),
                ],
                window_id=1,
            )
    assert not any(event[0] == "button" for event in backend.events)


def test_teaching_mode_follows_human_motion_without_allowing_injected_input(armed):
    controller, backend = armed
    controller.set_mode_local("teach")
    assert not controller.snapshot().armed
    controller.arm_local()
    revision = controller.input_revision
    controller.notify_human_input(kind="move", position=(100, 120))
    assert controller.snapshot().armed
    assert controller.snapshot().user_cursor == (100, 120)
    assert controller.input_revision == revision
    controller.notify_human_input(kind="button", position=(100, 120))
    assert controller.snapshot().armed
    assert controller.input_revision > revision
    with controller.operation("teaching"), pytest.raises(InputNotAllowed):
        controller.execute([Action(kind="text", text="must not type")])
    with controller.operation("teaching"), pytest.raises(InputNotAllowed):
        controller.emit(lambda: backend.text("must not type"))
    assert backend.events == []
    controller.stop("Ctrl+Shift+H")
    assert not controller.snapshot().armed


def test_mode_change_revokes_the_existing_operation(armed):
    controller, backend = armed
    with pytest.raises(DesktopStopped), controller.operation("old control mode"):
        controller.set_mode_local("teach")
        controller.arm_local()
        controller.checkpoint()
    assert backend.events == []
