"""UI policy and fake-Win32 lifecycle tests; never open a real window or hook."""

import ctypes
import queue
import threading
from dataclasses import replace
from types import SimpleNamespace

import pytest

from desktop_mcp import ui
from desktop_mcp.contracts import INJECTED_INPUT_TAG, ControlSnapshot
from desktop_mcp.cursor import render_cursor


class FakeController:
    def __init__(self):
        self.value = ControlSnapshot("stopped", "Not armed")
        self.calls = []
        self.lock = threading.RLock()
        self.serial_action_lock = threading.Lock()

    def snapshot(self):
        with self.lock:
            return self.value

    def update(self, **values):
        with self.lock:
            self.value = replace(self.value, **values)

    def arm_local(self):
        with self.lock:
            assert self.value.interface_ready
            self.calls.append(("arm",))
            self.value = replace(self.value, state="ready", reason="Armed locally")

    def stop(self, reason="Stopped locally"):
        with self.lock:
            self.calls.append(("stop", reason))
            self.value = replace(
                self.value,
                state="stopped",
                action=None,
                reason=reason,
                input_active=False,
                awaiting_user=False,
                generation=self.value.generation + 1,
            )

    def set_interface_ready(self, ready, error=None):
        with self.lock:
            self.calls.append(("interface", ready, error))
            self.value = replace(
                self.value,
                interface_ready=ready,
                last_error=error if error is not None else self.value.last_error,
                state="error" if error is not None else self.value.state,
            )

    def set_human_takeover(self, enabled):
        self.calls.append(("takeover", enabled))
        self.update(human_takeover=enabled)

    def notify_human_input(self, *, kind="move", position=None):
        with self.lock:
            self.calls.append(("human", kind, position))
            if position is not None:
                self.value = replace(self.value, user_cursor=position)
            if not self.value.armed:
                return
            if self.value.input_active and self.value.human_takeover:
                self.stop("Human input")
            elif kind != "move":
                self.value = replace(self.value, input_revision=self.value.input_revision + 1)


class FakeWin32:
    def __init__(self):
        self.surface = None
        self.owner = None
        self.events = []
        self.jobs = queue.SimpleQueue()
        self.wakeup = threading.Event()
        self.handles = ()
        self.point = (10, 20)
        self.cursor = None
        self.visible = False
        self.hidden = False
        self.models = []
        self.initialize_error = None
        self.shutdown_error = None
        self.render_error = None
        self.hide_error = None
        self.restore_error = None
        self.on_track = None
        self.initialize_gate = None
        self.pump_gate = None
        self.pump_blocked = threading.Event()
        self.modal_exit = None

    def _owned(self):
        assert self.owner == threading.get_ident()

    def initialize(self, surface):
        self.surface = surface
        self.owner = threading.get_ident()
        self.handles = (0x100000010, 0x100000020, 0x100000030)
        self.events.append("initialize")
        surface._human_input("keyboard", 0, 0, kind="key")
        if self.initialize_gate is not None:
            assert self.initialize_gate.wait(2)
        if self.initialize_error is not None:
            raise self.initialize_error
        self.visible = True
        self.events.extend(["hotkey", "keyboard-hook", "mouse-hook"])

    def window_handles(self):
        self._owned()
        return self.handles

    def wake(self):
        self.wakeup.set()

    def cancel_modal(self):
        self._owned()
        self.events.append("cancel-modal")
        if self.modal_exit is not None:
            self.modal_exit.set()

    def pump(self, timeout_ms):
        self._owned()
        if self.pump_gate is not None:
            self.pump_blocked.set()
            assert self.pump_gate.wait(2)
        self.wakeup.wait(timeout_ms / 1000)
        self.wakeup.clear()
        while True:
            try:
                callback, done, errors = self.jobs.get_nowait()
            except queue.Empty:
                return
            try:
                callback()
            except Exception as error:
                errors.append(error)
                self.surface._record_failure(error)
            finally:
                done.set()

    def invoke(self, callback):
        done = threading.Event()
        errors = []
        self.jobs.put((callback, done, errors))
        self.wake()
        assert done.wait(1), "Fake UI callback did not return"
        if errors:
            raise errors[0]

    def render_panel(self, model):
        self._owned()
        if self.render_error is not None:
            raise self.render_error
        self.models.append(model)

    def remember_foreground(self, control_windows):
        pass

    def cursor_position(self):
        self._owned()
        return self.point

    def track_cursor(self, point):
        self._owned()
        assert not self.hidden
        self.cursor = point
        if self.on_track is not None:
            self.on_track()

    def hide_cursor(self):
        self._owned()
        self.cursor = None

    def hide_for_capture(self):
        self._owned()
        self.hidden = True
        self.visible = False
        self.cursor = None
        self.events.append("hide-and-flush")
        if self.hide_error is not None:
            raise self.hide_error

    def restore_after_capture(self):
        self._owned()
        self.hidden = False
        self.visible = True
        self.events.append("restore")
        if self.restore_error is not None:
            raise self.restore_error

    def show_panel(self):
        self._owned()
        assert not self.hidden
        self.visible = True
        self.events.append("show")

    def minimize_panel(self):
        self._owned()
        self.events.append("minimize")

    def shutdown(self):
        self._owned()
        self.events.append("shutdown")
        self.visible = False
        self.cursor = None
        self.handles = ()
        if self.shutdown_error is not None:
            raise self.shutdown_error


@pytest.fixture
def surface_factory(monkeypatch):
    surfaces = []

    def make(adapter=None, controller=None):
        adapter = adapter or FakeWin32()
        controller = controller or FakeController()
        monkeypatch.setattr(ui, "_Win32Adapter", lambda: adapter)
        surface = ui.ControlSurface(controller)
        surfaces.append((surface, adapter))
        return surface, controller, adapter

    yield make
    for surface, adapter in surfaces:
        if adapter.initialize_gate is not None:
            adapter.initialize_gate.set()
        if adapter.pump_gate is not None:
            adapter.pump_gate.set()
        adapter.shutdown_error = None
        surface._shutdown_error = None
        surface.close()
        assert surface._thread is None or not surface._thread.is_alive()


@pytest.mark.parametrize(
    "state,ready,enabled",
    [
        ("stopped", False, False),
        ("stopped", True, True),
        ("ready", True, False),
        ("running", True, False),
        ("error", False, False),
        ("error", True, True),
        ("closed", True, False),
    ],
)
def test_panel_state_mapping(state, ready, enabled):
    model = ui._panel_model(ControlSnapshot(state, "Reason", interface_ready=ready))
    assert model.arm_enabled is enabled
    assert model.state == state
    assert model.detail == "Reason"
    assert model.action == "No action running"
    assert model.stop_enabled is (state != "closed")
    assert model.settings_enabled is (ready and state != "closed")


def test_panel_shows_current_action_and_failure():
    snapshot = ControlSnapshot(
        "error", "Stopped", action="Move pointer", last_error="Global stop unavailable"
    )
    model = ui._panel_model(snapshot)
    assert model.action == "Move pointer"
    assert model.detail == "Global stop unavailable"
    assert "Ctrl + Shift + H" in ui._SHORTCUT_HINT


