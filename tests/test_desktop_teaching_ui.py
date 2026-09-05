from types import SimpleNamespace
import ctypes

from PIL import Image, ImageFont
import pytest

from desktop_mcp.layers import upload_rgba
from desktop_mcp.teaching_ui import TeachingSurface, _DrawItem, _Request
from desktop_mcp.teaching import Mark, TeachingSnapshot, WaitTarget
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

    def SendMessage(self, *arguments):
        self.events.append(("message", *arguments))


@pytest.fixture
def surface():
    application = FixtureApplication(armed=True)
    session = SimpleNamespace(
        clear_local=lambda: None,
        conversation=application.teaching.conversation,
    )
    surface = TeachingSurface(application.controller, session)
    surface._panel, surface._canvas = 1, 2
    surface._composer, surface._send = 7, 8
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
        WM_CANCELMODE=0x1F,
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
    assert surface.window_handles() == (1, 2, 3, 7, 8, 4, 5, 6)


def test_transcript_x_requests_whole_application_exit_not_minimize(surface):
    exits = []
    surface._on_exit = lambda: exits.append("quit")
    surface._con.WM_TIMER = 0x113
    surface._con.WM_CLOSE = 0x10
    surface._procedure(surface._panel, surface._con.WM_CLOSE, 0, 0)
    assert exits == ["quit"]
    assert not surface.controller.snapshot().armed
    assert not any(event[0] == "show" for event in surface._gui.events)


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
        ("font", 7, 0x30, 60, True),
        ("font", 8, 0x30, 60, True),
        ("font", 5, 0x30, 60, True),
        ("delete", 50),
    ]
    assert surface._font == 60


@pytest.mark.parametrize("work", [(0, 0, 2560, 1528), (0, 0, 640, 360)])
def test_docking_accounts_for_scaled_minimum_size_before_positioning(surface, work):
    surface._scale = surface._dpi_scale = 1.5
    surface._work_area = lambda: work
    surface._gui.GetWindowRect = lambda handle: (80, 600, 760, 890)
    surface._con.SWP_NOZORDER = 4
    surface._dock("bottom")
    _, _, _, x, y, width, height, _ = surface._gui.events[-1]
    minimum = surface._minimum_size(work)
    assert width >= minimum[0] and height >= minimum[1]
    assert work[0] + 18 <= x and x + width <= work[2] - 18
    assert work[1] + 18 <= y and y + height == work[3] - 18


@pytest.mark.parametrize("scale,client", [(1.0, (444, 201)), (1.5, (666, 302)), (2.0, (888, 402))])
def test_wait_progress_has_its_own_readable_line_without_hiding_stop(surface, scale, client):
    surface._scale = surface._dpi_scale = scale
    surface._editor, surface._status = 3, 4
    surface._buttons = {201 + index: 10 + index for index in range(5)}
    surface._gui.GetClientRect = lambda window: (0, 0, *client)
    positions = {}
    surface._gui.MoveWindow = lambda window, x, y, w, h, repaint: positions.update(
        {window: (x, y, w, h)}
    )
    surface._layout()
    status_box = positions[4]
    assert status_box[3] >= round(48 * surface._scale)
    stop_box = positions[14]
    assert stop_box[1] + stop_box[3] <= client[1]
    assert positions[3][3] >= round(16 * surface._scale)
    assert positions[7][3] >= round(16 * surface._scale)
    assert positions[8][3] >= round(16 * surface._scale)

    snapshot = TeachingSnapshot(1, (), (), WaitTarget((10, 10), 28, True, 1.0, 1.0), None)
    surface.session.snapshot = lambda: snapshot
    surface._hide_count = 1
    texts = {}
    surface._gui.GetWindowText = lambda window: texts.get(window, "")
    surface._gui.SetWindowText = lambda window, text: texts.update({window: text})
    surface.controller.arm_local()
    with surface.controller.operation("Fixture cursor wait"):
        surface._refresh()
    lines = texts[4].splitlines()
    assert len(lines) == 2
    assert "Ctrl+Shift+H" in lines[0]
    assert "Your cursor: 100%" in lines[1]
    try:
        font = ImageFont.truetype("segoeui.ttf", round(16 * surface._scale))
    except OSError:
        pytest.skip("Segoe UI font metrics unavailable on this test host")
    assert all(font.getlength(line) <= status_box[2] for line in lines)


