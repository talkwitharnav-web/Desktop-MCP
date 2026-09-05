from types import SimpleNamespace

from PIL import Image
import pytest

from desktop_mcp.layers import upload_rgba
from desktop_mcp.teaching_ui import TeachingSurface, _Request
from tests.test_desktop_tools import FixtureApplication


class Gui:
    def __init__(self):
        self.visible = {1: True, 2: True}
        self.iconic = {1: False, 2: False}
        self.events = []

    def IsWindowVisible(self, handle):
        return self.visible[handle]

    def IsIconic(self, handle):
        return self.iconic[handle]

    def ShowWindow(self, handle, command):
        self.events.append(("show", handle, command))
        self.visible[handle] = command != 0
        self.iconic[handle] = command == 7

    def SetWindowPos(self, *arguments):
        self.events.append(("position", *arguments))


@pytest.fixture
def surface():
    application = FixtureApplication(armed=True)
    session = SimpleNamespace(clear_local=lambda: None)
    surface = TeachingSurface(application.controller, session)
    surface._panel, surface._canvas = 1, 2
    surface._gui = Gui()
    surface._con = SimpleNamespace(
        SW_HIDE=0,
        SW_SHOWNOACTIVATE=4,
        SW_SHOWMINNOACTIVE=7,
        HWND_BOTTOM=1,
        HWND_TOPMOST=-1,
        HWND_TOP=0,
        SWP_NOMOVE=2,
        SWP_NOSIZE=1,
        SWP_NOACTIVATE=16,
    )
    surface._native_error = OSError
    surface._dwm = SimpleNamespace(DwmFlush=lambda: 0)
    yield surface
    application.close()


def dispatch(surface, command, *, argument="", generation=None):
    request = _Request(command, argument, generation)
    surface._requests.put(request)
    surface._drain_requests()
    assert request.done.is_set()
    return request


def test_nested_guards_preserve_a_minimized_transcript(surface):
    surface._gui.iconic[1] = True
    assert dispatch(surface, "hide").error is None
    assert dispatch(surface, "hide").error is None
    assert not surface._gui.visible[1]
    assert dispatch(surface, "restore").error is None
    assert not surface._gui.visible[1]
    assert dispatch(surface, "restore").error is None
    assert surface._gui.visible[1]
    assert surface._gui.iconic[1]
    assert not surface._gui.visible[2]


def test_compositor_failure_restores_visibility_and_fails_the_guard(surface):
    surface._dwm.DwmFlush = lambda: -1
    request = dispatch(surface, "hide")
    assert isinstance(request.error, RuntimeError)
    assert surface._hide_count == 0
    assert surface._gui.visible[1]


def test_cancelled_hide_does_not_leave_a_hidden_window(surface):
    request = _Request("hide")
    surface._dwm.DwmFlush = lambda: request.cancelled.set() or 0
    surface._requests.put(request)
    surface._drain_requests()
    assert request.error is not None
    assert surface._hide_count == 0
    assert surface._gui.visible[1]


def test_a_local_pin_prevents_the_agent_lowering_the_transcript(surface):
    surface._pinned = True
    request = dispatch(
        surface, "show", argument="back", generation=surface.controller.snapshot().generation
    )
    assert request.error is not None
    assert surface._gui.events == []


def test_stale_model_window_requests_do_not_show_after_stop(surface):
    generation = surface.controller.snapshot().generation
    surface.controller.stop()
    request = dispatch(surface, "show", argument="front", generation=generation)
    assert request.error is not None
    assert surface._gui.events == []


def test_ink_window_bounds_are_cropped_to_the_actual_scene():
    snapshot = SimpleNamespace(
        marks=(SimpleNamespace(points=((100, 100), (200, 200))),),
        waiting=None,
    )
    assert TeachingSurface._scene_bounds(snapshot, (0, 0, 1920, 1080)) == (68, 68, 233, 233)
    snapshot.waiting = SimpleNamespace(center=(300, 400), radius=28)
    assert TeachingSurface._scene_bounds(snapshot, (0, 0, 1920, 1080)) == (68, 68, 361, 461)


def test_layer_upload_uses_premultiplied_pixels_without_a_native_window(monkeypatch):
    from windows_mcp.desktop import flash_overlay

    calls = []
    monkeypatch.setattr(flash_overlay, "_push_bitmap", lambda *args: calls.append(args))
    image = Image.new("RGBA", (2, 1), (200, 100, 50, 128))
    image.putpixel((1, 0), (255, 255, 255, 0))
    upload_rgba(123, (-100, 40), image)
    assert calls[0][:5] == (123, -100, 40, 2, 1)
    assert calls[0][-1] == bytes((25, 50, 100, 128, 0, 0, 0, 0))