@pytest.mark.parametrize("state", ["ready", "running"])
def test_unified_panel_makes_guidance_and_control_available(state):
    model = ui._panel_model(ControlSnapshot(state, "Ready to help", interface_ready=True))
    assert "guidance and control" in model.heading.lower()
    assert model.settings_enabled
    assert not model.arm_enabled


@pytest.mark.parametrize(
    "kind,flags,extra,physical",
    [
        ("keyboard", 0, 0, True),
        ("keyboard", 0x80, 0, True),
        ("keyboard", 0x10, 0, False),
        ("keyboard", 0x02, 0, False),
        ("keyboard", 0, INJECTED_INPUT_TAG, False),
        ("mouse", 0, 0, True),
        ("mouse", 1, 0, False),
        ("mouse", 2, 0, False),
        ("mouse", 0, INJECTED_INPUT_TAG, False),
    ],
)
def test_injected_events_never_count_as_human_input(kind, flags, extra, physical):
    assert ui._is_physical_input(kind, flags, extra) is physical


def test_constructor_is_lazy_and_start_is_disarmed_and_joinable(surface_factory):
    surface, controller, adapter = surface_factory()
    assert adapter.events == []
    assert controller.calls == []
    assert surface.window_handles() == ()
    controller.update(state="ready", interface_ready=True, human_takeover=False)
    surface.start()
    surface.start()
    assert controller.snapshot().interface_ready
    assert not controller.snapshot().armed
    assert controller.snapshot().human_takeover
    assert not any(call[0] in {"arm", "human"} for call in controller.calls)
    assert surface._thread.daemon is False
    assert adapter.owner != threading.get_ident()
    assert surface.window_handles() == adapter.handles
    surface.show()
    assert not controller.snapshot().armed
    surface.close()
    assert not surface._thread.is_alive()
    assert not controller.snapshot().interface_ready
    assert surface.window_handles() == ()
    assert adapter.events.count("shutdown") == 1
    with pytest.raises(RuntimeError, match="closed"):
        surface.start()


def test_only_local_arm_command_arms_and_hotkey_does_not_wait_for_actions(surface_factory):
    surface, controller, adapter = surface_factory()
    surface.start()
    adapter.invoke(lambda: surface._local_command(ui._LocalCommand.ARM))
    assert controller.snapshot().armed
    with controller.serial_action_lock:
        adapter.invoke(surface._hotkey)
    assert not controller.snapshot().armed
    assert "Ctrl+Shift+H" in controller.snapshot().reason
    assert adapter.cursor is None
    surface.show()
    with surface.capture_guard():
        assert not controller.snapshot().armed
    assert sum(call[0] == "arm" for call in controller.calls) == 1
    adapter.invoke(lambda: surface._local_command(ui._LocalCommand.ARM))
    adapter.invoke(lambda: surface._local_command(ui._LocalCommand.STOP))
    assert not controller.snapshot().armed
    assert "Stop button" in controller.snapshot().reason


def test_successful_local_arm_minimizes_once_and_refresh_does_not_raise_panel(surface_factory):
    surface, controller, adapter = surface_factory()
    surface.start()
    handles = surface.window_handles()
    adapter.invoke(lambda: surface._local_command(ui._LocalCommand.ARM))
    assert controller.snapshot().armed
    assert adapter.events.count("minimize") == 1
    assert surface.window_handles() == handles
    adapter.invoke(surface._refresh)
    adapter.invoke(lambda: surface._local_command(ui._LocalCommand.ARM))
    assert adapter.events.count("minimize") == 1
    assert "show" not in adapter.events


def test_local_arm_updates_controls_before_minimizing(surface_factory):
    surface, _, adapter = surface_factory()
    surface.start()
    minimize = adapter.minimize_panel

    def checked_minimize():
        assert adapter.models[-1].state == "ready"
        assert not adapter.models[-1].arm_enabled
        minimize()

    adapter.minimize_panel = checked_minimize
    adapter.invoke(lambda: surface._local_command(ui._LocalCommand.ARM))
    assert adapter.events.count("minimize") == 1


def test_ready_stopped_panel_explains_local_arming_instead_of_unavailability(surface_factory):
    surface, controller, adapter = surface_factory()
    surface.start()
    assert controller.snapshot().interface_ready
    assert adapter.models[-1].arm_enabled
    assert (
        adapter.models[-1].detail == "Arm / Resume enables guidance and desktop control together."
    )


@pytest.mark.parametrize("outcome", ["refused", "revoked", "error"])
def test_unsuccessful_local_arm_does_not_minimize_panel(surface_factory, monkeypatch, outcome):
    surface, controller, adapter = surface_factory()
    surface.start()
    original_arm = controller.arm_local

    def arm():
        if outcome == "error":
            raise RuntimeError("Arming failed")
        if outcome == "revoked":
            original_arm()
        controller.stop("Arming was not granted")

    monkeypatch.setattr(controller, "arm_local", arm)
    adapter.invoke(lambda: surface._local_command(ui._LocalCommand.ARM))
    assert not controller.snapshot().armed
    assert controller.snapshot().interface_ready
    assert surface._thread.is_alive()
    assert surface._error is None
    assert "minimize" not in adapter.events
    if outcome == "error":
        assert "Arming failed" in adapter.models[-1].detail


@pytest.mark.parametrize("error_type", [RuntimeError, ValueError])
def test_local_arm_rejection_preserves_ready_interface_and_allows_explicit_retry(
    surface_factory, monkeypatch, error_type
):
    surface, controller, adapter = surface_factory()
    surface.start()
    original_arm = controller.arm_local
    handles = surface.window_handles()
    generation = controller.snapshot().generation
    interface_calls = [call for call in controller.calls if call[0] == "interface"]
    attempts = []

    def reject():
        attempts.append("arm")
        raise error_type("Input release is still finishing; retry locally")

    monkeypatch.setattr(controller, "arm_local", reject)
    adapter.invoke(lambda: surface._local_command(ui._LocalCommand.ARM))
    adapter.invoke(surface._refresh)
    surface.show()
    assert attempts == ["arm"]
    assert controller.snapshot().generation == generation
    assert controller.snapshot().interface_ready
    assert not controller.snapshot().armed
    assert surface._thread.is_alive()
    assert not surface._closing.is_set()
    assert surface._error is None
    assert surface.window_handles() == handles
    assert [call for call in controller.calls if call[0] == "interface"] == interface_calls
    assert "Input release is still finishing" in adapter.models[-1].detail
    assert adapter.models[-1].arm_enabled
    assert "shutdown" not in adapter.events
    assert "minimize" not in adapter.events

    monkeypatch.setattr(controller, "arm_local", original_arm)
    adapter.invoke(lambda: surface._local_command(ui._LocalCommand.ARM))
    assert controller.snapshot().armed
    assert controller.snapshot().interface_ready
    assert adapter.events.count("minimize") == 1
    assert "Input release is still finishing" not in adapter.models[-1].detail
    assert surface._local_rejection is None


