from types import SimpleNamespace
import ctypes

from PIL import Image
import pytest

from desktop_mcp.layers import upload_rgba
from desktop_mcp.teaching_ui import TeachingSurface, _DrawItem, _Request
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


def test_every_guidance_child_is_protected_from_input_targeting(surface):
    surface._editor, surface._status = 3, 4
    surface._buttons = {201: 5, 202: 6}
    assert surface.window_handles() == (1, 2, 3, 4, 5, 6)


def test_transcript_font_uses_logfont_and_updates_every_child(surface):
    descriptions = []
    events = surface._gui.events
    surface._editor, surface._status, surface._font = 3, 4, 50
    surface._buttons = {201: 5}
    surface._user32 = SimpleNamespace(GetDpiForWindow=lambda window: 144)
    surface._con.WM_SETFONT = 0x30
    surface._con.CLEARTYPE_QUALITY = 5
    surface._gui.LOGFONT = SimpleNamespace

    def create(description):
        assert isinstance(description, SimpleNamespace), "CreateFontIndirect requires LOGFONT"
        descriptions.append(description)
        return 60

    surface._gui.CreateFontIndirect = create
    surface._gui.SendMessage = lambda *args: events.append(("font", *args))
    surface._gui.DeleteObject = lambda handle: events.append(("delete", handle))
    surface._set_font()
    assert descriptions[0].lfFaceName == "Segoe UI"
    assert descriptions[0].lfHeight == -24
    assert descriptions[0].lfWeight == 400
    assert descriptions[0].lfQuality == 5
    assert events == [
        ("font", 3, 0x30, 60, True),
        ("font", 4, 0x30, 60, True),
        ("font", 5, 0x30, 60, True),
        ("delete", 50),
    ]
    assert surface._font == 60


@pytest.mark.parametrize("work", [(0, 0, 2560, 1528), (0, 0, 640, 360)])
def test_docking_accounts_for_scaled_minimum_size_before_positioning(surface, work):
    surface._scale = 1.5
    surface._work_area = lambda: work
    surface._gui.GetWindowRect = lambda handle: (80, 600, 760, 890)
    surface._con.SWP_NOZORDER = 4
    surface._dock("bottom")
    _, _, _, x, y, width, height, _ = surface._gui.events[-1]
    minimum = surface._minimum_size(work)
    assert width >= minimum[0] and height >= minimum[1]
    assert work[0] + 18 <= x and x + width <= work[2] - 18
    assert work[1] + 18 <= y and y + height == work[3] - 18


def test_rounded_buttons_clear_their_corners_to_the_panel_background(surface):
    events = surface._gui.events
    surface._background = 50
    surface._api = SimpleNamespace(RGB=lambda *values: 0)
    surface._con = SimpleNamespace(
        PS_SOLID=0, TRANSPARENT=1, DT_CENTER=1, DT_VCENTER=4, DT_SINGLELINE=32
    )
    surface._gui.CreateSolidBrush = lambda color: 41
    surface._gui.CreatePen = lambda *args: 42
    surface._gui.SelectObject = lambda *args: 43
    surface._gui.FillRect = lambda *args: events.append(("fill", *args))
    surface._gui.RoundRect = lambda *args: events.append(("round", *args))
    surface._gui.SetBkMode = surface._gui.SetTextColor = lambda *args: None
    surface._gui.GetWindowText = lambda window: "Pin"
    surface._gui.DrawText = surface._gui.DeleteObject = lambda *args: None
    item = _DrawItem()
    item.dc, item.window = 9, 5
    item.rect.right, item.rect.bottom = 120, 36
    surface._paint_button(ctypes.addressof(item))
    assert events[0] == ("fill", 9, (0, 0, 120, 36), 50)
    assert events[1][0] == "round"


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


def test_show_cannot_stamp_a_new_generation_after_its_checkpoint(surface, monkeypatch):
    original_checkpoint = surface.controller.checkpoint

    def stop_after_checking():
        original_checkpoint()
        surface.controller.stop("Fixture generation race")
        surface.controller.arm_local()

    def deliver(command, argument="", generation=None):
        request = dispatch(surface, command, argument=argument, generation=generation)
        if request.error is not None:
            raise request.error

    surface._request = deliver
    with pytest.raises(RuntimeError, match="revoked"):
        with surface.controller.operation("Fixture transcript"):
            monkeypatch.setattr(surface.controller, "checkpoint", stop_after_checking)
            surface.show("front")
    assert surface._gui.events == []


def test_native_initialization_failure_releases_every_waiter(surface):
    def unavailable():
        raise OSError("Fixture native import failure")

    surface._run_windows = unavailable
    request = _Request("hide")
    surface._requests.put(request)
    surface._run()
    assert surface._ready.is_set()
    assert surface._finished.is_set()
    assert request.done.is_set()
    assert request.error is not None
    assert not surface.controller.snapshot().armed


def test_close_still_joins_the_thread_when_the_command_cannot_be_posted(surface):
    events = []
    surface._thread = SimpleNamespace(
        is_alive=lambda: not surface._exit,
        join=lambda timeout: events.append(("join", timeout)),
    )

    def unavailable(command):
        raise RuntimeError("Fixture post failure")

    surface._request = unavailable
    with pytest.raises(RuntimeError, match="Fixture post"):
        surface.close()
    assert surface._exit
    assert events == [("join", 3)]


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