@pytest.mark.parametrize(
    "scale,work,chrome",
    [
        (2.0, (0, 0, 640, 360), (26, 71)),
        (3.0, (0, 0, 800, 600), (36, 103)),
        (3.0, (0, 0, 640, 360), (36, 103)),
    ],
)
def test_clamped_work_area_keeps_readable_editor_status_and_stop(surface, scale, work, chrome):
    surface._scale = surface._dpi_scale = scale
    surface._work_area = lambda: work
    surface._gui.GetWindowRect = lambda window: (80, 600, 760, 890)
    surface._con.SWP_NOZORDER = 4
    surface._dock("bottom")
    _, _, _, _, _, outer_width, outer_height, _ = surface._gui.events[-1]
    client = outer_width - chrome[0], outer_height - chrome[1]
    surface._editor, surface._status = 3, 4
    surface._buttons = {201 + index: 10 + index for index in range(5)}
    surface._gui.GetClientRect = lambda window: (0, 0, *client)
    positions = {}
    surface._gui.MoveWindow = lambda window, x, y, width, height, repaint: positions.update(
        {window: (x, y, width, height)}
    )
    surface._layout()
    font_height = round(16 * surface._scale)
    assert positions[3][3] >= font_height
    assert positions[14][3] >= font_height
    assert positions[7][3] >= font_height and positions[8][3] >= font_height
    assert surface._status_lines >= 1
    for x, y, width, height in positions.values():
        assert 0 <= x < x + width <= client[0]
        assert 0 <= y < y + height <= client[1]

    snapshot = TeachingSnapshot(1, (), (), WaitTarget((10, 10), 28, True, 1.0, 1.0), None)
    surface.session.snapshot = lambda: snapshot
    surface._hide_count = 1
    texts = {}
    surface._gui.GetWindowText = lambda window: texts.get(window, "")
    surface._gui.SetWindowText = lambda window, text: texts.update({window: text})
    surface.controller.arm_local()
    with surface.controller.operation("Fixture compact wait"):
        surface._refresh()
    lines = texts[4].splitlines()
    assert len(lines) <= surface._status_lines
    assert "Ctrl+Shift+H" in texts[4] and "100%" in texts[4]
    try:
        font = ImageFont.truetype("segoeui.ttf", font_height)
    except OSError:
        pytest.skip("Segoe UI font metrics unavailable on this test host")
    assert all(font.getlength(line) <= positions[4][2] for line in lines)


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


def test_chat_visibility_works_while_paused_but_never_after_close(surface):
    def deliver(command, argument="", generation=None):
        request = dispatch(surface, command, argument=argument, generation=generation)
        if request.error is not None:
            raise request.error

    surface._request = deliver
    surface.controller.stop()
    surface.show("front")
    assert surface.visible
    assert not surface.controller.snapshot().armed
    surface.controller.close()
    with pytest.raises(RuntimeError, match="closed"):
        surface.show("front")


def test_local_visibility_toggle_posts_without_blocking_the_stop_thread(surface):
    requests = []
    surface._shown = True
    surface._post = lambda *args, **kwargs: requests.append(args)
    surface._request = lambda *args, **kwargs: pytest.fail("Local toggle blocked on another UI")
    surface.toggle_local()
    assert requests == [("visibility", "off")]


def test_visibility_toggle_during_capture_is_applied_after_the_guard(surface):
    surface._shown = True
    assert dispatch(surface, "hide").error is None
    assert dispatch(surface, "visibility", argument="off").error is None
    assert not surface.enabled
    assert dispatch(surface, "restore").error is None
    assert not surface._gui.visible[1]
    assert dispatch(surface, "visibility", argument="on").error is None
    assert surface.visible
    assert not surface.controller.snapshot().input_active


def test_native_send_keeps_failed_drafts_and_clears_only_accepted_messages(surface):
    texts = {7: "I see it, but what does it do?"}
    surface._gui.GetWindowText = lambda handle: texts.get(handle, "")
    surface._gui.SetWindowText = lambda handle, text: texts.update({handle: text})
    surface._refresh = lambda: None
    surface.controller.stop()
    surface._send_user()
    assert texts[7] == ""
    assert surface.session.conversation.status()["pending_messages"] == 1
    assert not surface.controller.snapshot().armed
    texts[7] = " "  # Invalid input is retained, not silently discarded.
    surface._send_user()
    assert texts[7] == " "
    assert surface._message_error