@pytest.mark.parametrize("stop_source", ["hotkey", "remote"])
def test_new_stop_supersedes_recoverable_arm_notice(surface_factory, monkeypatch, stop_source):
    surface, controller, adapter = surface_factory()
    surface.start()

    def reject():
        raise RuntimeError("Input release is still finishing")

    monkeypatch.setattr(controller, "arm_local", reject)
    adapter.invoke(lambda: surface._local_command(ui._LocalCommand.ARM))
    if stop_source == "hotkey":
        adapter.invoke(surface._hotkey)
    else:
        controller.stop("Stopped remotely")
        adapter.invoke(surface._refresh)
    assert controller.snapshot().interface_ready
    assert not controller.snapshot().armed
    assert surface._local_rejection is None
    assert adapter.models[-1].detail == controller.snapshot().reason


def test_native_minimize_failure_is_still_fatal_after_successful_arm(surface_factory, monkeypatch):
    surface, controller, adapter = surface_factory()
    surface.start()

    def fail_minimize():
        raise OSError("Native minimize failed")

    monkeypatch.setattr(adapter, "minimize_panel", fail_minimize)
    with pytest.raises(OSError, match="Native minimize failed"):
        adapter.invoke(lambda: surface._local_command(ui._LocalCommand.ARM))
    assert surface._closed.wait(1)
    assert not controller.snapshot().armed
    assert not controller.snapshot().interface_ready
    assert "Native minimize failed" in controller.snapshot().last_error


def test_overlay_follows_actual_pointer_not_snapshot_or_an_animation(surface_factory):
    surface, controller, adapter = surface_factory()
    controller.update(cursor=(9999, 9999))
    surface.start()
    assert adapter.cursor is None
    adapter.invoke(lambda: surface._local_command(ui._LocalCommand.ARM))
    controller.update(input_active=True)
    for point in ((-200, 42), (-191, 50), (0, 0), (501, -99)):
        adapter.point = point
        adapter.invoke(surface._refresh)
        assert adapter.cursor == point
    controller.stop("Remote stop")
    adapter.invoke(surface._refresh)
    assert adapter.cursor is None


def test_stop_racing_cursor_render_cannot_leave_an_armed_visual(surface_factory):
    surface, controller, adapter = surface_factory()
    surface.start()
    controller.update(input_active=True)
    adapter.on_track = lambda: controller.stop("Stopped during rendering")
    adapter.invoke(lambda: surface._local_command(ui._LocalCommand.ARM))
    assert not controller.snapshot().armed
    assert adapter.cursor is None


def test_human_takeover_ignores_injection_and_can_be_disabled_locally(surface_factory):
    surface, controller, adapter = surface_factory()
    surface.start()
    adapter.invoke(lambda: surface._local_command(ui._LocalCommand.ARM))
    controller.update(input_active=True)
    for kind, flags, extra in (
        ("keyboard", 0x10, 0),
        ("mouse", 1, 0),
        ("keyboard", 0, INJECTED_INPUT_TAG),
        ("mouse", 0, INJECTED_INPUT_TAG),
    ):
        adapter.invoke(
            lambda: surface._human_input(
                kind, flags, extra, kind="move" if kind == "mouse" else "key"
            )
        )
        assert controller.snapshot().armed
    adapter.invoke(lambda: surface._human_input("mouse", 0, 0, kind="move"))
    assert not controller.snapshot().armed
    assert adapter.cursor is None
    assert ("human", "move", None) in controller.calls
    adapter.invoke(lambda: surface._local_command(ui._LocalCommand.TAKEOVER))
    assert not controller.snapshot().human_takeover
    adapter.invoke(lambda: surface._local_command(ui._LocalCommand.ARM))
    adapter.invoke(lambda: surface._human_input("keyboard", 0, 0, kind="key"))
    assert controller.snapshot().armed
    adapter.invoke(surface._hotkey)
    assert not controller.snapshot().armed


def test_cursor_tracks_automatic_input_not_idle_learner_movement(surface_factory):
    surface, controller, adapter = surface_factory()
    surface.start()
    adapter.invoke(lambda: surface._local_command(ui._LocalCommand.ARM))
    assert adapter.cursor is None
    generation = controller.snapshot().generation
    controller.update(input_active=True)
    adapter.invoke(surface._refresh)
    assert adapter.cursor == adapter.point
    controller.update(input_active=False, awaiting_user=True, state="running")
    adapter.invoke(surface._refresh)
    assert "Your turn" in adapter.models[-1].heading
    assert adapter.cursor is None
    assert sum(call[0] == "arm" for call in controller.calls) == 1
    assert controller.snapshot().armed
    with surface.capture_guard():
        assert adapter.cursor is None
    controller.update(awaiting_user=False, input_active=True)
    adapter.invoke(surface._refresh)
    assert controller.snapshot().armed
    assert adapter.cursor == adapter.point
    assert controller.snapshot().generation == generation


def test_there_is_no_mutually_exclusive_mode_selector():
    assert set(ui._LocalCommand) == {
        ui._LocalCommand.ARM,
        ui._LocalCommand.STOP,
        ui._LocalCommand.TAKEOVER,
        ui._LocalCommand.TRANSCRIPT,
    }


def test_main_transcript_toggle_is_visibility_only(surface_factory):
    surface, controller, adapter = surface_factory()
    visibility = [True]
    surface._transcript_visible = lambda: visibility[0]
    surface._on_transcript_toggle = lambda: visibility.__setitem__(0, not visibility[0])
    surface.start()
    generation = controller.snapshot().generation
    adapter.invoke(lambda: surface._local_command(ui._LocalCommand.TRANSCRIPT))
    assert not adapter.models[-1].transcript_visible
    assert not controller.snapshot().armed
    assert controller.snapshot().generation == generation
    adapter.invoke(lambda: surface._local_command(ui._LocalCommand.TRANSCRIPT))
    assert adapter.models[-1].transcript_visible


def test_capture_does_not_change_unified_permission(surface_factory):
    surface, controller, adapter = surface_factory()
    surface.start()
    adapter.invoke(lambda: surface._local_command(ui._LocalCommand.ARM))
    generation = controller.snapshot().generation
    with surface.capture_guard():
        assert adapter.cursor is None
    assert controller.snapshot().generation == generation
    assert controller.snapshot().armed


@pytest.mark.parametrize("takeover", [True, False])
def test_idle_input_notifications_keep_guidance_usable_with_either_takeover_preference(
    surface_factory, takeover
):
    surface, controller, adapter = surface_factory()
    surface.start()
    controller.update(human_takeover=takeover)
    adapter.invoke(lambda: surface._local_command(ui._LocalCommand.ARM))
    generation = controller.snapshot().generation
    revision = controller.snapshot().input_revision

    adapter.invoke(lambda: surface._human_input("mouse", 0, 0, kind="move", position=(-225, 91)))
    assert controller.snapshot().user_cursor == (-225, 91)
    assert controller.snapshot().input_revision == revision
    assert controller.snapshot().generation == generation
    assert controller.snapshot().armed
    assert ("human", "move", (-225, 91)) in controller.calls

    adapter.invoke(lambda: surface._human_input("mouse", 0, 0, kind="button", position=(-220, 95)))
    adapter.invoke(lambda: surface._human_input("keyboard", 0, 0, kind="key"))
    assert ("human", "button", (-220, 95)) in controller.calls
    assert ("human", "key", None) in controller.calls
    assert controller.snapshot().user_cursor == (-220, 95)
    assert controller.snapshot().input_revision == revision + 2
    assert controller.snapshot().generation == generation
    assert controller.snapshot().armed
    assert adapter.cursor is None
    adapter.invoke(surface._hotkey)
    assert not controller.snapshot().armed


