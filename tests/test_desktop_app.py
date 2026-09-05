from contextlib import contextmanager
import threading
from types import SimpleNamespace

import pytest

from desktop_mcp.app import DesktopApplication
from desktop_mcp.contracts import CaptureContext
from desktop_mcp.conversation import Conversation
from desktop_mcp.runtime import Controller, DesktopStopped
from tests.test_desktop_runtime import FakeInput


@pytest.fixture
def app():
    application = DesktopApplication.__new__(DesktopApplication)
    application.controller = Controller(FakeInput())
    application.controller.set_interface_ready(True)
    application.controller.arm_local()
    application.surface = SimpleNamespace(window_handles=lambda: (10, 11))
    application.teaching_surface = SimpleNamespace(window_handles=lambda: (20, 21))
    application.teaching = SimpleNamespace(
        conversation=Conversation(
            is_closed=lambda: application.controller.snapshot().state == "closed"
        )
    )
    yield application
    application.controller.close()


def test_all_native_windows_are_protected(app):
    assert app.window_handles() == (10, 11, 20, 21)


def test_local_quit_revokes_and_signals_the_host_without_joining_ui_threads(app):
    app.exit_requested = threading.Event()
    app.request_exit()
    assert not app.controller.snapshot().armed
    assert app.exit_requested.is_set()


def test_start_publishes_arming_only_after_teaching_is_ready(app):
    events = []
    app.teaching_surface.start = lambda: events.append("teaching")
    app.surface.start = lambda: events.append("control")
    app.start()
    assert events == ["teaching", "control"]


def test_failed_guidance_start_cannot_leave_control_armed(app):
    def unavailable():
        raise RuntimeError("Fixture startup failure")

    app.teaching_surface.start = unavailable
    app.surface.start = lambda: pytest.fail("Control started after guidance failed")
    with pytest.raises(RuntimeError, match="Fixture startup"):
        app.start()
    assert not app.controller.snapshot().armed
    assert not app.controller.snapshot().interface_ready


def test_close_attempts_every_resource_even_if_a_native_window_fails(app):
    events = []

    def fail_guidance():
        events.append("teaching")
        assert app.controller.snapshot().state == "closed"
        raise RuntimeError("Fixture close failure")

    app.teaching_surface.close = fail_guidance
    app.surface.close = lambda: events.append("control")
    app.vision = SimpleNamespace(invalidate=lambda: events.append("vision"))
    app.image_files = SimpleNamespace(close=lambda: events.append("images"))
    with pytest.raises(RuntimeError, match="Fixture close"):
        app.close()
    assert events == ["teaching", "control", "vision", "images"]


def test_capture_hides_every_surface_and_restores_in_reverse_order(app):
    events = []

    @contextmanager
    def guard(name):
        events.append(("hide", name))
        try:
            yield
        finally:
            events.append(("restore", name))

    app.surface.capture_guard = lambda: guard("control")
    app.teaching_surface.capture_guard = lambda: guard("teaching")
    with app.controller.operation("fixture capture"), app.capture_guard():
        events.append(("capture", "fixture"))
    assert events == [
        ("hide", "control"),
        ("hide", "teaching"),
        ("capture", "fixture"),
        ("restore", "teaching"),
        ("restore", "control"),
    ]


@pytest.mark.parametrize("stop_instead", [False, True])
def test_guard_failure_or_revocation_never_falls_back_to_an_unguarded_capture(app, stop_instead):
    events = []

    @contextmanager
    def control_guard():
        try:
            yield
        finally:
            events.append("restored")

    @contextmanager
    def teaching_guard():
        if stop_instead:
            app.controller.stop("Fixture stop during hide")
        else:
            raise RuntimeError("Fixture compositor failure")
        yield

    app.surface.capture_guard = control_guard
    app.teaching_surface.capture_guard = teaching_guard
    with pytest.raises(DesktopStopped if stop_instead else RuntimeError):
        with app.controller.operation("fixture capture"), app.capture_guard():
            pytest.fail("A capture proceeded after a failed guard")
    assert events == ["restored"]


@pytest.mark.parametrize("scope", ["active", "desktop"])
def test_teaching_context_preserves_scope_even_for_a_fullscreen_app(app, scope):
    context = CaptureContext(1, (0, 0, 100, 100), (0, 0, 100, 100), scope=scope)
    calls = []

    def read_context(*, scope):
        calls.append(scope)
        return context

    app.capture = SimpleNamespace(context=read_context)
    # This read is called by the UI timer, outside Controller.operation.
    assert app.teaching_context(context) == context
    assert calls == [scope]


@pytest.mark.parametrize("window", [0, 10, 11, 20, 21])
def test_teaching_has_no_context_for_missing_targets_or_our_windows(app, window):
    app.capture = SimpleNamespace(
        context=lambda **kwargs: CaptureContext(window, (0, 0, 100, 100), (0, 0, 100, 100))
    )
    assert app.teaching_context(None) is None


def test_target_disappearing_clears_teaching_context_without_capturing(app):
    def missing(**kwargs):
        raise OSError("Fixture foreground disappeared")

    app.capture = SimpleNamespace(context=missing)
    assert app.teaching_context(None) is None