def test_enter_sends_once_shift_enter_and_ime_stay_with_the_editor(surface):
    from ctypes import wintypes

    calls = []
    surface._con.WM_KEYDOWN, surface._con.VK_RETURN, surface._con.VK_SHIFT = 0x100, 13, 16
    surface._user32 = SimpleNamespace(GetKeyState=lambda key: 0)
    surface._composition_active = lambda: False
    surface._send_user = lambda: calls.append("send")
    message = wintypes.MSG(hWnd=7, message=0x100, wParam=13, lParam=0)
    assert surface._composer_key(message)
    message.lParam = 1 << 30
    assert surface._composer_key(message)
    assert calls == ["send"]
    message.lParam = 0
    surface._user32.GetKeyState = lambda key: 0x8000
    assert not surface._composer_key(message)
    surface._user32.GetKeyState = lambda key: 0
    surface._composition_active = lambda: True
    assert not surface._composer_key(message)


def test_clicking_the_chat_window_uses_normal_windows_activation(surface):
    import win32con

    surface._con = win32con
    surface._gui.DefWindowProc = lambda *args: 47
    surface._gui.GetForegroundWindow = lambda: 999
    assert surface._procedure(surface._panel, win32con.WM_MOUSEACTIVATE, 0, 0) == 47


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


def test_close_command_cancels_the_owned_transcript_modal_loop(surface):
    request = dispatch(surface, "close")
    assert request.error is None
    assert surface._exit
    assert ("message", 1, 0x1F, 0, 0) in surface._gui.events


def test_fatal_dispatch_cancels_modal_processing_without_reentering_ui_logic(surface):
    from desktop_mcp.teaching_ui import _COMMAND

    def failed_dispatch():
        raise OSError("Fixture native failure")

    surface._drain_requests = failed_dispatch
    surface._gui.DefWindowProc = lambda *args: 99
    surface._gui.SendMessage = lambda *args: surface._gui.events.append(
        ("cancel", surface._procedure(1, 0x0232, 0, 0))
    )
    assert surface._procedure(1, _COMMAND, 0, 0) == 0
    assert surface._exit
    assert not surface.controller.snapshot().interface_ready
    assert surface._gui.events == [("cancel", 99)]


def test_ink_window_bounds_are_cropped_to_the_actual_scene():
    mark = Mark("ink", "path", ((100, 100), (200, 200)), "#ffb454", 3, 0, None, None)
    snapshot = TeachingSnapshot(1, (), (mark,), None, None)
    assert TeachingSurface._scene_bounds(snapshot, (0, 0, 1920, 1080)) == (95, 95, 206, 206)
    snapshot = TeachingSnapshot(2, (), (mark,), WaitTarget((300, 400), 28, False, 0, 0), None)
    assert TeachingSurface._scene_bounds(snapshot, (0, 0, 1920, 1080)) == (95, 95, 333, 433)


def test_scene_bounds_include_wide_laser_glow():
    mark = Mark("laser", "laser", ((200, 200), (230, 220)), "#ffb454", 32, 0, 2, None)
    snapshot = TeachingSnapshot(1, (), (mark,), None, None)
    assert TeachingSurface._scene_bounds(snapshot, (0, 0, 1920, 1080), now=1) == (72, 72, 359, 349)


def test_oversized_transient_scene_is_hidden_without_killing_the_interface(surface):
    mark = Mark("ink", "path", ((100, 100), (9000, 100)), "#ffb454", 3, 0, None, None)
    surface.session.snapshot = lambda: TeachingSnapshot(1, (), (mark,), None, None)
    surface._api = SimpleNamespace(
        GetSystemMetrics=lambda code: {76: 0, 77: 0, 78: 11520, 79: 2160}[code]
    )
    surface._gui.GetWindowText = lambda handle: ""
    surface._gui.SetWindowText = lambda *args: surface._gui.events.append(("text", *args))
    surface._refresh()
    assert not surface._gui.visible[2]
    assert not surface._exit
    assert surface.controller.snapshot().interface_ready
    assert any("scene too large" in str(event) for event in surface._gui.events)


def test_layer_upload_uses_premultiplied_pixels_without_a_native_window(monkeypatch):
    from windows_mcp.desktop import flash_overlay

    calls = []
    monkeypatch.setattr(flash_overlay, "_push_bitmap", lambda *args: calls.append(args))
    image = Image.new("RGBA", (2, 1), (200, 100, 50, 128))
    image.putpixel((1, 0), (255, 255, 255, 0))
    upload_rgba(123, (-100, 40), image)
    assert calls[0][:5] == (123, -100, 40, 2, 1)
    assert calls[0][-1] == bytes((25, 50, 100, 128, 0, 0, 0, 0))