def test_physical_cursor_history_updates_while_stopped_but_injection_is_ignored(surface_factory):
    surface, controller, adapter = surface_factory()
    surface.start()
    adapter.invoke(lambda: surface._human_input("mouse", 0, 0, kind="move", position=(-300, 40)))
    assert controller.snapshot().user_cursor == (-300, 40)
    for flags, tag in ((1, 0), (2, 0), (0, INJECTED_INPUT_TAG)):
        adapter.invoke(
            lambda: surface._human_input("mouse", flags, tag, kind="move", position=(999, 999))
        )
    assert controller.snapshot().user_cursor == (-300, 40)
    assert not controller.snapshot().armed


def test_panel_close_stops_and_ends_the_control_thread(surface_factory):
    surface, controller, adapter = surface_factory()
    surface.start()
    adapter.invoke(lambda: surface._local_command(ui._LocalCommand.ARM))
    adapter.invoke(surface._panel_close)
    assert not controller.snapshot().armed
    assert adapter.cursor is None
    assert surface._closed.wait(1)
    assert adapter.events[-1] == "shutdown"
    assert surface.window_handles() == ()


def test_panel_x_requests_application_exit_instead_of_minimizing(surface_factory):
    surface, controller, adapter = surface_factory()
    exits = []
    surface._on_exit = lambda: exits.append("quit")
    surface.start()
    adapter.invoke(surface._panel_close)
    assert exits == ["quit"]
    assert not controller.snapshot().armed
    assert "minimize" not in adapter.events


def test_nested_capture_hides_until_last_exit_and_defers_show(surface_factory):
    surface, controller, adapter = surface_factory()
    surface.start()
    adapter.invoke(lambda: surface._local_command(ui._LocalCommand.ARM))
    controller.update(input_active=True)
    with surface.capture_guard():
        assert adapter.hidden and not adapter.visible and adapter.cursor is None
        with surface.capture_guard():
            surface.show()
            adapter.invoke(surface._refresh)
            assert adapter.cursor is None
            assert "show" not in adapter.events
        assert adapter.hidden
        assert "restore" not in adapter.events
    assert not adapter.hidden
    assert adapter.cursor == adapter.point
    assert adapter.events.count("hide-and-flush") == 1
    assert adapter.events.count("restore") == 1
    assert adapter.events.count("show") == 1
    assert controller.snapshot().armed


def test_capture_restores_after_body_exception_without_rearming(surface_factory):
    surface, controller, adapter = surface_factory()
    surface.start()
    with pytest.raises(ValueError, match="capture failed"):
        with surface.capture_guard():
            raise ValueError("capture failed")
    assert adapter.visible and not adapter.hidden
    assert not controller.snapshot().armed


def test_global_stop_still_works_during_capture_and_cursor_does_not_resume(surface_factory):
    surface, controller, adapter = surface_factory()
    surface.start()
    adapter.invoke(lambda: surface._local_command(ui._LocalCommand.ARM))
    with surface.capture_guard():
        adapter.invoke(surface._hotkey)
        assert not controller.snapshot().armed
        assert adapter.hidden
        assert adapter.cursor is None
    assert adapter.visible
    assert adapter.cursor is None
    assert not controller.snapshot().armed


def test_capture_can_run_on_owning_thread_without_waiting_on_itself(surface_factory):
    surface, _, adapter = surface_factory()
    surface.start()

    def local_capture():
        with surface.capture_guard():
            assert adapter.hidden
            with surface.capture_guard():
                assert adapter.hidden
        assert not adapter.hidden

    adapter.invoke(local_capture)
    assert adapter.events.count("hide-and-flush") == 1
    assert adapter.events.count("restore") == 1


def test_failed_hide_never_acknowledges_capture_and_revokes_control(surface_factory):
    surface, controller, adapter = surface_factory()
    surface.start()
    adapter.hide_error = OSError("DwmFlush failed")
    with pytest.raises(RuntimeError, match="DwmFlush"):
        with surface.capture_guard():
            pytest.fail("A failed hide cannot permit capture")
    assert surface._closed.wait(1)
    assert not controller.snapshot().armed
    assert not controller.snapshot().interface_ready


def test_restore_failure_keeps_original_capture_error_and_stops(surface_factory):
    surface, controller, adapter = surface_factory()
    surface.start()
    adapter.restore_error = OSError("Restore failed")
    with pytest.raises(ValueError, match="Capture failed") as caught:
        with surface.capture_guard():
            raise ValueError("Capture failed")
    assert any("restoration also failed" in note for note in caught.value.__notes__)
    assert surface._closed.wait(1)
    assert not controller.snapshot().armed


def test_overlapping_capture_guards_from_different_threads(surface_factory):
    surface, _, adapter = surface_factory()
    surface.start()
    entered = threading.Event()
    release = threading.Event()
    finished = threading.Event()
    errors = []

    def worker():
        try:
            with surface.capture_guard():
                entered.set()
                assert release.wait(1)
        except Exception as error:
            errors.append(error)
        finally:
            finished.set()

    thread = threading.Thread(target=worker)
    thread.start()
    try:
        assert entered.wait(1)
        with surface.capture_guard():
            release.set()
            assert finished.wait(1)
            assert adapter.hidden
            assert "restore" not in adapter.events
        assert not adapter.hidden
        assert adapter.events.count("restore") == 1
        assert not errors
    finally:
        release.set()
        thread.join(2)
        assert not thread.is_alive()


def test_capture_rejects_unstarted_and_closed_interfaces(surface_factory):
    surface, _, adapter = surface_factory()
    with pytest.raises(RuntimeError, match="not available"):
        with surface.capture_guard():
            pytest.fail("An unacknowledged capture must not run")
    surface.start()
    with surface.capture_guard():
        surface.close()
    assert "restore" not in adapter.events
    assert adapter.events[-1] == "shutdown"
    with pytest.raises(RuntimeError, match="not available"):
        with surface.capture_guard():
            pytest.fail("A closed surface must not allow capture")


def test_hotkey_startup_failure_propagates_and_cleans_partial_resources(surface_factory):
    adapter = FakeWin32()
    adapter.initialize_error = OSError("RegisterHotKey failed")
    surface, controller, adapter = surface_factory(adapter)
    with pytest.raises(RuntimeError, match="RegisterHotKey"):
        surface.start()
    assert surface._closed.wait(1)
    assert not controller.snapshot().armed
    assert not controller.snapshot().interface_ready
    assert "RegisterHotKey" in controller.snapshot().last_error
    assert adapter.events[-1] == "shutdown"
    assert surface.window_handles() == ()


def test_thread_creation_failure_revokes_control_without_an_unjoinable_thread(
    surface_factory, monkeypatch
):
    surface, controller, adapter = surface_factory()
    controller.update(state="ready", interface_ready=True)

    def fail_start(_thread):
        raise RuntimeError("Cannot create thread")

    monkeypatch.setattr(threading.Thread, "start", fail_start)
    with pytest.raises(RuntimeError, match="thread could not start"):
        surface.start()
    assert not controller.snapshot().armed
    assert not controller.snapshot().interface_ready
    assert surface._thread is None
    assert adapter.events == []
    surface.close()


def test_startup_timeout_is_bounded_and_never_sets_interface_ready(surface_factory, monkeypatch):
    adapter = FakeWin32()
    adapter.initialize_gate = threading.Event()
    surface, controller, adapter = surface_factory(adapter)
    monkeypatch.setattr(ui, "_START_TIMEOUT", 0.03)
    with pytest.raises(RuntimeError, match="did not become ready"):
        surface.start()
    assert not controller.snapshot().armed
    assert not controller.snapshot().interface_ready
    adapter.initialize_gate.set()
    assert surface._closed.wait(1)
    assert not any(call[:2] == ("interface", True) for call in controller.calls)


def test_unacknowledged_capture_times_out_and_stops_instead_of_capturing(
    surface_factory, monkeypatch
):
    surface, controller, adapter = surface_factory()
    surface.start()
    adapter.invoke(lambda: surface._local_command(ui._LocalCommand.ARM))
    adapter.pump_gate = threading.Event()
    adapter.wake()
    assert adapter.pump_blocked.wait(1)
    monkeypatch.setattr(ui, "_REQUEST_TIMEOUT", 0.03)
    with pytest.raises(RuntimeError, match="acknowledgement timed out"):
        with surface.capture_guard():
            pytest.fail("Timed-out capture must never execute")
    assert not controller.snapshot().armed
    assert not controller.snapshot().interface_ready
    adapter.pump_gate.set()
    assert surface._closed.wait(1)
    assert adapter.events[-1] == "shutdown"
    assert "hide-and-flush" not in adapter.events


def test_paint_failure_stops_and_reports_error(surface_factory):
    surface, controller, adapter = surface_factory()
    surface.start()
    adapter.render_error = OSError("Panel paint failed")
    controller.update(action="New action")
    adapter.wake()
    assert surface._closed.wait(1)
    assert not controller.snapshot().armed
    assert not controller.snapshot().interface_ready
    assert "Panel paint failed" in controller.snapshot().last_error
    assert surface.window_handles() == ()


def test_cleanup_error_is_not_silently_swallowed(surface_factory):
    surface, controller, adapter = surface_factory()
    surface.start()
    adapter.shutdown_error = OSError("UnhookWindowsHookEx failed")
    with pytest.raises(RuntimeError, match="cleanup failed"):
        surface.close()
    assert not surface._thread.is_alive()
    assert not controller.snapshot().armed
    assert "UnhookWindowsHookEx" in controller.snapshot().last_error


def test_window_styles_keep_panel_in_alt_tab_and_overlay_noninteractive():
    assert ui._PANEL_EX_STYLE & 0x00040000  # WS_EX_APPWINDOW
    assert not ui._PANEL_EX_STYLE & (0x80 | 0x08000000)
    assert ui._PANEL_STYLE & 0x00020000  # WS_MINIMIZEBOX
    assert ui._OVERLAY_EX_STYLE & 0x00080000  # WS_EX_LAYERED
    assert ui._OVERLAY_EX_STYLE & 0x00000020  # WS_EX_TRANSPARENT
    assert ui._OVERLAY_EX_STYLE & 0x00000008  # WS_EX_TOPMOST
    assert ui._OVERLAY_EX_STYLE & 0x00000080  # WS_EX_TOOLWINDOW
    assert ui._OVERLAY_EX_STYLE & 0x08000000  # WS_EX_NOACTIVATE


class HookEvent(ctypes.Structure):
    _fields_ = [("flags", ctypes.c_uint32), ("dwExtraInfo", ctypes.c_size_t)]


class HookPoint(ctypes.Structure):
    _fields_ = [("x", ctypes.c_int32), ("y", ctypes.c_int32)]


class MouseHookEvent(ctypes.Structure):
    _fields_ = [
        ("pt", HookPoint),
        ("mouseData", ctypes.c_uint32),
        ("flags", ctypes.c_uint32),
        ("time", ctypes.c_uint32),
        ("dwExtraInfo", ctypes.c_size_t),
    ]


def safety_adapter(register=True, fail_hook=None):
    """Exercise native registration logic using Python callables, not Win32."""
    calls = []
    adapter = object.__new__(ui._Win32Adapter)
    adapter.ctypes = ctypes
    adapter._panel = 0x100000010
    adapter._instance = 0x100000020
    adapter._wake_posted = False
    adapter._destroying = False
    adapter._hotkey_registered = False
    adapter._hooks = []
    adapter._hook_callbacks = []
    adapter.types = SimpleNamespace(
        HookProc=lambda callback: callback, KeyboardInput=HookEvent, MouseInput=MouseHookEvent
    )
    adapter._surface = SimpleNamespace(
        _human_input=lambda *args, **kwargs: calls.append(("human", *args, kwargs)),
        _record_failure=lambda error: calls.append(("failure", str(error))),
    )

    def hook(identifier, reference, instance, thread):
        calls.append(("hook", identifier, reference, instance, thread))
        return 0 if identifier == fail_hook else 0x100000100 + identifier

    adapter.user32 = SimpleNamespace(
        RegisterHotKey=lambda *args: calls.append(("register", *args)) or register,
        SetWindowsHookExW=hook,
        CallNextHookEx=lambda *args: calls.append(("next", *args)) or 0x100000050,
        PostMessageW=lambda *args: calls.append(("post", *args)) or True,
    )
    return adapter, calls


def test_native_registration_requires_global_nonrepeating_stop_and_keeps_callbacks():
    adapter, calls = safety_adapter()
    adapter._register_safety_controls()
    assert calls[0] == (
        "register",
        adapter._panel,
        ui._HOTKEY_ID,
        0x0002 | 0x0004 | 0x4000,
        ord("H"),
    )
    assert [call[1] for call in calls if call[0] == "hook"] == [13, 14]
    assert len(adapter._hook_callbacks) == len(adapter._hooks) == 2
    assert all(handle > 2**32 for handle in adapter._hooks)
    event = HookEvent(0x10, INJECTED_INPUT_TAG)
    keyboard, mouse = adapter._hook_callbacks
    assert keyboard(-1, 0, 1) == 0x100000050  # Negative hook codes must not dereference.
    assert keyboard(0, 0x100, ctypes.addressof(event)) == 0x100000050
    assert ("human", "keyboard", 0x10, INJECTED_INPUT_TAG, {"kind": "key"}) in calls
    mouse_event = MouseHookEvent(HookPoint(-50, 60), 0, 1, 0, INJECTED_INPUT_TAG)
    assert mouse(0, 0x200, ctypes.addressof(mouse_event)) == 0x100000050
    assert (
        "human",
        "mouse",
        1,
        INJECTED_INPUT_TAG,
        {"kind": "move", "position": (-50, 60)},
    ) in calls
    assert any(call[0] == "post" for call in calls)


@pytest.mark.parametrize(
    "message,kind",
    [
        (0x200, "move"),
        (0x201, "button"),
        (0x202, "button"),
        (0x204, "button"),
        (0x20A, "button"),
        (0x20E, "button"),
    ],
)
def test_native_mouse_hooks_forward_actual_event_kind_and_signed_position(message, kind):
    adapter, calls = safety_adapter()
    adapter._register_safety_controls()
    event = MouseHookEvent(HookPoint(-450, 123), 0, 0, 0, 0)
    assert adapter._hook_callbacks[1](0, message, ctypes.addressof(event)) == 0x100000050
    assert ("human", "mouse", 0, 0, {"kind": kind, "position": (-450, 123)}) in calls


def test_native_hotkey_failure_does_not_install_hooks():
    adapter, calls = safety_adapter(register=False)
    with pytest.raises(OSError, match="remains stopped"):
        adapter._register_safety_controls()
    assert not adapter._hotkey_registered
    assert not adapter._hooks
    assert not any(call[0] == "hook" for call in calls)


def test_partial_hook_failure_preserves_first_handle_for_cleanup():
    adapter, _ = safety_adapter(fail_hook=14)
    with pytest.raises(OSError, match="SetWindowsHookExW"):
        adapter._register_safety_controls()
    assert adapter._hotkey_registered
    assert adapter._hooks == [0x100000100 + 13]


def test_native_commands_only_map_clicked_owned_buttons_and_global_hotkey():
    adapter = object.__new__(ui._Win32Adapter)
    calls = []
    adapter._panel, adapter._overlay = 101, 102
    adapter._buttons = {ui._LocalCommand.ARM: 103, ui._LocalCommand.TAKEOVER: 104}
    adapter._labels = {}
    adapter._layout_ready = False
    adapter._destroying = False
    adapter.con = SimpleNamespace(
        WM_CLOSE=0x10,
        WM_HOTKEY=0x312,
        WM_COMMAND=0x111,
        BN_CLICKED=0,
        WM_PAINT=0xF,
        WM_ERASEBKGND=0x14,
        WM_DRAWITEM=0x2B,
        WM_DESTROY=2,
        WM_QUERYENDSESSION=0x11,
        WM_ENDSESSION=0x16,
        WM_CTLCOLORSTATIC=0x138,
        WM_SIZE=5,
        WM_DISPLAYCHANGE=0x7E,
        WM_SETTINGCHANGE=0x1A,
        SPI_SETWORKAREA=47,
    )
    adapter._surface = SimpleNamespace(
        _local_command=lambda command: calls.append(("command", command)),
        _hotkey=lambda: calls.append(("hotkey",)),
        _panel_close=lambda: calls.append(("close",)),
        _record_failure=lambda error: calls.append(("failure", str(error))),
    )
    adapter.gui = SimpleNamespace(DefWindowProc=lambda *args: 99)
    adapter._wndproc(101, 0x111, int(ui._LocalCommand.ARM), 0)
    adapter._wndproc(101, 0x111, int(ui._LocalCommand.ARM) | (1 << 16), 103)
    adapter._wndproc(101, 0x312, ui._HOTKEY_ID + 1, 0)
    assert calls == []
    adapter._wndproc(101, 0x111, int(ui._LocalCommand.ARM), 103)
    adapter._wndproc(101, 0x111, int(ui._LocalCommand.TAKEOVER), 104)
    adapter._wndproc(101, 0x312, ui._HOTKEY_ID, 0)
    adapter._wndproc(101, 0x10, 0, 0)
    assert calls == [
        ("command", ui._LocalCommand.ARM),
        ("command", ui._LocalCommand.TAKEOVER),
        ("hotkey",),
        ("close",),
    ]


@pytest.mark.parametrize("closing", [False, True])
def test_wake_dispatch_services_capture_and_shutdown_inside_a_native_modal_loop(
    surface_factory, closing
):
    wndproc = ui._Win32Adapter._wndproc
    surface, controller, adapter = surface_factory()
    surface.start()
    controller.update(state="ready")
    adapter.modal_exit = threading.Event()
    entered = threading.Event()
    errors = []
    native = SimpleNamespace(_surface=surface, _wake_posted=True)

    def modal_loop():
        entered.set()
        while not adapter.modal_exit.is_set():
            if adapter.wakeup.wait(0.01):
                adapter.wakeup.clear()
                wndproc(native, 101, ui._WAKE_MESSAGE, 0, 0)

    def enter_modal():
        try:
            adapter.invoke(modal_loop)
        except Exception as error:
            errors.append(error)

    opener = threading.Thread(target=enter_modal)
    opener.start()
    try:
        assert entered.wait(1)
        if closing:
            surface.close()
            assert "cancel-modal" in adapter.events
            assert not surface._thread.is_alive()
        else:
            with surface.capture_guard():
                assert adapter.hidden
                assert not adapter.modal_exit.is_set()
            assert not adapter.hidden
            assert controller.snapshot().interface_ready
            assert surface._error is None
    finally:
        adapter.modal_exit.set()
        adapter.wake()
        opener.join(2)
    assert not opener.is_alive()
    assert not errors


def test_reentrant_wake_does_not_acknowledge_another_capture_before_flush_finishes(
    surface_factory,
):
    wndproc = ui._Win32Adapter._wndproc
    surface, _, adapter = surface_factory()
    surface.start()
    native = SimpleNamespace(_surface=surface, _wake_posted=True)
    original_hide = adapter.hide_for_capture
    pending = ui._Request("capture_begin", object())

    def hide_with_reentrant_message():
        original_hide()
        surface._requests.put(pending)
        wndproc(native, 101, ui._WAKE_MESSAGE, 0, 0)
        assert not pending.done.is_set()
        with pytest.raises(RuntimeError, match="already being serviced"):
            surface._request("show")

    adapter.hide_for_capture = hide_with_reentrant_message
    with surface.capture_guard():
        assert pending.done.wait(1)
        assert pending.error is None
        surface._request("capture_end", pending.token)
        assert adapter.hidden
    assert not adapter.hidden
    assert surface._error is None


def test_native_modal_cancellation_targets_only_our_panel():
    adapter = object.__new__(ui._Win32Adapter)
    calls = []
    adapter._panel = 101
    adapter.con = SimpleNamespace(WM_CANCELMODE=0x1F)
    adapter.gui = SimpleNamespace(SendMessage=lambda *args: calls.append(args))
    adapter.cancel_modal()
    assert calls == [(101, 0x1F, 0, 0)]


@pytest.mark.parametrize("registration_fails", [False, True])
def test_paint_brush_stays_privately_owned_across_class_registration_and_shutdown(
    registration_fails,
):
    adapter = object.__new__(ui._Win32Adapter)
    classes, deleted = [], []
    adapter._instance = 100
    adapter._class_name = "SyntheticControl"
    adapter._wndproc_callback = lambda *args: 0
    adapter._registered_class = False
    adapter._background = 0
    adapter._panel = adapter._overlay = 0
    adapter._hooks = []
    adapter._hotkey_registered = False
    adapter._fonts = {}
    adapter._old_dpi_context = None
    adapter.con = SimpleNamespace(IDC_ARROW=1, IDI_APPLICATION=2)
    adapter.api = SimpleNamespace(RGB=lambda *args: 0)
    brushes = iter((500, 501))

    def register(window_class):
        classes.append(window_class)
        if registration_fails:
            raise OSError("Fixture class registration failed")

    def delete(handle):
        assert handle not in deleted, "A class-owned brush was deleted twice"
        deleted.append(handle)

    def unregister(*args):
        if classes[0].hbrBackground:
            delete(classes[0].hbrBackground)

    adapter.gui = SimpleNamespace(
        CreateSolidBrush=lambda color: next(brushes),
        WNDCLASS=SimpleNamespace,
        LoadCursor=lambda *args: 600,
        LoadIcon=lambda *args: 700,
        RegisterClass=register,
        UnregisterClass=unregister,
        DeleteObject=delete,
    )
    if registration_fails:
        with pytest.raises(OSError, match="registration failed"):
            adapter._register_window_class()
    else:
        adapter._register_window_class()
    assert classes[0].hbrBackground == 0
    adapter.shutdown()
    assert deleted == [500, 501]
    assert adapter._background == 0
    assert adapter._detail_background == 0


def test_native_unified_controls_expose_owned_handles_without_mode_tabs():
    adapter = object.__new__(ui._Win32Adapter)
    adapter._panel, adapter._overlay = 101, 102
    adapter._buttons = {command: 2000 + int(command) for command in ui._LocalCommand}
    adapter._labels = {"detail": 1101, "action": 1102}
    enabled = {}
    titles = []
    adapter.gui = SimpleNamespace(
        SetWindowText=lambda hwnd, title: titles.append((hwnd, title)),
        EnableWindow=lambda hwnd, value: enabled.update({hwnd: value}),
        InvalidateRect=lambda *args: None,
    )
    adapter.render_panel(
        ui._panel_model(ControlSnapshot("stopped", "Arm locally", interface_ready=True))
    )
    assert dict(titles)[adapter._panel] == "Desktop-MCP · Stopped"
    assert enabled[adapter._buttons[ui._LocalCommand.ARM]]
    assert len(adapter._buttons) == 4
    assert set(adapter._buttons.values()) <= set(adapter.window_handles())
    assert set(adapter._labels.values()) <= set(adapter.window_handles())


def test_native_accessible_names_publish_takeover_rejection_and_activity():
    adapter = object.__new__(ui._Win32Adapter)
    adapter._panel, adapter._overlay = 101, 102
    adapter._buttons = {command: int(command) for command in ui._LocalCommand}
    adapter._labels = {"detail": 1101, "action": 1102}
    text = {}
    adapter.gui = SimpleNamespace(
        SetWindowText=lambda handle, value: text.update({handle: value}),
        EnableWindow=lambda *args: None,
        InvalidateRect=lambda *args: None,
    )
    snapshot = ControlSnapshot(
        "stopped",
        "Arm not granted: input still finishing",
        action="Waiting for release",
        interface_ready=True,
        human_takeover=False,
    )
    adapter.render_panel(ui._panel_model(snapshot))
    assert "Off" in text[int(ui._LocalCommand.TAKEOVER)]
    assert text[1101] == snapshot.reason
    assert text[1102] == "Current action: Waiting for release"
    adapter.render_panel(ui._panel_model(replace(snapshot, human_takeover=True)))
    assert "On" in text[int(ui._LocalCommand.TAKEOVER)]


@pytest.mark.parametrize("dpi", [96, 144, 192, 288])
@pytest.mark.parametrize("work", [(0, 0, 800, 600), (-480, -320, 0, 0), (0, 0, 1920, 1080)])
def test_compact_layout_keeps_every_control_inside_the_work_area(dpi, work):
    from ctypes import wintypes

    adapter = object.__new__(ui._Win32Adapter)
    adapter._dpi = dpi
    adapter.ctypes, adapter.wintypes = ctypes, wintypes
    adapter._check = lambda result, operation: result

    def adjust(pointer, style, menu, extended, actual_dpi):
        rectangle = pointer._obj
        border = round(8 * actual_dpi / 96)
        rectangle.left -= border
        rectangle.right += border
        rectangle.top -= round(31 * actual_dpi / 96)
        rectangle.bottom += border
        return True

    adapter._adjust_rect = adjust
    adapter._fit_to_work_area(work)
    width, height = adapter._outer_size()
    assert width <= work[2] - work[0] and height <= work[3] - work[1]
    assert adapter._dpi == dpi
    assert 0 < adapter._layout_scale <= dpi / 96
    positions = {}
    adapter._buttons = {command: int(command) for command in ui._LocalCommand}
    adapter._labels = {"detail": 1101, "action": 1102}
    adapter.con = SimpleNamespace(SWP_NOACTIVATE=16, SWP_NOZORDER=4)
    adapter.gui = SimpleNamespace(
        SetWindowPos=lambda handle, after, x, y, w, h, flags: positions.update(
            {handle: (x, y, w, h)}
        )
    )
    adapter._layout_buttons()
    client_width, client_height = adapter._scale(ui._PANEL_WIDTH), adapter._scale(ui._PANEL_HEIGHT)
    for x, y, w, h in positions.values():
        assert 0 <= x < x + w <= client_width
        assert 0 <= y < y + h <= client_height
    assert int(ui._LocalCommand.STOP) in positions
    assert int(ui._LocalCommand.TAKEOVER) in positions


def test_reflow_clamps_origin_without_activation_or_recursive_layout():
    adapter = object.__new__(ui._Win32Adapter)
    adapter._layout_ready = True
    adapter._laying_out = False
    adapter._layout_scale = 2.0
    adapter._panel = 101
    events = []
    work = (-800, -600, 0, 0)
    adapter._fit_to_work_area = lambda area: setattr(adapter, "_layout_scale", 1.0)
    adapter._make_fonts = lambda: events.append("fonts")
    adapter._outer_size = lambda: (480, 390)
    adapter._layout_buttons = lambda: events.append("layout")
    adapter.con = SimpleNamespace(SWP_NOACTIVATE=16, SWP_NOZORDER=4)

    def position(*args):
        events.append(("position", *args))
        adapter._reflow_panel(work)

    adapter.gui = SimpleNamespace(
        GetWindowRect=lambda window: (500, 500, 1400, 1200),
        SetWindowPos=position,
        InvalidateRect=lambda *args: events.append("invalidate"),
    )
    adapter._reflow_panel(work)
    assert events == [
        "fonts",
        ("position", 101, 0, -480, -390, 480, 390, 20),
        "layout",
        "invalidate",
    ]
    assert not adapter._laying_out


def test_completed_same_dpi_monitor_move_reflows_into_smaller_work_area():
    from ctypes import wintypes
    import win32con

    adapter = object.__new__(ui._Win32Adapter)
    adapter.ctypes, adapter.wintypes, adapter.con = ctypes, wintypes, win32con
    adapter._panel, adapter._overlay = 101, 102
    adapter._labels = {}
    adapter._dpi, adapter._layout_scale = 192, 2.0
    adapter._layout_ready, adapter._laying_out = True, False
    adapter._destroying = False
    rectangle = [2000, 100, 2944, 874]
    work = (1920, 0, 2720, 600)
    calls = []

    def adjust(pointer, *args):
        rect = pointer._obj
        rect.left -= 16
        rect.right += 16
        rect.top -= 62
        rect.bottom += 16
        return True

    def position(window, after, x, y, width, height, flags):
        calls.append((x, y, width, height, flags))
        rectangle[:] = [x, y, x + width, y + height]
        adapter._wndproc(101, win32con.WM_SIZE, 0, 0)

    adapter._adjust_rect = adjust
    adapter._check = lambda result, operation: result
    adapter._work_area = lambda: work
    adapter._make_fonts = lambda: None
    adapter._layout_buttons = lambda: None
    adapter._surface = SimpleNamespace(
        _record_failure=lambda error: pytest.fail(f"Unexpected native dispatch failure: {error}")
    )
    adapter.gui = SimpleNamespace(
        IsIconic=lambda window: False,
        GetWindowRect=lambda window: tuple(rectangle),
        SetWindowPos=position,
        InvalidateRect=lambda *args: None,
        DefWindowProc=lambda *args: 99,
    )
    assert adapter._outer_size() == (944, 774)
    assert adapter._wndproc(101, win32con.WM_MOVE, 0, 0) == 99
    assert not calls
    assert adapter._wndproc(101, 0x0232, 0, 0) == 0
    assert adapter._dpi == 192
    assert adapter._outer_size() == (716, 600)
    assert len(calls) == 1
    x, y, width, height, flags = calls[0]
    assert work[0] <= x and x + width <= work[2]
    assert work[1] <= y and y + height <= work[3]
    assert flags & win32con.SWP_NOACTIVATE
    assert not adapter._laying_out


def test_native_fonts_use_logfont_objects_and_scale_for_dpi():
    adapter = object.__new__(ui._Win32Adapter)
    adapter._dpi = 144
    adapter._fonts = {"old": 50}
    adapter._labels = {}
    descriptions, deleted = [], []

    def create(description):
        assert isinstance(description, SimpleNamespace), "CreateFontIndirect requires LOGFONT"
        descriptions.append(description)
        return 100 + len(descriptions)

    adapter.con = SimpleNamespace(CLEARTYPE_QUALITY=5)
    adapter.gui = SimpleNamespace(
        LOGFONT=SimpleNamespace, CreateFontIndirect=create, DeleteObject=deleted.append
    )
    adapter._make_fonts()
    assert [font.lfHeight for font in descriptions] == [-34, -24, -21, -18, -21]
    assert [font.lfWeight for font in descriptions] == [600, 600, 400, 400, 600]
    assert all(font.lfFaceName == "Segoe UI" and font.lfQuality == 5 for font in descriptions)
    assert deleted == [50]
    assert len(adapter._fonts) == 5


@pytest.mark.parametrize(
    "visible,minimized,restore_command",
    [
        (True, False, 4),
        (True, True, 7),
        (False, False, None),
    ],
)
def test_native_capture_flushes_hiding_and_restores_without_activation(
    visible, minimized, restore_command
):
    adapter = object.__new__(ui._Win32Adapter)
    calls = []
    adapter._panel = 101
    adapter._overlay = 102
    adapter._cursor_visible = True
    adapter._capturing = False
    adapter.con = SimpleNamespace(SW_HIDE=0, SW_SHOWNOACTIVATE=4, SW_SHOWMINNOACTIVE=7)
    adapter.gui = SimpleNamespace(
        IsWindowVisible=lambda _: visible,
        IsIconic=lambda _: minimized,
        ShowWindow=lambda *args: calls.append(("show", *args)),
    )
    adapter.dwmapi = SimpleNamespace(DwmFlush=lambda: calls.append(("flush",)) or 0)
    adapter.hide_for_capture()
    assert calls == [("show", 102, 0), ("show", 101, 0), ("flush",)]
    assert adapter._capturing
    adapter.restore_after_capture()
    assert not adapter._capturing
    if restore_command is not None:
        assert calls[-1] == ("show", 101, restore_command)
    else:
        assert calls[-1] == ("flush",)


def test_native_minimize_uses_normal_windows_foreground_handoff():
    adapter = object.__new__(ui._Win32Adapter)
    calls = []
    adapter._panel = 101
    adapter._capturing = False
    adapter._return_window = 0
    adapter._surface = SimpleNamespace(_control_windows=lambda: (101, 102))
    adapter.con = SimpleNamespace(SW_MINIMIZE=6)
    adapter.gui = SimpleNamespace(
        ShowWindow=lambda *args: calls.append(args), GetForegroundWindow=lambda: 200
    )
    adapter.minimize_panel()
    assert calls == [(101, 6)]


@pytest.mark.parametrize("foreground", [0, 102, 200])
def test_local_minimize_returns_from_an_own_transcript_but_not_another_app(foreground):
    adapter = object.__new__(ui._Win32Adapter)
    calls = []
    adapter._panel = 101
    adapter._capturing = False
    adapter._return_window = 300
    adapter._surface = SimpleNamespace(_control_windows=lambda: (101, 102))
    adapter.con = SimpleNamespace(SW_MINIMIZE=6)
    adapter.gui = SimpleNamespace(
        ShowWindow=lambda *args: calls.append(("minimize", *args)),
        IsWindow=lambda window: window == 300,
        GetForegroundWindow=lambda: foreground,
        SetForegroundWindow=lambda window: calls.append(("foreground", window)),
    )
    adapter.minimize_panel()
    assert calls == [("minimize", 101, 6)] + (
        [("foreground", 300)] if foreground in {0, 102} else []
    )


def test_foreground_tracking_ignores_all_composed_own_windows():
    adapter = object.__new__(ui._Win32Adapter)
    adapter._return_window = 300
    adapter.gui = SimpleNamespace(GetForegroundWindow=lambda: 102)
    adapter.remember_foreground((101, 102))
    assert adapter._return_window == 300
    adapter.gui.GetForegroundWindow = lambda: 400
    adapter.remember_foreground((101, 102))
    assert adapter._return_window == 400


def test_native_overlay_moves_hotspot_without_focus_or_per_frame_bitmap_upload():
    adapter = object.__new__(ui._Win32Adapter)
    calls = []
    adapter._capturing = False
    adapter._layout_scale = 0.75
    adapter._sprite = render_cursor()
    adapter._sprite_dpi = 96
    adapter._get_window_dpi = lambda _: 96
    adapter.ctypes = ctypes
    adapter._overlay = 102
    adapter._cursor_visible = False
    adapter._cursor_at = None
    adapter._upload_cursor = lambda *args: calls.append(("upload", *args))
    adapter.con = SimpleNamespace(
        HWND_TOPMOST=-1, SWP_NOSIZE=1, SWP_NOACTIVATE=0x10, SW_SHOWNOACTIVATE=4
    )
    adapter.gui = SimpleNamespace(
        SetWindowPos=lambda *args: calls.append(("position", *args)),
        ShowWindow=lambda *args: calls.append(("show", *args)),
    )
    adapter.track_cursor((-50, 100))
    adapter.track_cursor((-50, 100))
    adapter.track_cursor((-45, 105))
    assert calls == [
        ("position", 102, -1, -56, 95, 0, 0, 0x11),
        ("show", 102, 4),
        ("position", 102, -1, -51, 100, 0, 0, 0x11),
    ]
    adapter._capturing = True
    adapter.track_cursor((999, 999))
    assert len(calls) == 3
    adapter._capturing = False
    adapter._get_window_dpi = lambda _: 192
    adapter.track_cursor((-45, 105))
    assert calls[-1][0] == "upload"
    assert calls[-1][1] == (-45, 105)
    assert calls[-1][2].hotspot == (12, 10)
    assert adapter._sprite_dpi == 192
