from types import SimpleNamespace
from bisect import bisect_right
from dataclasses import replace
import ctypes

from PIL import Image, ImageFont
import pytest
import win32con

from desktop_mcp.layers import upload_rgba
from desktop_mcp.teaching_ui import (
    TeachingSurface,
    _DrawItem,
    _MinMaxInfo,
    _Request,
    _TextMetric,
    _TrackMouseEvent,
    _REFLOW,
    _CLIENT_AREA_ANIMATION,
    _ANIMATION_TIMER_MS,
    _IDLE_TIMER_MS,
)
from desktop_mcp.teaching import Mark, TeachingSnapshot, WaitTarget
from desktop_mcp.transcript_layout import (
    BOTTOM,
    CLEAR,
    COMPOSER,
    COMPOSER_LABEL,
    COMPOSER_SCROLL,
    EXPAND,
    HISTORY,
    HISTORY_LABEL,
    HISTORY_SCROLL,
    LATEST,
    PIN,
    SEND,
    STATUS,
    STOP,
    TASKBAR,
    TOP,
    layout_client,
    usable_area,
)
from desktop_mcp.transcript_scroll import ScrollState, thumb_geometry
from tests.test_desktop_tools import FixtureApplication


class Gui:
    def __init__(self):
        self.visible = {1: True, 2: True}
        self.iconic = {1: False, 2: False}
        self.zoomed = False
        self.events = []
        self.rect = (80, 80, 1200, 244)
        self.chrome = (16, 39)
        self.positions = {}
        self.texts = {}
        self.selections = {}
        self.first_lines = {}
        self.created = {}
        self.font_height = 14
        self.font_face = "Segoe UI Variable Text"
        self.client_animations = True
        self.foreground = 999
        self.focused = 0
        self.capture = 0
        self.on_capture_changed = None
        self.on_end_defer = None
        self.on_redraw = None
        self.subclasses = {}
        self.window_styles = {3: 0, 7: 0}
        self.extra_styles = {7: win32con.WS_EX_CLIENTEDGE}
        self.margins = {}
        self.wheel_lines = 3
        self.batch = []
        self.native_key = None
        self._gdi_handle = 800
        self.LOGFONT = SimpleNamespace

    def IsWindowVisible(self, handle):
        return self.visible.get(handle, True) and (handle in (1, 2) or self.visible.get(1, True))

    def IsIconic(self, handle):
        return self.iconic.get(handle, False)

    def ShowWindow(self, handle, command):
        self.events.append(("show", handle, command))
        self.visible[handle] = command != 0
        self.iconic[handle] = command == 7
        if command == win32con.SW_RESTORE:
            self.zoomed = False

    def SetWindowPos(self, handle, target, x, y, width, height, flags):
        self.events.append(("position", handle, target, x, y, width, height, flags))
        if handle == 1:
            if flags & win32con.SWP_NOMOVE:
                x, y = self.rect[:2]
            if flags & win32con.SWP_NOSIZE:
                width, height = self.rect[2] - self.rect[0], self.rect[3] - self.rect[1]
            self.rect = (x, y, x + width, y + height)

    def GetWindowRect(self, handle):
        return self.rect

    def GetClientRect(self, handle):
        if handle == 1:
            return (
                0,
                0,
                self.rect[2] - self.rect[0] - self.chrome[0],
                self.rect[3] - self.rect[1] - self.chrome[1],
            )
        width, height = self.positions.get(handle, (0, 0, 600, 100))[2:]
        border = 4 if self.extra_styles.get(handle, 0) & win32con.WS_EX_CLIENTEDGE else 0
        return 0, 0, max(0, width - border), max(0, height - border)

    def MoveWindow(self, handle, x, y, width, height, repaint):
        self.events.append(("move", handle, x, y, width, height, repaint))
        self.positions[handle] = (x, y, width, height)
        self._clamp_scroll(handle)

    def GetWindowText(self, handle):
        return self.texts.get(handle, "")

    def SetWindowText(self, handle, text):
        self.events.append(("text", handle, text))
        callback = self.subclasses.get(handle)
        if callback is not None:
            callback(handle, win32con.WM_SETTEXT, 0, text, 1, 0)
        else:
            self.store_text(handle, text)

    def store_text(self, handle, text):
        self.texts[handle] = text
        self.selections[handle] = (0, 0)
        self.first_lines[handle] = 0

    def _lines(self, handle):
        margin = self.margins.get(handle, 4)
        scrollbar = 17 if self.window_styles.get(handle, 0) & win32con.WS_VSCROLL else 0
        columns = max(
            1,
            (self.GetClientRect(handle)[2] - 2 * margin - scrollbar)
            // max(1, self.font_height // 2),
        )
        lines, offset = [], 0
        for paragraph in self.texts.get(handle, "").split("\r\n"):
            length = len(paragraph.encode("utf-16-le")) // 2
            lines.extend(offset + index for index in range(0, max(1, length), columns))
            offset += length + 2
        return lines

    def _page(self, handle):
        return max(1, self.GetClientRect(handle)[3] // (self.font_height + 2))

    def _clamp_scroll(self, handle):
        maximum = max(0, len(self._lines(handle)) - self._page(handle))
        self.first_lines[handle] = min(maximum, max(0, self.first_lines.get(handle, 0)))

    def GetScrollInfo(self, handle, bar, mask):
        if not self.window_styles.get(handle, 0) & win32con.WS_VSCROLL:
            raise OSError(1447, "GetScrollInfo: no scrollbars")
        self._clamp_scroll(handle)
        return 0, 0, len(self._lines(handle)) - 1, self._page(handle), self.first_lines[handle], 0

    def SendMessage(self, handle, message, wparam, lparam):
        self.events.append(("message", handle, message, wparam, lparam))
        callback = self.subclasses.get(handle)
        if callback is not None:
            return callback(handle, message, wparam, lparam, 1, 0)
        return self.native_message(handle, message, wparam, lparam)

    def native_message(self, handle, message, wparam, lparam):
        if message == win32con.WM_GETTEXTLENGTH:
            return len(self.texts.get(handle, "").encode("utf-16-le")) // 2
        if message == win32con.WM_GETTEXT:
            encoded = self.texts.get(handle, "").encode("utf-16-le")[: max(0, wparam - 1) * 2]
            ctypes.memmove(lparam, encoded + b"\0\0", len(encoded) + 2)
            return len(encoded) // 2
        if message == win32con.EM_GETSEL:
            start, end = self.selections.get(handle, (0, 0))
            ctypes.cast(wparam, ctypes.POINTER(ctypes.wintypes.DWORD)).contents.value = start
            ctypes.cast(lparam, ctypes.POINTER(ctypes.wintypes.DWORD)).contents.value = end
            return (start & 0xFFFF) | ((end & 0xFFFF) << 16)
        if message == win32con.EM_SETSEL:
            self.selections[handle] = wparam, lparam
        elif message == win32con.WM_SETTEXT:
            self.store_text(handle, lparam)
            return 1
        elif message == win32con.WM_SETREDRAW:
            self.visible[handle] = bool(wparam)
        elif message == win32con.EM_GETLINECOUNT:
            return len(self._lines(handle))
        elif message == win32con.EM_GETRECT:
            rectangle = ctypes.cast(lparam, ctypes.POINTER(ctypes.wintypes.RECT)).contents
            _, _, width, height = self.GetClientRect(handle)
            rectangle.left = self.margins.get(handle, 4)
            rectangle.top, rectangle.right, rectangle.bottom = 0, width - rectangle.left, height
        elif message == win32con.EM_SETMARGINS:
            self.margins[handle] = lparam & 0xFFFF
        elif message == win32con.EM_GETFIRSTVISIBLELINE:
            return self.first_lines.get(handle, 0)
        elif message == win32con.EM_LINEINDEX:
            lines = self._lines(handle)
            return lines[min(max(0, wparam), len(lines) - 1)]
        elif message == win32con.EM_LINEFROMCHAR:
            return max(0, bisect_right(self._lines(handle), wparam) - 1)
        elif message == win32con.EM_LINESCROLL:
            self.first_lines[handle] = self.first_lines.get(handle, 0) + lparam
            self._clamp_scroll(handle)
        elif message == win32con.EM_SCROLLCARET:
            if not self.IsWindowVisible(handle):
                return 0
            caret = self.selections.get(handle, (0, 0))[1]
            line = max(0, bisect_right(self._lines(handle), caret) - 1)
            first = self.first_lines.get(handle, 0)
            if line < first:
                self.first_lines[handle] = line
            elif line >= first + self._page(handle):
                self.first_lines[handle] = line - self._page(handle) + 1
            self._clamp_scroll(handle)
        elif message == win32con.WM_KEYDOWN and self.native_key:
            self.native_key(handle, wparam)
        return 0

    def CreateWindowEx(self, *arguments):
        identifier = arguments[9]
        handle = 1000 + identifier
        self.created[handle] = arguments
        self.window_styles[handle] = arguments[3]
        self.extra_styles[handle] = arguments[0]
        self.texts[handle] = arguments[2]
        self.positions[handle] = tuple(arguments[4:8])
        return handle

    def CreateFontIndirect(self, description):
        self.font_height = -description.lfHeight
        self.font_face = description.lfFaceName
        return 60

    def DeleteObject(self, handle):
        self.events.append(("delete", handle))

    def InvalidateRect(self, *arguments):
        self.events.append(("invalidate", *arguments))

    def GetForegroundWindow(self):
        return self.foreground

    def SetFocus(self, handle):
        self.focused = handle
        self.events.append(("focus", handle))

    def GetFocus(self):
        return self.focused

    def GetClassName(self, handle):
        return "FixtureGuidance"

    def GetDC(self, handle):
        return 900

    def ReleaseDC(self, handle, dc):
        self.events.append(("release-dc", handle, dc))

    def SelectObject(self, dc, obj):
        self.events.append(("select", dc, obj))
        return 901

    def get_text_metrics(self, dc, pointer):
        metrics = ctypes.cast(pointer, ctypes.POINTER(_TextMetric)).contents
        metrics.height = self.font_height + 2
        return True

    def get_text_face(self, dc, count, buffer):
        buffer.value = self.font_face[: count - 1]
        return len(buffer.value)

    def begin_defer(self, count):
        self.events.append(("begin-defer", count))
        self.batch = []
        return 400

    def defer(self, batch, handle, target, x, y, width, height, flags):
        assert batch == 400
        self.events.append(("defer", handle, x, y, width, height, flags))
        self.batch.append((handle, x, y, width, height, flags))
        return batch

    def end_defer(self, batch):
        assert batch == 400
        for handle, x, y, width, height, flags in self.batch:
            self.MoveWindow(handle, x, y, width, height, False)
            if flags & win32con.SWP_SHOWWINDOW:
                self.visible[handle] = True
            elif flags & win32con.SWP_HIDEWINDOW:
                self.visible[handle] = False
        self.events.append(("end-defer",))
        if self.on_end_defer:
            self.on_end_defer()
        return True

    def RedrawWindow(self, handle, rect, region, flags):
        self.events.append(("redraw", handle, flags))
        if self.on_redraw:
            self.on_redraw(handle, flags)

    def PostMessage(self, *arguments):
        self.events.append(("post", *arguments))

    def SetCapture(self, handle):
        old, self.capture = self.capture, handle
        self.events.append(("capture", handle))
        return old

    def GetCapture(self):
        return self.capture

    def ReleaseCapture(self):
        old, self.capture = self.capture, 0
        self.events.append(("release-capture", old))
        if self.on_capture_changed and old:
            self.on_capture_changed(old, 0x0215, 0, 0)

    def system_parameters(self, action, unused, pointer, flags):
        assert not flags
        if action == _CLIENT_AREA_ANIMATION:
            ctypes.cast(
                pointer, ctypes.POINTER(ctypes.wintypes.BOOL)
            ).contents.value = self.client_animations
        else:
            assert action == 0x0068
            ctypes.cast(
                pointer, ctypes.POINTER(ctypes.wintypes.UINT)
            ).contents.value = self.wheel_lines
        return True

    def track_mouse(self, pointer):
        item = ctypes.cast(pointer, ctypes.POINTER(_TrackMouseEvent)).contents
        self.events.append(("track-mouse", item.window, item.flags))
        return True

    def set_subclass(self, handle, callback, identifier, data):
        self.subclasses[handle] = callback
        self.events.append(("subclass", handle, identifier))
        return True

    def remove_subclass(self, handle, callback, identifier):
        self.subclasses.pop(handle, None)
        self.events.append(("remove-subclass", handle, identifier))
        return True

    def default_subclass(self, handle, message, wparam, lparam):
        self.events.append(("default-edit", handle, message, wparam, lparam))
        return self.native_message(handle, message, wparam, lparam)

    def BeginPaint(self, handle):
        self.events.append(("begin-paint", handle))
        return 900, ("fixture-paint", handle)

    def EndPaint(self, handle, paint):
        self.events.append(("end-paint", handle))

    def FillRect(self, dc, rectangle, brush):
        self.events.append(("fill", dc, rectangle, brush))

    def CreateSolidBrush(self, color):
        self._gdi_handle += 1
        self.events.append(("brush", color))
        return self._gdi_handle

    def CreatePen(self, style, width, color):
        self._gdi_handle += 1
        return self._gdi_handle

    def RoundRect(self, *arguments):
        self.events.append(("round", *arguments))

    def DrawFocusRect(self, *arguments):
        self.events.append(("focus-rect", *arguments))

    def DefWindowProc(self, *arguments):
        return 47


class History:
    """Recording component port; rendering and native selection belong to its own tests."""

    def __init__(self, gui, *, on_change=None, on_error=None, hwnd=0):
        self.gui = gui
        self.hwnd = hwnd
        self.on_change = on_change
        self.on_error = on_error
        self.calls = []
        self.entries = ()
        self.view = object()
        self.state = ScrollState(1200, 120, 0)
        self.following = True
        self.unread = False
        self.animation_active = False
        self.interacting = False
        self.roles = {}

    def create(self, parent, instance, control_id):
        self.calls.append(("create", parent, instance, control_id))
        self.hwnd = self.gui.CreateWindowEx(
            win32con.WS_EX_CONTROLPARENT,
            "FixtureChatHistory",
            "",
            win32con.WS_CHILD | win32con.WS_VISIBLE | win32con.WS_TABSTOP,
            0,
            0,
            1,
            1,
            parent,
            control_id,
            instance,
            None,
        )
        return self.hwnd

    def close(self):
        self.calls.append(("close",))
        self.hwnd = 0
        self.roles = {}

    def set_entries(self, entries, *, now=None, animate=True):
        self.calls.append(("entries", entries, now, animate))
        changed = entries != self.entries
        self.entries = entries
        return changed

    def set_font(self, font, *, line_height, scale=1.0):
        self.calls.append(("font", font, line_height, scale))

    def reflow(self, width, height):
        self.calls.append(("reflow", width, height))

    def capture_view(self):
        self.calls.append(("capture-view",))
        return self.view

    def restore_view(self, view):
        self.calls.append(("restore-view", view))
        assert view is self.view

    def latest(self):
        self.calls.append(("latest",))
        self.following, self.unread = True, False
        self.state = ScrollState(self.state.lines, self.state.page, self.state.maximum)

    def scroll_state(self):
        return self.state

    def scroll_to(self, position):
        self.calls.append(("scroll-to", position))
        self.state = ScrollState(self.state.lines, self.state.page, self.state.clamp(position))

    def scroll_command(self, command):
        self.calls.append(("scroll-command", command))
        return True

    def wheel(self, delta, lines_per_notch=3):
        self.calls.append(("wheel", delta, lines_per_notch))

    def tick(self, now):
        self.calls.append(("tick", now))
        return self.animation_active

    def set_interacting(self, active):
        self.calls.append(("interacting", active))
        self.interacting = active

    def cancel_animation(self):
        self.calls.append(("cancel-animation",))
        self.animation_active = False

    def cancel_interaction(self):
        self.calls.append(("cancel-interaction",))
        self.interacting = False
        self.animation_active = False

    def window_handles(self):
        return tuple(self.window_roles())

    def window_roles(self):
        return ({self.hwnd: "transcript-history"} if self.hwnd else {}) | self.roles


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
    surface._con = win32con
    surface._user32 = SimpleNamespace(
        GetDpiForWindow=lambda window: 96,
        IsZoomed=lambda window: surface._gui.zoomed,
        BeginDeferWindowPos=surface._gui.begin_defer,
        DeferWindowPos=surface._gui.defer,
        EndDeferWindowPos=surface._gui.end_defer,
        SystemParametersInfoW=surface._gui.system_parameters,
        TrackMouseEvent=surface._gui.track_mouse,
        SetTimer=lambda window, identifier, interval, callback: (
            surface._gui.events.append(("timer", window, identifier, interval)) or 1
        ),
    )
    surface._gdi32 = SimpleNamespace(
        GetTextMetricsW=surface._gui.get_text_metrics,
        GetTextFaceW=surface._gui.get_text_face,
    )
    surface._comctl = SimpleNamespace(
        SetWindowSubclass=surface._gui.set_subclass,
        RemoveWindowSubclass=surface._gui.remove_subclass,
        DefSubclassProc=surface._gui.default_subclass,
    )
    surface._api = SimpleNamespace(RGB=lambda r, g, b: r | (g << 8) | (b << 16))
    surface._gui.on_capture_changed = surface._procedure
    surface._native_error = OSError
    surface._dwm = SimpleNamespace(DwmFlush=lambda: 0)
    surface._composition_active = lambda: False
    surface._text_width = lambda text: len(text) * surface._font_height / 2
    surface._new_history = lambda: History(
        surface._gui, on_change=surface._history_changed, on_error=surface._history_failed
    )
    yield surface
    application.close()


def prepare_layout(surface, *, scale=1.0, work=(0, 0, 1920, 1040), monitor=None, chrome=None):
    surface._history_window, surface._status = 3, 4
    surface._history = History(
        surface._gui,
        on_change=surface._history_changed,
        on_error=surface._history_failed,
        hwnd=3,
    )
    surface._history_font_dirty = True
    surface._history_label, surface._composer_label = 9, 10
    surface._buttons = {
        identifier: identifier + 100
        for identifier in (PIN, TOP, BOTTOM, TASKBAR, CLEAR, EXPAND, STOP, LATEST)
    }
    surface._scrollbars = {HISTORY_SCROLL: 406, COMPOSER_SCROLL: 407}
    surface._scale = surface._dpi_scale = scale
    surface._user32.GetDpiForWindow = lambda window: round(scale * 96)
    surface._work_area = lambda: work
    surface._monitor_area = lambda: monitor or work
    surface._gui.chrome = chrome or (round(16 * scale), round(39 * scale))
    surface._gui.rect = (80, 80, 80 + round(1120 * scale), 80 + round(184 * scale))


def dispatch(surface, command, *, argument="", generation=None):
    request = _Request(command, argument, generation)
    surface._requests.put(request)
    surface._drain_requests()
    assert request.done.is_set()
    return request


def test_every_guidance_child_is_protected_from_input_targeting(surface):
    surface._history_window, surface._status = 3, 4
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
    surface._history_window, surface._status, surface._font = 3, 4, 50
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
    assert descriptions[0].lfFaceName == "Segoe UI Variable Text"
    assert descriptions[0].lfHeight == -21
    assert descriptions[0].lfWeight == 400
    assert descriptions[0].lfQuality == 5
    assert [event for event in events if event[0] in {"font", "delete"}] == [
        ("font", 4, 0x30, 60, False),
        ("font", 7, 0x30, 60, False),
        ("font", 8, 0x30, 60, False),
        ("font", 5, 0x30, 60, False),
        ("delete", 50),
    ]
    assert surface._font == 60


def test_unavailable_variable_font_falls_back_to_an_actual_native_font_face(surface):
    created = []
    original = surface._gui.CreateFontIndirect

    def create(description):
        created.append(description.lfFaceName)
        return original(description)

    def actual_face(dc, count, buffer):
        buffer.value = "Arial" if "Variable" in surface._gui.font_face else "Segoe UI"
        return len(buffer.value)

    surface._gui.CreateFontIndirect = create
    surface._gdi32.GetTextFaceW = actual_face
    surface._set_font()
    assert created == ["Segoe UI Variable Text", "Segoe UI"]
    assert surface._font_face == "Segoe UI" and surface._font_face_verified
    assert ("delete", 60) in surface._gui.events


@pytest.mark.parametrize("enabled", [False, True])
def test_client_animation_preference_is_read_without_mutating_windows_settings(surface, enabled):
    surface._gui.client_animations = enabled
    assert surface._client_animations_enabled() is enabled
    surface._user32.SystemParametersInfoW = lambda *args: False
    assert surface._client_animations_enabled() is False


def test_active_scene_ticks_do_not_refresh_the_whole_ui_at_sixty_hz(surface, monkeypatch):
    clock = [10.0]
    monkeypatch.setattr("desktop_mcp.teaching_ui.time.monotonic", lambda: clock[0])
    calls = []
    surface.session.snapshot = lambda: (
        calls.append("snapshot") or TeachingSnapshot(1, (), (), None, None)
    )
    surface._update_history = lambda entries, **kwargs: calls.append("history")
    surface._refresh_status = lambda *args: calls.append("status")
    surface._sync_scrollbars = lambda: calls.append("scrollbars")

    def animate(control, snapshot, now, **kwargs):
        calls.append(("scene", now))
        surface._scene_animating = True

    surface._refresh_scene = animate
    surface._timer_running = True
    surface._timer_interval = _IDLE_TIMER_MS
    surface._on_timer()
    clock[0] += 0.016
    surface._on_timer()
    assert calls.count("snapshot") == 2
    assert calls.count("history") == calls.count("status") == 1
    assert len([call for call in calls if isinstance(call, tuple)]) == 2
    assert [event for event in surface._gui.events if event[0] == "timer"] == [
        ("timer", 1, 1, _ANIMATION_TIMER_MS)
    ]
    clock[0] += 0.018
    surface._on_timer()
    assert calls.count("snapshot") == 3
    assert calls.count("history") == 2


@pytest.mark.parametrize("capture_hidden,chat_visible", [(True, True), (False, False)])
def test_hidden_surfaces_do_not_keep_the_fast_animation_timer(
    surface, capture_hidden, chat_visible
):
    surface._timer_running = True
    surface._timer_interval = _ANIMATION_TIMER_MS
    surface._hide_count = int(capture_hidden)
    surface._shown = chat_visible
    surface._scene_animating = False
    surface._chat_animating = True
    surface._schedule_timer()
    assert surface._timer_interval == _IDLE_TIMER_MS
    assert surface._gui.events[-1] == ("timer", 1, 1, _IDLE_TIMER_MS)


def test_hidden_chat_does_not_throttle_a_separately_visible_guidance_animation(surface):
    surface._timer_running = True
    surface._timer_interval = _IDLE_TIMER_MS
    surface._shown = False
    surface._scene_animating = True
    surface._schedule_timer()
    assert surface._timer_interval == _ANIMATION_TIMER_MS


@pytest.mark.parametrize("animated", [True, False])
def test_active_scene_uses_every_admitted_wake_without_a_time_bucket(
    surface, monkeypatch, animated
):
    rendered = []
    closed = []

    def render(snapshot, bounds, *, now):
        rendered.append(now)
        return SimpleNamespace(close=lambda: closed.append(now))

    monkeypatch.setattr("desktop_mcp.teaching_render.render_marks", render)
    monkeypatch.setattr("desktop_mcp.layers.upload_rgba", lambda *args: None)
    surface._scene_bounds = lambda *args, **kwargs: (0, 0, 12, 12)
    surface._api.GetSystemMetrics = lambda index: {76: 0, 77: 0, 78: 1920, 79: 1080}[index]
    mark = Mark(
        "fixture", "laser" if animated else "path", ((5, 5),), "#ffb454", 3, 1.0, None, None
    )
    snapshot = TeachingSnapshot(1, (), (mark,), None, None)
    surface.session.snapshot = lambda: snapshot
    control = surface.controller.snapshot()
    surface._refresh_scene(control, snapshot, 1.020)
    surface._refresh_scene(control, snapshot, 1.021)
    assert len(rendered) == (2 if animated else 1)
    assert closed == rendered
    assert surface._scene_animating is animated


def test_stop_between_animation_ticks_revokes_cached_scene_before_rendering(surface, monkeypatch):
    clock = [10.0]
    monkeypatch.setattr("desktop_mcp.teaching_ui.time.monotonic", lambda: clock[0])
    surface.session.snapshot = lambda: TeachingSnapshot(1, (), (), None, None)
    surface._update_history = lambda entries, **kwargs: None
    surface._refresh_status = lambda *args: None
    surface._sync_scrollbars = lambda: None
    surface._api.GetSystemMetrics = lambda index: {76: 0, 77: 0, 78: 1920, 79: 1080}[index]
    surface._refresh(now=10.0)
    surface._scene_animating = True
    surface.controller.stop()
    clock[0] = 10.016
    surface._on_timer()
    assert not surface._scene_animating
    assert not surface._gui.IsWindowVisible(2)


@pytest.mark.parametrize("work", [(0, 0, 2560, 1528), (0, 0, 640, 360)])
def test_docking_accounts_for_scaled_minimum_size_before_positioning(surface, work):
    prepare_layout(surface, scale=1.5, work=work)
    surface._gui.rect = (80, 600, 760, 890)
    surface._dock("bottom")
    event = next(event for event in surface._gui.events if event[0] == "position")
    _, _, _, x, y, width, height, _ = event
    minimum = surface._minimum_size(work)
    assert width >= minimum[0] and height >= minimum[1]
    assert work[0] + 12 <= x and x + width <= work[2] - 12
    assert work[1] + 12 <= y and y + height == work[3] - 12


@pytest.mark.parametrize("scale", [1.0, 1.5, 2.0, 3.0])
def test_compact_status_keeps_progress_chat_state_and_hotkey_on_one_readable_line(surface, scale):
    prepare_layout(surface, scale=scale, work=(0, 0, round(1920 * scale), round(1040 * scale)))
    surface._resize_mode(initial=True)
    status_box = surface._gui.positions[4]
    snapshot = TeachingSnapshot(1, (), (), WaitTarget((10, 10), 28, True, 1.0, 1.0), None)
    surface.session.snapshot = lambda: snapshot
    surface._hide_count = 1
    try:
        font = ImageFont.truetype("segoeui.ttf", surface._font_height)
    except OSError:
        pytest.skip("Segoe UI font metrics unavailable on this test host")
    surface._text_width = font.getlength
    surface._refresh()
    text = surface._gui.texts[4]
    assert len(text.splitlines()) == 1
    assert "Ctrl+Shift+H" in text and "100%" in text and "no listener" in text.lower()
    assert font.getlength(text) <= status_box[2]
    assert status_box[3] >= surface._font_height


@pytest.mark.parametrize(
    "scale,work,chrome",
    [
        (2.0, (0, 0, 640, 360), (26, 71)),
        (3.0, (0, 0, 800, 600), (36, 103)),
        (3.0, (0, 0, 640, 360), (36, 103)),
    ],
)
def test_clamped_work_area_keeps_readable_editor_status_and_stop(surface, scale, work, chrome):
    prepare_layout(surface, scale=scale, work=work, chrome=chrome)
    surface._resize_mode(initial=True)
    client = surface._gui.GetClientRect(1)[2:]
    positions = surface._gui.positions
    font_height = surface._font_height
    assert positions[3][3] >= font_height
    assert positions[surface._buttons[STOP]][3] >= font_height
    assert positions[7][3] >= font_height and positions[8][3] >= font_height
    assert font_height >= 12
    for x, y, width, height in positions.values():
        assert 0 <= x < x + width <= client[0]
        assert 0 <= y < y + height <= client[1]

    snapshot = TeachingSnapshot(1, (), (), WaitTarget((10, 10), 28, True, 1.0, 1.0), None)
    surface.session.snapshot = lambda: snapshot
    surface._hide_count = 1
    try:
        font = ImageFont.truetype("segoeui.ttf", font_height)
    except OSError:
        pytest.skip("Segoe UI font metrics unavailable on this test host")
    surface._text_width = font.getlength
    surface._refresh()
    lines = surface._gui.texts[4].splitlines()
    assert len(lines) == 1
    assert "Ctrl+Shift+H" in lines[0] and "100%" in lines[0]
    assert all(font.getlength(line) <= positions[4][2] for line in lines)


def test_native_child_creation_preserves_ids_wrapping_and_content_free_roles(surface):
    surface._create_controls(42)
    children = surface._children()
    assert set(children) == {
        PIN,
        TOP,
        BOTTOM,
        CLEAR,
        STOP,
        SEND,
        EXPAND,
        TASKBAR,
        LATEST,
        HISTORY,
        STATUS,
        COMPOSER,
        HISTORY_LABEL,
        COMPOSER_LABEL,
        HISTORY_SCROLL,
        COMPOSER_SCROLL,
    }
    for identifier, handle in children.items():
        arguments = surface._gui.created[handle]
        assert arguments[8:11] == (1, identifier, 42)
        assert arguments[3] & win32con.WS_CHILD
    for identifier in (COMPOSER,):
        style = surface._gui.created[children[identifier]][3]
        assert style & win32con.ES_MULTILINE
        assert not style & win32con.WS_VSCROLL
        assert not style & (win32con.WS_HSCROLL | win32con.ES_AUTOHSCROLL)
    assert surface._gui.created[children[COMPOSER]][3] & win32con.ES_WANTRETURN
    assert surface._gui.created[children[HISTORY]][1] == "FixtureChatHistory"
    assert children[HISTORY] not in surface._gui.subclasses
    assert (
        "message",
        children[COMPOSER],
        win32con.EM_SETLIMITTEXT,
        16000,
        0,
    ) in surface._gui.events
    surface._gui.GetWindowText = lambda handle: pytest.fail("Roles must not inspect window text")
    roles = surface.window_roles()
    assert roles[1] == "transcript"
    assert roles[2] == "annotation-overlay"
    assert roles[children[HISTORY]] == "transcript-history"
    assert roles[children[COMPOSER]] == "transcript-composer"
    assert roles[children[SEND]] == "transcript-send"
    assert roles[children[HISTORY_SCROLL]] == "transcript-history-scrollbar"
    assert roles[children[COMPOSER_SCROLL]] == "transcript-composer-scrollbar"
    assert set(surface.window_handles()) == {1, 2, *children.values()}
    assert all(
        roles[handle] == "transcript-controls"
        for identifier, handle in children.items()
        if identifier not in {HISTORY, COMPOSER, SEND, HISTORY_SCROLL, COMPOSER_SCROLL}
    )
    surface._set_font()
    font_targets = {
        event[1]
        for event in surface._gui.events
        if event[0] == "message" and event[2] == win32con.WM_SETFONT
    }
    assert font_targets == set(children.values()) - {children[HISTORY]}
    surface._style_history()
    assert surface._history.calls[-1][0] == "font"


@pytest.mark.parametrize("scale", [1.0, 1.5, 2.0, 3.0])
def test_default_native_placement_uses_outer_logical_size_without_activation(surface, scale):
    prepare_layout(surface, scale=scale, work=(0, 0, round(1920 * scale), round(1040 * scale)))
    surface._set_font()
    surface._resize_mode(initial=True)
    left, top, right, bottom = surface._gui.rect
    assert (right - left, bottom - top) == (round(1120 * scale), round(184 * scale))
    assert bottom == round(1040 * scale) - round(8 * scale)
    assert left == round(400 * scale)
    client = surface._gui.GetClientRect(1)[2:]
    planned = layout_client(*client, scale, compact=True)
    for identifier, handle in surface._children().items():
        if identifier in planned.controls:
            x, y, width, height = surface._gui.positions[handle]
            assert planned.controls[identifier] == (x, y, x + width, y + height)
    status = surface.layout_status()
    assert status == {
        "compact": True,
        "dock": "bottom",
        "bounds": (left, top, right, bottom),
        "dpi": round(96 * scale),
        "font_height": round(14 * scale),
        "font_face": "Segoe UI Variable Text",
        "text_size": "Medium",
        "font_dip": 14,
        "split": True,
        "scrollbar_width": round(8 * scale),
    }
    status["bounds"] = ("do not retain caller mutations",)
    assert surface.layout_status()["bounds"] == surface._gui.rect
    assert not any(event[0] in {"focus", "show"} for event in surface._gui.events)
    assert all(
        event[-1] & win32con.SWP_NOACTIVATE and event[-1] & win32con.SWP_NOZORDER
        for event in surface._gui.events
        if event[0] == "position"
    )


@pytest.mark.parametrize("scale", [1.5, 2.0, 3.0])
def test_native_minimum_query_during_placement_uses_the_proposed_width(surface, scale):
    work = (0, 0, round(1920 * scale), round(1040 * scale))
    prepare_layout(surface, scale=scale, work=work, chrome=(round(16 * scale), round(39 * scale)))
    original = surface._gui.SetWindowPos

    def constrain_before_resize(handle, target, x, y, width, height, flags):
        minimum = _MinMaxInfo()
        surface._procedure(handle, win32con.WM_GETMINMAXINFO, 0, ctypes.addressof(minimum))
        original(
            handle,
            target,
            x,
            y,
            max(width, minimum.min_track.x),
            max(height, minimum.min_track.y),
            flags,
        )

    surface._gui.SetWindowPos = constrain_before_resize
    surface._set_font()
    surface._resize_mode(initial=True)
    left, top, right, bottom = surface._gui.rect
    assert (right - left, bottom - top) == (round(1120 * scale), round(184 * scale))
    assert bottom == work[3] - round(8 * scale)
    assert surface._placement_width is None
    assert surface._error is None


def test_rejected_placement_cannot_leave_a_minimum_width_override(surface):
    prepare_layout(surface)

    def fail(*args):
        raise OSError("Fixture placement rejected")

    surface._gui.SetWindowPos = fail
    with pytest.raises(OSError, match="placement rejected"):
        surface._place_panel((0, 0, 1200, 200))
    assert surface._placement_width is None


def test_frame_metrics_use_actual_dpi_adjustment_not_fixed_chrome(surface):
    prepare_layout(surface, scale=3, work=(-800, -600, 0, 0))
    calls = []

    def adjust(pointer, style, menu, extended, dpi):
        calls.append((style, menu, extended, dpi))
        rectangle = ctypes.cast(pointer, ctypes.POINTER(ctypes.wintypes.RECT)).contents
        rectangle.left, rectangle.top, rectangle.right, rectangle.bottom = -18, -85, 18, 18
        return True

    surface._user32.AdjustWindowRectExForDpi = adjust
    assert surface._chrome_size() == (36, 103)
    minimum = surface._minimum_size(surface._work_area())
    assert minimum[0] <= 800 - 48 and minimum[1] <= 600 - 48
    assert calls[-1] == (
        win32con.WS_OVERLAPPEDWINDOW,
        False,
        win32con.WS_EX_APPWINDOW | win32con.WS_EX_CONTROLPARENT,
        288,
    )


def test_transcript_minimum_tracking_never_applies_to_the_annotation_canvas(surface):
    prepare_layout(surface, scale=3, work=(0, 0, 640, 360))
    panel, canvas = _MinMaxInfo(), _MinMaxInfo()
    surface._procedure(1, win32con.WM_GETMINMAXINFO, 0, ctypes.addressof(panel))
    assert (panel.min_track.x, panel.min_track.y) == surface._minimum_size(surface._work_area())
    assert panel.min_track.x <= 592 and panel.min_track.y <= 312
    assert surface._procedure(2, win32con.WM_GETMINMAXINFO, 0, ctypes.addressof(canvas)) == 47
    assert (canvas.min_track.x, canvas.min_track.y) == (0, 0)


@pytest.mark.parametrize("edge", ["top", "bottom", "taskbar-edge"])
def test_local_dock_choices_keep_size_pin_state_and_all_child_bounds(surface, edge):
    work, monitor = (-1920, -100, 0, 940), (-1920, -100, 0, 980)
    prepare_layout(surface, work=work, monitor=monitor)
    surface._resize_mode(initial=True)
    original_size = (
        surface._gui.rect[2] - surface._gui.rect[0],
        surface._gui.rect[3] - surface._gui.rect[1],
    )
    surface._gui.events.clear()
    surface._button({"top": TOP, "bottom": BOTTOM, "taskbar-edge": TASKBAR}[edge])
    area = monitor if edge == "taskbar-edge" else work
    left, top, right, bottom = usable_area(area, 1, edge)
    rectangle = surface._gui.rect
    assert left <= rectangle[0] < rectangle[2] <= right
    assert (rectangle[2] - rectangle[0], rectangle[3] - rectangle[1]) == original_size
    assert rectangle[1] == top if edge == "top" else rectangle[3] == bottom
    assert surface.layout_status()["dock"] == edge
    assert not surface._pinned
    assert all(
        event[2] != win32con.HWND_TOPMOST and event[-1] & win32con.SWP_NOACTIVATE
        for event in surface._gui.events
        if event[0] == "position"
    )
    client = surface._gui.GetClientRect(1)[2:]
    for x, y, width, height in surface._gui.positions.values():
        assert 0 <= x < x + width <= client[0]
        assert 0 <= y < y + height <= client[1]


def test_dragging_can_float_and_never_implicitly_selects_taskbar_edge(surface):
    prepare_layout(surface, monitor=(0, 0, 1920, 1080))
    surface._gui.rect = (200, 300, 1320, 464)
    surface._procedure(1, 0x0232, 0, 0)
    assert surface.layout_status()["dock"] == "floating"
    assert surface._gui.rect == (200, 300, 1320, 464)
    surface._gui.rect = (200, 916, 1320, 1080)
    surface._procedure(1, 0x0232, 0, 0)
    assert surface.layout_status()["dock"] != "taskbar-edge"
    assert surface._gui.rect[3] <= 1032
    surface._button(TASKBAR)
    surface._procedure(1, 0x0232, 0, 0)
    assert surface.layout_status()["dock"] == "taskbar-edge"
    assert surface._gui.rect[3] == 1080


def test_same_dpi_monitor_transition_reflows_only_after_move_and_preserves_draft(surface):
    prepare_layout(surface, scale=1.5, work=(0, 0, 2880, 1560))
    surface._set_font()
    surface._resize_mode(initial=True)
    surface._gui.SetWindowText(7, "Unsent draft 😀\r\nAnother line")
    surface._gui.selections[7] = (7, 14)
    surface._gui.events.clear()
    surface._gui.rect = (-520, 80, 1160, 326)
    surface._work_area = lambda: (-640, 0, 0, 360)
    surface._procedure(1, win32con.WM_MOVE, 0, 0)
    assert not surface._gui.events
    surface._procedure(1, 0x0232, 0, 0)
    assert not surface._exit
    assert surface._dpi_scale == 1.5
    assert surface._font_height < 21
    assert surface._gui.rect == (-628, 12, -12, 348)
    assert surface._gui.texts[7] == "Unsent draft 😀\r\nAnother line"
    assert surface._gui.selections[7] == (7, 14)
    assert not any(event[0] == "focus" for event in surface._gui.events)
    client = surface._gui.GetClientRect(1)[2:]
    for x, y, width, height in surface._gui.positions.values():
        assert 0 <= x < x + width <= client[0]
        assert 0 <= y < y + height <= client[1]


@pytest.mark.parametrize("dpi", [96, 144, 192, 288])
def test_dpi_changed_suggestion_is_clamped_to_the_new_monitor_without_activation(surface, dpi):
    prepare_layout(surface, work=(0, 0, 1920, 1040))
    surface._set_font()
    surface._resize_mode(initial=True)
    surface._gui.texts[7] = "Draft remains during DPI changes"
    surface._gui.selections[7] = (4, 8)
    surface._work_area = lambda: (-800, -600, 0, 0)
    surface._gui.chrome = (round(16 * dpi / 96), round(39 * dpi / 96))
    proposed = ctypes.wintypes.RECT(-900, -800, 900, 0)
    surface._gui.events.clear()
    surface._procedure(1, 0x02E0, dpi | (dpi << 16), ctypes.addressof(proposed))
    assert not surface._exit
    assert surface._dpi_scale == dpi / 96
    left, top, right, bottom = usable_area(surface._work_area(), dpi / 96, "bottom")
    assert left <= surface._gui.rect[0] < surface._gui.rect[2] <= right
    assert top <= surface._gui.rect[1] < surface._gui.rect[3] <= bottom
    assert surface._gui.selections[7] == (4, 8)
    assert surface._gui.texts[7] == "Draft remains during DPI changes"
    assert all(
        event[-1] & win32con.SWP_NOACTIVATE
        for event in surface._gui.events
        if event[0] == "position"
    )
    assert not any(event[0] == "focus" for event in surface._gui.events)


def test_expand_compact_retains_each_mode_size_draft_and_pending_conversation(surface):
    prepare_layout(surface)
    surface._resize_mode(initial=True)
    compact_bounds = surface._gui.rect
    surface.session.conversation.send_user("A pending correction")
    conversation = surface.session.conversation.entries()
    surface._gui.SetWindowText(7, "Still typing 😀\r\nSecond line")
    surface._gui.selections[7] = (6, 12)
    surface._button(EXPAND)
    assert not surface._compact and not surface.layout_status()["split"]
    assert surface._gui.rect[3] - surface._gui.rect[1] == 440
    assert surface._gui.texts[surface._buttons[EXPAND]] == "Compact"
    left, _, right, bottom = surface._gui.rect
    surface._gui.rect = (left, bottom - 500, right, bottom)
    surface._layout()
    surface._button(EXPAND)
    assert surface._compact and surface._gui.rect == compact_bounds
    surface._button(EXPAND)
    assert surface._gui.rect[3] - surface._gui.rect[1] == 500
    assert surface._gui.texts[7] == "Still typing 😀\r\nSecond line"
    assert surface._gui.selections[7] == (6, 12)
    assert surface.session.conversation.entries() == conversation
    assert surface.session.conversation.status()["pending_messages"] == 1


def test_layout_changes_during_capture_never_resurrect_a_hidden_transcript(surface):
    prepare_layout(surface)
    surface._resize_mode(initial=True)
    surface._shown = True
    dispatch(surface, "hide")
    dispatch(surface, "visibility", argument="off")
    surface._button(EXPAND)
    surface._button(TASKBAR)
    assert not surface.enabled and not surface.visible
    assert surface._hide_count == 1 and not surface._gui.IsWindowVisible(1)
    dispatch(surface, "restore")
    assert not surface._gui.IsWindowVisible(1)
    assert surface.layout_status()["dock"] == "taskbar-edge"


def history_entries(count=12, *, start=1, text=None):
    return tuple(
        (index, "Fixture", text or f"Message {index} " + "wrapped words " * 12, "assistant")
        for index in range(start, start + count)
    )


@pytest.mark.parametrize("position,following", [(16, False), (17, True)])
def test_native_scroll_info_has_six_fields_with_position_at_index_four(
    surface, position, following
):
    prepare_layout(surface)
    surface._resize_mode(initial=True)
    legacy = 9000
    surface._gui.window_styles[legacy] = win32con.WS_VSCROLL
    surface._gui.positions[legacy] = (0, 0, 400, 64)
    surface._gui.texts[legacy] = "\r\n".join("line" for _ in range(21))
    surface._gui.first_lines[legacy] = position
    info = surface._gui.GetScrollInfo(legacy, win32con.SB_VERT, 7)
    assert info == (0, 0, 20, 4, position, 0)
    assert (info[4] >= info[2] - info[3] + 1) is following


@pytest.mark.parametrize("position,following", [(16, False), (17, True)])
def test_barless_edit_uses_actual_lines_and_formatting_rect_not_scrollinfo(
    surface, position, following
):
    prepare_layout(surface)
    surface._resize_mode(initial=True)
    surface._gui.positions[7] = (0, 0, 400, 68)
    surface._gui.texts[7] = "\r\n".join("line" for _ in range(21))
    surface._gui.first_lines[7] = position
    with pytest.raises(OSError) as error:
        surface._gui.GetScrollInfo(7, win32con.SB_VERT, 7)
    assert error.value.errno == 1447
    assert surface._scroll_state(7).at_end is following


@pytest.mark.parametrize("action", ["expand", "dock", "fit"])
def test_maximized_layout_queries_user32_not_a_nonexistent_pywin32_export(surface, action):
    prepare_layout(surface)
    surface._resize_mode(initial=True)
    surface._gui.zoomed = True
    calls = []

    def zoomed(handle):
        calls.append(handle)
        return surface._gui.zoomed

    surface._user32.IsZoomed = zoomed
    if action == "expand":
        surface._button(EXPAND)
        assert not surface._gui.zoomed and not surface._compact
    elif action == "dock":
        surface._dock("top")
        assert not surface._gui.zoomed
    else:
        surface._fit_current()
        assert surface._gui.zoomed
    assert calls and set(calls) == {surface._panel}
    assert not hasattr(surface._gui, "IsZoomed")


def test_history_unread_and_following_are_owned_by_the_component(surface):
    prepare_layout(surface)
    surface._resize_mode(initial=True)
    entries = history_entries()
    surface._update_history(entries)
    surface._history.following = False
    surface._history.unread = True
    surface._gui.events.clear()
    surface._update_history((*entries, *history_entries(1, start=13)))
    assert surface._history.following is False
    assert surface._history_unread
    assert surface._gui.texts[surface._buttons[LATEST]] == "Latest *"
    assert not any(
        event[0] == "message" and event[2] == win32con.EM_SCROLLCARET
        for event in surface._gui.events
    )
    assert not any(event[0] in {"focus", "show", "position"} for event in surface._gui.events)
    surface._button(LATEST)
    assert not surface._history_unread
    assert surface._history.following
    assert ("latest",) in surface._history.calls


def test_history_host_never_receives_native_edit_or_redraw_suppression_messages(surface):
    prepare_layout(surface)
    surface._resize_mode(initial=True)
    entries = history_entries(30)
    surface._update_history(entries)
    surface._gui.events.clear()
    surface._update_history((*entries, *history_entries(1, start=31)))
    surface._layout()
    surface._scroll_latest()
    edit_messages = {
        win32con.EM_GETSEL,
        win32con.EM_SETSEL,
        win32con.EM_GETLINECOUNT,
        win32con.EM_GETFIRSTVISIBLELINE,
        win32con.EM_LINEFROMCHAR,
        win32con.EM_LINEINDEX,
        win32con.EM_GETRECT,
        win32con.EM_SETMARGINS,
        win32con.EM_LINESCROLL,
        win32con.WM_SETREDRAW,
        win32con.WM_SETTEXT,
        win32con.WM_SETFONT,
    }
    assert not any(
        event[0] == "message" and event[1] == 3 and event[2] in edit_messages
        for event in surface._gui.events
    )
    assert surface._history.entries[-1][0] == 31


def test_incoming_entries_do_not_manipulate_the_components_opaque_selection(surface):
    prepare_layout(surface)
    surface._resize_mode(initial=True)
    entries = history_entries()
    surface._update_history(entries)
    view = surface._history.view
    surface._history.calls.clear()
    surface._update_history((*entries, *history_entries(1, start=13)))
    assert surface._history.view is view
    assert not any(
        call[0] in {"capture-view", "restore-view", "latest"} for call in surface._history.calls
    )


def test_expand_and_compact_restore_the_component_view_without_interpreting_it(surface):
    prepare_layout(surface)
    surface._resize_mode(initial=True)
    entries = history_entries(6, text="A short message.")
    surface._update_history(entries)
    view = surface._history.view
    surface._history.calls.clear()
    surface._button(EXPAND)
    surface._button(EXPAND)
    restored = [call[1] for call in surface._history.calls if call[0] == "restore-view"]
    assert len(restored) == 2 and all(item is view for item in restored)
    assert surface._last_text == entries


def test_resizing_delegates_history_reflow_and_preserves_native_composer_selection(surface):
    prepare_layout(surface)
    surface._resize_mode(initial=True)
    surface._update_history(history_entries())
    history_view = surface._history.view
    surface._gui.SetWindowText(7, "Draft\r\nin progress")
    surface._gui.selections[7] = (7, 10)
    surface._gui.events.clear()
    surface._gui.rect = (100, 100, 760, 360)
    surface._layout()
    width, height = surface._gui.GetClientRect(3)[2:]
    assert ("reflow", width, height) in surface._history.calls
    assert ("restore-view", history_view) in surface._history.calls
    assert surface._gui.selections[7] == (7, 10)
    assert surface._gui.texts[7] == "Draft\r\nin progress"
    assert not any(event[0] == "text" and event[1] in {3, 7} for event in surface._gui.events)


def test_history_receives_complete_unflattened_unicode_entries_above_64k(surface):
    prepare_layout(surface)
    surface._resize_mode(initial=True)
    entries = history_entries(8, text="😀" * 3000 + "\n" + "Long fixture text " * 300)
    surface._update_history(entries)
    assert sum(len(entry[2].encode("utf-16-le")) // 2 for entry in entries) > 65535
    assert surface._history.entries is entries
    assert not any(event[0] == "text" and event[1] == 3 for event in surface._gui.events)


def test_pruning_and_clear_delegate_exact_entries_without_mutating_the_message_queue(surface):
    prepare_layout(surface)
    surface._resize_mode(initial=True)
    entries = history_entries()
    surface._update_history(entries)
    surface._update_history(entries[2:])
    assert surface._history.entries == entries[2:]
    assert surface.session.conversation.entries() == ()
    surface._update_history(())
    assert not surface._history_unread
    assert surface._history.entries == ()


@pytest.mark.parametrize("armed", [True, False])
@pytest.mark.parametrize(
    "chat,expected",
    [
        ({}, "no listener"),
        ({"listener_connected": True}, "not"),
        ({"listener_connected": True, "listener_waiting": True}, "listening"),
        ({"listener_connected": True, "awaiting_reply": True, "pending_messages": 2}, "reply"),
        ({"pending_messages": 16}, "16 queued"),
    ],
)
@pytest.mark.parametrize("client", [(1104, 125), (344, 190), (304, 160)])
def test_compact_status_is_honest_readable_and_does_not_hide_stop(
    surface, armed, chat, expected, client
):
    prepare_layout(surface)
    surface._gui.rect = (0, 0, client[0] + 16, client[1] + 39)
    surface._layout()
    status = dict(
        pending_messages=0,
        listener_connected=False,
        listener_waiting=False,
        awaiting_reply=False,
        listener_name=None,
    )
    status.update(chat)
    surface.session.conversation = SimpleNamespace(status=lambda: status)
    snapshot = TeachingSnapshot(1, (), (), WaitTarget((10, 10), 28, True, 1.0, 1.0), None)
    try:
        font = ImageFont.truetype("segoeui.ttf", surface._font_height)
    except OSError:
        pytest.skip("Segoe UI font metrics unavailable on this test host")
    surface._text_width = font.getlength
    surface._refresh_status(SimpleNamespace(armed=armed), snapshot)
    text = surface._gui.texts[4]
    assert ("ready" if armed else "paused") in text.lower()
    assert expected in text.lower()
    assert "Ctrl+Shift+H" in text and "100%" in text
    assert "…" not in text
    assert font.getlength(text) <= surface._status_width
    assert surface._gui.positions[surface._buttons[STOP]][3] >= surface._font_height


def test_send_failure_has_concise_status_and_preserves_the_entire_draft(surface):
    prepare_layout(surface, work=(0, 0, 380, 800))
    surface._resize_mode(initial=True)
    surface._gui.SetWindowText(7, " ")
    surface.session.snapshot = lambda: TeachingSnapshot(1, (), (), None, None)
    surface.controller.stop()
    surface._send_user()
    assert surface._gui.texts[7] == " "
    assert "not sent" in surface._gui.texts[4].lower()
    assert "kept" in surface._gui.texts[4]
    assert "Ctrl+Shift+H" in surface._gui.texts[4]
    assert not surface.controller.snapshot().armed


@pytest.mark.parametrize("foreground,expected", [(1, [("focus", 7)]), (999, [])])
def test_send_button_returns_focus_only_when_the_user_is_in_the_transcript(
    surface, foreground, expected
):
    surface._gui.foreground = foreground
    surface._send_user = lambda: None
    surface._button(SEND)
    assert surface._gui.events == expected


@pytest.mark.parametrize("fail_measurement", [False, True])
def test_status_measurement_uses_the_control_font_and_always_releases_its_dc(
    surface, fail_measurement
):
    events = []
    surface._font = 60
    surface._gui.GetDC = lambda window: 500
    surface._gui.SelectObject = lambda dc, font: events.append(("select", dc, font)) or 40
    surface._gui.ReleaseDC = lambda window, dc: events.append(("release", window, dc))

    def measure(dc, text):
        assert dc == 500 and text == "Status"
        if fail_measurement:
            raise OSError("Fixture text measurement failure")
        return 48, 17

    surface._gui.GetTextExtentPoint32 = measure
    if fail_measurement:
        with pytest.raises(OSError, match="measurement"):
            TeachingSurface._text_width(surface, "Status")
    else:
        assert TeachingSurface._text_width(surface, "Status") == 48
    assert events == [("select", 500, 60), ("select", 500, 40), ("release", 1, 500)]


def test_layout_does_not_reset_composer_selection_or_scroll_during_ime_composition(surface):
    prepare_layout(surface)
    surface._resize_mode(initial=True)
    surface._composition_active = lambda: True
    surface._gui.SetWindowText(7, "Uncommitted composition remains native")
    surface._gui.selections[7] = (12, 12)
    surface._gui.events.clear()
    surface._gui.rect = (100, 100, 760, 360)
    surface._layout()
    assert surface._gui.texts[7] == "Uncommitted composition remains native"
    assert surface._gui.selections[7] == (12, 12)
    assert not any(
        event[0] == "message"
        and event[1] == 7
        and event[2] in {win32con.EM_SETSEL, win32con.EM_LINESCROLL, win32con.EM_SCROLLCARET}
        for event in surface._gui.events
    )
    assert surface.session.conversation.entries() == ()


def test_stop_clear_and_pin_remain_local_independent_actions(surface):
    prepare_layout(surface)
    cleared = []
    surface.session.clear_local = lambda: cleared.append(True)
    surface.session.conversation.send_user("Keep this pending message")
    surface._button(PIN)
    assert surface._pinned
    assert surface._gui.events[-1][2] == win32con.HWND_TOPMOST
    surface._button(PIN)
    assert not surface._pinned
    assert surface._gui.events[-1][2] == win32con.HWND_NOTOPMOST
    surface._button(CLEAR)
    assert cleared == [True]
    assert surface.session.conversation.status()["pending_messages"] == 1
    surface._button(STOP)
    assert not surface.controller.snapshot().armed
    assert surface.session.conversation.status()["pending_messages"] == 1


def test_rounded_buttons_clear_their_corners_to_the_panel_background(surface):
    events = surface._gui.events
    surface._background = 50
    surface._api = SimpleNamespace(RGB=lambda *values: 0)
    surface._con = SimpleNamespace(
        PS_SOLID=0, TRANSPARENT=1, DT_CENTER=1, DT_VCENTER=4, DT_SINGLELINE=32, DT_NOPREFIX=0x800
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
    texts = surface._gui.texts
    texts[7] = "I see it, but what does it do?"
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


@pytest.mark.parametrize("draft", ["x" * 4000, "Hello \U0001f369 " * 900])
def test_send_reads_the_entire_native_buffer_not_a_truncated_window_caption(surface, draft):
    surface._gui.SetWindowText(7, draft)
    surface._gui.GetWindowText = lambda handle: draft[:511]
    surface._refresh = lambda: None
    received = []
    send = surface.session.conversation.send_user

    def record(text):
        received.append(text)
        return send(text)

    surface.session.conversation.send_user = record
    surface._send_user()
    assert received == [draft]
    assert surface._gui.texts[7] == ""
    assert surface.session.conversation.status()["pending_messages"] == 1


def test_an_oversized_native_draft_is_retained_instead_of_truncated_and_sent(surface):
    draft = "x" * 16001
    surface._gui.SetWindowText(7, draft)
    surface._refresh = lambda: None
    surface._send_user()
    assert surface._gui.texts[7] == draft
    assert surface.session.conversation.status()["pending_messages"] == 0
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


def prepare_scrolling(surface, *, scale=1.0):
    prepare_layout(surface, scale=scale, work=(0, 0, round(1920 * scale), round(1040 * scale)))
    for editor in (7,):
        surface._comctl.SetWindowSubclass(editor, surface._edit_callback, 1, 0)
    surface._set_font()
    surface._resize_mode(initial=True)
    surface._update_history(history_entries(20))
    surface._gui.SetWindowText(7, "\r\n".join(f"Draft line {index}" for index in range(80)))
    surface._scroll_to(3, 0)
    surface._scroll_to(7, 0)
    surface._sync_scrollbars()


def mouse_point(x, y):
    return (x & 0xFFFF) | ((y & 0xFFFF) << 16)


def grab_thumb(surface, identifier=HISTORY_SCROLL):
    scrollbar = surface._scrollbars[identifier]
    editor = surface._scroll_target(scrollbar)
    state = surface._scroll_state(editor)
    _, _, width, height = surface._gui.GetClientRect(scrollbar)
    thumb = thumb_geometry(state, height, surface._scale)
    point = mouse_point(width // 2, thumb.top + thumb.length // 2)
    surface._procedure(scrollbar, win32con.WM_LBUTTONDOWN, win32con.MK_LBUTTON, point)
    assert surface._scroll_drag is not None
    assert surface._gui.capture == scrollbar
    return scrollbar


def test_layout_batches_geometry_and_restores_redraw_before_one_complete_erase(surface):
    prepare_scrolling(surface)
    surface._gui.events.clear()
    surface._gui.rect = (100, 100, 850, 400)
    surface._dpi_scale = 1.5
    surface._gui.on_redraw = lambda window, flags: surface._procedure(
        window, win32con.WM_ERASEBKGND, 900, 0
    )
    surface._layout()
    events = surface._gui.events
    redraws = [event for event in events if event[0] == "redraw"]
    assert len(redraws) == 1 and redraws[0][1] == 1
    required = win32con.RDW_INVALIDATE | win32con.RDW_ERASE | win32con.RDW_ALLCHILDREN
    required |= win32con.RDW_FRAME | win32con.RDW_UPDATENOW
    assert redraws[0][2] & required == required
    assert ("fill", 900, surface._gui.GetClientRect(1), surface._background) in events
    for event in events:
        if event[0] == "move":
            assert event[-1] is False
        elif event[0] == "defer":
            flags = event[-1]
            assert flags & win32con.SWP_NOREDRAW and flags & win32con.SWP_NOCOPYBITS
            assert flags & win32con.SWP_NOACTIVATE and flags & win32con.SWP_NOZORDER
        elif event[0] == "message" and event[2] == win32con.WM_SETFONT:
            assert event[-1] is False
    end = next(index for index, event in enumerate(events) if event[0] == "end-defer")
    redraw = next(index for index, event in enumerate(events) if event[0] == "redraw")
    for editor in (7,):
        disable = events.index(("message", editor, win32con.WM_SETREDRAW, False, 0))
        enable = events.index(("message", editor, win32con.WM_SETREDRAW, True, 0))
        assert disable < end < enable < redraw
        assert surface._gui.IsWindowVisible(editor)
    assert not any(
        event[0] == "message" and event[1] == 1 and event[2] == win32con.WM_SETREDRAW
        for event in events
    )


def test_dpi_reflow_paints_only_the_final_clamped_placement(surface):
    prepare_scrolling(surface)
    surface._gui.events.clear()
    surface._gui.chrome = (24, 58)
    surface._work_area = lambda: (0, 0, 1200, 700)
    suggested = ctypes.wintypes.RECT(-100, 600, 1900, 920)
    surface._procedure(1, 0x02E0, 144 | (144 << 16), ctypes.addressof(suggested))
    assert not surface._exit
    assert surface._layout_hold == 0 and surface._placement_width is None
    assert len([e for e in surface._gui.events if e[0] == "redraw" and e[1] == 1]) == 1
    assert all(
        event[-1] & win32con.SWP_NOREDRAW and event[-1] & win32con.SWP_NOCOPYBITS
        for event in surface._gui.events
        if event[0] == "position"
    )
    assert surface.layout_status()["scrollbar_width"] == round(8 * surface._scale)


def test_synchronous_wm_size_does_not_cause_a_second_complete_repaint(surface):
    prepare_scrolling(surface)
    original = surface._gui.SetWindowPos

    def resize(*arguments):
        original(*arguments)
        surface._procedure(1, win32con.WM_SIZE, win32con.SIZE_RESTORED, 0)

    surface._gui.SetWindowPos = resize
    surface._gui.events.clear()
    surface._place_panel((100, 100, 1000, 320))
    assert len([e for e in surface._gui.events if e[0] == "redraw" and e[1] == 1]) == 1


def test_nested_resize_is_deferred_not_recursive_and_does_not_freeze_redraw(surface):
    prepare_scrolling(surface)
    surface._gui.events.clear()
    surface._gui.on_end_defer = lambda: (surface._layout(), surface._layout())
    surface._layout()
    assert not surface._layout_busy and not surface._programmatic_depth
    assert surface._layout_posted
    assert [e for e in surface._gui.events if e[0] == "post"] == [("post", 1, _REFLOW, 0, 0)]
    assert len([e for e in surface._gui.events if e[0] == "redraw"]) == 1
    surface._gui.on_end_defer = None
    surface._procedure(1, _REFLOW, 0, 0)
    assert not surface._layout_posted and not surface._layout_busy


def test_failed_child_batch_restores_every_edit_and_erases_partial_layout(surface):
    prepare_scrolling(surface)
    surface._gui.events.clear()
    surface._user32.DeferWindowPos = lambda *args: 0
    with pytest.raises(OSError):
        surface._layout()
    assert not surface._layout_busy and not surface._programmatic_depth
    assert surface._gui.IsWindowVisible(3) and surface._gui.IsWindowVisible(7)
    assert any(event[0] == "redraw" for event in surface._gui.events)
    assert not any(event[0] == "end-defer" for event in surface._gui.events)


def test_capture_visibility_intent_survives_a_reentrant_layout_transaction(surface):
    prepare_scrolling(surface)
    surface._shown = True

    def hide_during_layout():
        assert surface._layout_busy and surface._programmatic_depth
        assert dispatch(surface, "hide").error is None
        assert dispatch(surface, "visibility", argument="off").error is None

    surface._gui.on_end_defer = hide_during_layout
    surface._layout()
    assert not surface.visible and not surface.enabled
    assert not surface._gui.IsWindowVisible(1)
    assert not surface._gui.IsWindowVisible(3)
    assert dispatch(surface, "restore").error is None
    assert not surface._gui.IsWindowVisible(1)


@pytest.mark.parametrize("identifier", [HISTORY_SCROLL, COMPOSER_SCROLL])
@pytest.mark.parametrize("scale", [1, 1.5, 2, 3])
def test_dark_thumb_drag_pages_and_reaches_both_ends_without_changing_selection(
    surface, identifier, scale
):
    prepare_scrolling(surface, scale=scale)
    surface.controller.stop()
    generation = surface.controller.snapshot().generation
    scrollbar = surface._scrollbars[identifier]
    editor = surface._scroll_target(scrollbar)
    surface._gui.selections[editor] = (10, 20)
    draft = surface._gui.texts[7]
    maximum = surface._scroll_state(editor).maximum
    scrollbar = grab_thumb(surface, identifier)
    _, _, width, height = surface._gui.GetClientRect(scrollbar)
    surface._procedure(
        scrollbar, win32con.WM_MOUSEMOVE, win32con.MK_LBUTTON, mouse_point(width // 2, height + 100)
    )
    assert surface._scroll_state(editor).position == maximum
    surface._procedure(scrollbar, win32con.WM_LBUTTONUP, 0, mouse_point(width // 2, height + 100))
    assert surface._scroll_drag is None and not surface._gui.capture
    assert surface._gui.selections[editor] == (10, 20)
    surface._procedure(scrollbar, win32con.WM_LBUTTONDOWN, 1, mouse_point(width // 2, 0))
    assert (
        surface._scroll_state(editor).position == maximum - surface._scroll_state(editor).page_step
    )
    surface._procedure(scrollbar, win32con.WM_KEYDOWN, win32con.VK_HOME, 0)
    if identifier == HISTORY_SCROLL:
        assert ("scroll-command", win32con.SB_TOP) in surface._history.calls
    else:
        assert surface._scroll_state(editor).position == 0
    surface._procedure(scrollbar, win32con.WM_KEYDOWN, win32con.VK_END, 0)
    if identifier == HISTORY_SCROLL:
        assert ("scroll-command", win32con.SB_BOTTOM) in surface._history.calls
    else:
        assert surface._scroll_state(editor).position == maximum
    assert surface._gui.texts[7] == draft
    assert surface.controller.snapshot().generation == generation
    assert not surface.controller.snapshot().armed
    assert surface.controller.snapshot().completed_actions == 0


@pytest.mark.parametrize("identifier", [HISTORY_SCROLL, COMPOSER_SCROLL])
@pytest.mark.parametrize("move_away", [False, True])
def test_outer_thumb_grab_preserves_exact_origin_in_pixel_and_line_ranges(
    surface, identifier, move_away
):
    prepare_scrolling(surface)
    if identifier == HISTORY_SCROLL:
        surface._history.state = ScrollState(3001, 9, 1000)
    else:
        surface._gui.SetWindowText(7, "line\r\n" * 3000)
        surface._scroll_to(7, 1000)
    bar = surface._scrollbars[identifier]
    editor = surface._scroll_target(bar)
    _, _, width, height = surface._gui.GetClientRect(bar)
    state = surface._scroll_state(editor)
    thumb = thumb_geometry(state, height, surface._scale)
    y = thumb.top + thumb.length // 2
    grab_thumb(surface, identifier)
    if move_away:
        surface._procedure(
            bar, win32con.WM_MOUSEMOVE, win32con.MK_LBUTTON, mouse_point(width // 2, y + 8)
        )
        assert surface._scroll_state(editor).position > 1000
    surface._procedure(bar, win32con.WM_MOUSEMOVE, win32con.MK_LBUTTON, mouse_point(width // 2, y))
    surface._procedure(bar, win32con.WM_LBUTTONUP, 0, mouse_point(width // 2, y))
    assert surface._scroll_state(editor).position == 1000
    assert surface._scroll_drag is None and not surface._gui.capture


def test_scroll_adapter_keeps_full_native_line_deltas_above_65535(surface):
    prepare_scrolling(surface)
    original = surface._gui._lines
    surface._gui._lines = lambda handle: range(100000) if handle == 7 else original(handle)
    surface._scroll_to(7, 0)
    scrollbar = grab_thumb(surface, COMPOSER_SCROLL)
    surface._gui.events.clear()
    surface._procedure(scrollbar, win32con.WM_LBUTTONUP, 0, mouse_point(4, 30000))
    assert surface._scroll_state(7).position == surface._scroll_state(7).maximum
    assert any(
        event[0] == "message" and event[1:3] == (7, win32con.EM_LINESCROLL) and event[-1] > 65535
        for event in surface._gui.events
    )
    assert not surface._gui.capture


@pytest.mark.parametrize("editor", [7])
def test_edit_subclass_consumes_wheel_once_with_precision_and_windows_preferences(surface, editor):
    prepare_scrolling(surface)
    surface.controller.stop()
    surface._scroll_to(editor, 10)
    surface._gui.events.clear()
    for _ in range(3):
        surface._edit_procedure(editor, win32con.WM_MOUSEWHEEL, 30 << 16, 0)
    assert surface._scroll_state(editor).position == 10
    surface._edit_procedure(editor, win32con.WM_MOUSEWHEEL, 30 << 16, 0)
    assert surface._scroll_state(editor).position == 7
    assert not any(
        e[0] == "default-edit" and e[2] == win32con.WM_MOUSEWHEEL for e in surface._gui.events
    )
    surface._gui.wheel_lines = 0
    surface._edit_procedure(editor, win32con.WM_MOUSEWHEEL, (-120 & 0xFFFF) << 16, 0)
    assert surface._scroll_state(editor).position == 7
    surface._gui.wheel_lines = 0xFFFFFFFF
    surface._edit_procedure(editor, win32con.WM_MOUSEWHEEL, (-120 & 0xFFFF) << 16, 0)
    assert surface._scroll_state(editor).position == 7 + surface._scroll_state(editor).page_step
    assert not surface.controller.snapshot().armed


def test_wheel_over_the_custom_bar_and_refused_preference_read_use_safe_defaults(surface):
    prepare_scrolling(surface)
    surface._user32.SystemParametersInfoW = lambda *args: False
    scrollbar = surface._scrollbars[COMPOSER_SCROLL]
    surface._procedure(scrollbar, win32con.WM_MOUSEWHEEL, (-120 & 0xFFFF) << 16, 0)
    assert surface._scroll_state(7).position == 3


@pytest.mark.parametrize(
    "reason",
    [
        "up",
        "cancel",
        "capture-changed",
        "destroy",
        "hide",
        "visibility",
        "close",
        "root-cancel",
        "deactivate",
        "lost-button",
        "reflow",
        "minimize",
    ],
)
def test_local_thumb_capture_has_bounded_owned_cleanup_on_every_exit(surface, reason):
    prepare_scrolling(surface)
    surface._shown = True
    scrollbar = grab_thumb(surface)
    if reason == "up":
        surface._procedure(scrollbar, win32con.WM_LBUTTONUP, 0, mouse_point(-50, -50))
    elif reason == "cancel":
        surface._procedure(scrollbar, win32con.WM_CANCELMODE, 0, 0)
    elif reason == "capture-changed":
        surface._gui.capture = 999
        surface._procedure(scrollbar, 0x0215, 0, 999)
        assert surface._gui.capture == 999
    elif reason == "destroy":
        surface._procedure(scrollbar, win32con.WM_NCDESTROY, 0, 0)
        assert scrollbar not in surface.window_roles()
    elif reason == "hide":
        assert dispatch(surface, "hide").error is None
        assert dispatch(surface, "restore").error is None
    elif reason == "visibility":
        surface._apply_visibility(False)
    elif reason == "close":
        assert dispatch(surface, "close").error is None
    elif reason == "root-cancel":
        surface._procedure(1, win32con.WM_CANCELMODE, 0, 0)
    elif reason == "deactivate":
        surface._procedure(1, win32con.WM_ACTIVATE, 0, 0)
    elif reason == "lost-button":
        surface._procedure(scrollbar, win32con.WM_MOUSEMOVE, 0, mouse_point(4, 12))
    elif reason == "minimize":
        surface._procedure(1, win32con.WM_SIZE, win32con.SIZE_MINIMIZED, 0)
    else:
        surface._layout()
    assert surface._scroll_drag is None
    assert surface._gui.capture == (999 if reason == "capture-changed" else 0)


def test_bar_does_not_steal_another_window_capture_or_force_foreground(surface):
    prepare_scrolling(surface)
    surface._gui.capture = 999
    surface._gui.events.clear()
    scrollbar = surface._scrollbars[HISTORY_SCROLL]
    state = surface._scroll_state(3)
    thumb = thumb_geometry(state, surface._gui.GetClientRect(scrollbar)[3], surface._scale)
    surface._procedure(scrollbar, win32con.WM_LBUTTONDOWN, 1, mouse_point(4, thumb.top + 1))
    assert surface._gui.capture == 999 and surface._scroll_drag is None
    assert not any(event[0] in {"capture", "focus"} for event in surface._gui.events)


def test_refused_mouse_capture_leaves_no_latent_drag(surface):
    prepare_scrolling(surface)
    surface._gui.SetCapture = lambda window: 0
    scrollbar = surface._scrollbars[HISTORY_SCROLL]
    thumb = thumb_geometry(surface._scroll_state(3), surface._gui.GetClientRect(scrollbar)[3], 1)
    surface._procedure(scrollbar, win32con.WM_LBUTTONDOWN, 1, mouse_point(4, thumb.top + 1))
    assert surface._scroll_drag is None
    assert not surface._exit


def test_empty_editor_keeps_its_dark_scroll_affordance_without_a_spurious_thumb(surface):
    prepare_scrolling(surface)
    scrollbar = surface._scrollbars[COMPOSER_SCROLL]
    surface._gui.SetWindowText(7, "")
    state = surface._scroll_states[scrollbar]
    assert isinstance(state, ScrollState) and state.maximum == 0
    assert surface._gui.IsWindowVisible(scrollbar)
    surface._gui.events.clear()
    surface._procedure(scrollbar, win32con.WM_PAINT, 0, 0)
    assert len([event for event in surface._gui.events if event[0] == "round"]) == 1
    surface._procedure(scrollbar, win32con.WM_LBUTTONDOWN, 1, mouse_point(4, 12))
    assert surface._scroll_drag is None and not surface._gui.capture
    assert surface.window_roles()[scrollbar] == "transcript-composer-scrollbar"


def test_paint_erases_old_thumb_and_uses_only_slim_dark_track_and_thumb_colors(surface):
    prepare_scrolling(surface)
    scrollbar = surface._scrollbars[HISTORY_SCROLL]
    surface._gui.events.clear()
    surface._procedure(scrollbar, win32con.WM_PAINT, 0, 0)
    events = surface._gui.events
    assert events[0] == ("begin-paint", scrollbar)
    assert events[1] == ("fill", 900, surface._gui.GetClientRect(scrollbar), surface._background)
    assert events[-1] == ("end-paint", scrollbar)
    assert len([event for event in events if event[0] == "round"]) == 2
    for event in events:
        if event[0] == "brush":
            rgb = event[1]
            assert max(rgb & 255, (rgb >> 8) & 255, (rgb >> 16) & 255) < 180
    width = surface._gui.GetClientRect(scrollbar)[2]
    assert width == 8
    assert surface.window_roles()[scrollbar] == "transcript-history-scrollbar"
    surface._gui.events.clear()
    surface._procedure(scrollbar, win32con.WM_MOUSEMOVE, 0, mouse_point(4, 4))
    assert ("track-mouse", scrollbar, 2) in surface._gui.events
    other = surface._scrollbars[COMPOSER_SCROLL]
    surface._gui.events.clear()
    surface._procedure(other, win32con.WM_MOUSEMOVE, 0, mouse_point(4, 4))
    assert ("invalidate", scrollbar, None, False) in surface._gui.events
    surface._procedure(other, 0x02A3, 0, 0)
    assert not surface._scroll_hover


def test_bar_paint_failure_still_finishes_paint_and_releases_its_gdi_objects(surface):
    prepare_scrolling(surface)
    scrollbar = surface._scrollbars[HISTORY_SCROLL]
    surface._gui.RoundRect = lambda *args: (_ for _ in ()).throw(OSError("Fixture paint failed"))
    surface._gui.events.clear()
    with pytest.raises(OSError, match="paint failed"):
        surface._paint_scrollbar(scrollbar)
    assert ("end-paint", scrollbar) in surface._gui.events
    assert len([event for event in surface._gui.events if event[0] == "delete"]) == 2


def test_programmatic_reflow_leaves_the_component_following_policy_authoritative(surface):
    prepare_scrolling(surface)
    entries = history_entries(6, text="A short message.")
    surface._update_history(entries)
    surface._scroll_to(3, 3)
    surface._history.following = False
    view = surface._history.view
    surface._button(EXPAND)
    assert surface._history.following is False
    surface._button(EXPAND)
    assert surface._history.view is view
    surface._history.unread = True
    surface._update_history((*entries, *history_entries(1, start=7)))
    assert surface._history_unread
    surface._scroll_latest()
    assert surface._history.following and not surface._history_unread


def test_reply_during_a_thumb_drag_preserves_reading_and_does_not_move_the_composer(surface):
    prepare_scrolling(surface)
    surface._scroll_to(3, 5)
    scrollbar = grab_thumb(surface)
    assert surface._history.interacting
    view = surface._history.view
    draft, selection = surface._gui.texts[7], surface._gui.selections.get(7)
    surface._update_history(history_entries(21))
    assert surface._history.view is view and surface._history.interacting
    assert surface._gui.capture == scrollbar
    assert surface._gui.texts[7] == draft and surface._gui.selections.get(7) == selection
    surface._procedure(scrollbar, win32con.WM_LBUTTONUP, 0, mouse_point(4, 2000))
    assert not surface._history.interacting


def test_native_keyboard_and_ime_messages_delegate_without_replacing_native_edit_behavior(surface):
    prepare_scrolling(surface)
    surface._scroll_to(7, surface._scroll_state(7).maximum)
    before = surface._scroll_state(7).position

    def page_up(handle, key):
        if key == win32con.VK_PRIOR:
            surface._gui.first_lines[handle] = max(0, surface._gui.first_lines[handle] - 2)

    surface._gui.native_key = page_up
    surface._gui.events.clear()
    surface._edit_procedure(7, win32con.WM_KEYDOWN, win32con.VK_PRIOR, 0)
    assert surface._scroll_state(7).position < before
    surface._edit_procedure(7, 0x010F, 0, 8)  # WM_IME_COMPOSITION stays with the native EDIT.
    calls = [
        e
        for e in surface._gui.events
        if e[0] == "default-edit" and e[1:4] == (7, win32con.WM_KEYDOWN, win32con.VK_PRIOR)
    ]
    assert len(calls) == 1
    assert ("default-edit", 7, 0x010F, 0, 8) in surface._gui.events
    surface._edit_procedure(7, win32con.WM_NCDESTROY, 0, 0)
    assert 7 not in surface._gui.subclasses
    assert ("remove-subclass", 7, 1) in surface._gui.events


def test_custom_scrollbar_keyboard_keeps_tab_navigation_and_wants_scrolling_keys(surface):
    prepare_scrolling(surface)
    scrollbar = surface._scrollbars[HISTORY_SCROLL]
    assert surface._procedure(scrollbar, win32con.WM_GETDLGCODE, win32con.VK_TAB, 0) == (
        win32con.DLGC_WANTARROWS
    )
    assert (
        surface._procedure(scrollbar, win32con.WM_GETDLGCODE, win32con.VK_NEXT, 0)
        & win32con.DLGC_WANTALLKEYS
    )


def test_empty_ime_preedit_is_native_and_survives_reflow_without_redraw_or_selection_toggles(
    surface,
):
    prepare_scrolling(surface)
    surface._edit_procedure(7, 0x010D, 0, 0)
    assert surface._ime_composing
    assert TeachingSurface._composition_active(surface)
    sends = []
    surface._user32.GetKeyState = lambda key: 0
    surface._send_user = lambda: sends.append(True)
    message = ctypes.wintypes.MSG(hWnd=7, message=win32con.WM_KEYDOWN, wParam=13, lParam=0)
    assert not surface._composer_key(message)
    surface._gui.events.clear()
    surface._gui.rect = (100, 100, 900, 400)
    surface._layout()
    assert not any(
        event[0] == "message"
        and event[1] == 7
        and event[2] in (win32con.WM_SETREDRAW, win32con.EM_SETSEL, win32con.EM_LINESCROLL)
        for event in surface._gui.events
    )
    assert not sends
    surface._edit_procedure(7, 0x010E, 0, 0)
    assert not surface._ime_composing
    assert surface._composer_key(message)
    assert sends == [True]


def test_incoming_history_does_not_toggle_composer_redraw_or_disturb_its_ime(surface):
    prepare_scrolling(surface)
    surface._edit_procedure(7, 0x010D, 0, 0)
    draft, selection = surface._gui.texts[7], surface._gui.selections.get(7)
    surface._gui.events.clear()
    surface._update_history(history_entries(21))
    assert surface._ime_composing
    assert surface._gui.texts[7] == draft and surface._gui.selections.get(7) == selection
    assert not any(
        event[0] == "message" and event[1] == 7 and event[2] == win32con.WM_SETREDRAW
        for event in surface._gui.events
    )


def test_x_while_a_thumb_is_captured_releases_capture_and_keeps_real_quit_semantics(surface):
    prepare_scrolling(surface)
    grab_thumb(surface)
    exits = []
    surface._on_exit = lambda: exits.append(True)
    surface._procedure(1, win32con.WM_CLOSE, 0, 0)
    assert exits == [True]
    assert surface._scroll_drag is None and not surface._gui.capture
    assert not surface.controller.snapshot().armed
    assert ("message", 1, win32con.WM_CANCELMODE, 0, 0) in surface._gui.events


def test_page_size_uses_the_native_formatting_rectangle_and_measured_font_height(surface):
    prepare_scrolling(surface)
    original = surface._gui.native_message

    def formatted(handle, message, wparam, lparam):
        if handle == 7 and message == win32con.EM_GETRECT:
            rect = ctypes.cast(lparam, ctypes.POINTER(ctypes.wintypes.RECT)).contents
            rect.left, rect.top, rect.right, rect.bottom = 4, 7, 500, 43
            return 0
        return original(handle, message, wparam, lparam)

    def metrics(dc, pointer):
        ctypes.cast(pointer, ctypes.POINTER(_TextMetric)).contents.height = 18
        return True

    surface._gui.native_message = formatted
    surface._gdi32.GetTextMetricsW = metrics
    surface._line_height = 0
    state = surface._scroll_state(7)
    assert state.page == 2
    assert surface._line_height == 18
    assert surface._gui.GetClientRect(7)[3] != 36


def test_history_component_dynamic_and_hidden_hwnds_are_protected_without_text_queries(surface):
    prepare_layout(surface)
    surface._history.roles = {
        9001: "transcript-history-body",
        9002: "transcript-history-label",
    }
    surface._gui.visible[9002] = False
    surface._gui.GetWindowText = lambda handle: pytest.fail("Roles must not inspect content")
    roles = surface.window_roles()
    assert roles[3] == "transcript-history"
    assert roles[9001] == "transcript-history-body"
    assert roles[9002] == "transcript-history-label"
    assert {9001, 9002} <= set(surface.window_handles())
    surface._history.roles[9003] = "transcript-history-body"
    assert 9003 in surface.window_handles()


def test_history_font_is_replaced_before_the_old_borrowed_font_is_deleted(surface):
    prepare_layout(surface)
    surface._set_font()
    surface._layout()
    assert surface._font == 60
    surface._gui.events.clear()
    original = surface._history.set_font

    def borrow(font, **kwargs):
        surface._gui.events.append(("history-font", font))
        original(font, **kwargs)

    surface._history.set_font = borrow
    surface._gui.CreateFontIndirect = lambda description: 61
    surface._replace_font()
    assert 60 in surface._retired_fonts
    assert ("delete", 60) not in surface._gui.events
    surface._layout()
    assert surface._gui.events.index(("history-font", 61)) < surface._gui.events.index(
        ("delete", 60)
    )
    assert not surface._retired_fonts


def test_history_wheel_and_pixel_scroll_commands_delegate_without_edit_line_conversion(surface):
    prepare_scrolling(surface)
    history = surface._history
    history.state = ScrollState(200000, 120, 70000)
    history.calls.clear()
    surface._scroll_to(3, 150000)
    assert ("scroll-to", 150000) in history.calls
    surface._scroll_command(3, win32con.SB_PAGEUP)
    assert ("scroll-command", win32con.SB_PAGEUP) in history.calls
    surface._procedure(
        surface._scrollbars[HISTORY_SCROLL],
        win32con.WM_MOUSEWHEEL,
        (-30 & 0xFFFF) << 16,
        0,
    )
    assert ("wheel", -30, 3) in history.calls
    with pytest.raises(RuntimeError, match="only to the composer"):
        surface._sample_view(3)


@pytest.mark.parametrize("visible,motion", [(False, True), (True, False), (True, True)])
def test_arrival_animation_policy_is_visible_and_reduced_motion_aware(surface, visible, motion):
    prepare_layout(surface)
    surface._shown = visible
    surface._motion_enabled = motion
    entries = history_entries(1)
    surface._update_history(entries, now=20.0)
    assert surface._history.calls[-1] == ("entries", entries, 20.0, visible and motion)
    surface._history.animation_active = True
    surface._tick_history(20.016)
    if visible and motion:
        assert ("tick", 20.016) in surface._history.calls
        assert surface._chat_animating
    else:
        assert ("cancel-animation",) in surface._history.calls
        assert not surface._chat_animating


def test_hide_show_reflow_and_motion_setting_changes_do_not_replay_arrivals(surface):
    prepare_layout(surface)
    surface._shown = True
    surface._motion_enabled = True
    entries = history_entries(2)
    surface._update_history(entries)
    history = surface._history
    history.animation_active = True
    history.calls.clear()
    surface._apply_visibility(False)
    surface._apply_visibility(True)
    surface._layout()
    surface._update_history(entries)
    assert not history.animation_active
    assert not any(call[0] == "entries" for call in history.calls)
    history.animation_active = True
    surface._gui.client_animations = False
    surface._procedure(1, 0x001A, 0x1043, 0)
    assert not surface._motion_enabled and not history.animation_active


def test_chat_arrivals_tick_without_sixty_hz_model_or_status_churn(surface, monkeypatch):
    prepare_layout(surface)
    surface.controller.stop()
    clock = [10.0]
    monkeypatch.setattr("desktop_mcp.teaching_ui.time.monotonic", lambda: clock[0])
    surface._shown = True
    surface._motion_enabled = True
    surface._history.animation_active = True
    counts = []
    surface.session.snapshot = lambda: (
        counts.append("model") or TeachingSnapshot(1, (), (), None, None)
    )
    surface._refresh_status = lambda *args: counts.append("status")
    surface._timer_running = True
    surface._timer_interval = _IDLE_TIMER_MS
    surface._on_timer()
    clock[0] += 0.016
    surface._on_timer()
    assert counts == ["model", "status"]
    assert len([call for call in surface._history.calls if call[0] == "tick"]) == 2
    assert surface._timer_interval == _ANIMATION_TIMER_MS
    for index in range(2, 121):
        clock[0] = 10.0 + index * 0.016
        surface._on_timer()
    assert 50 <= counts.count("model") <= 61
    assert counts.count("status") == counts.count("model")
    assert len([call for call in surface._history.calls if call[0] == "tick"]) == 121
    assert surface._next_state_refresh > clock[0]
    assert not surface._gui.IsWindowVisible(surface._canvas)


def test_settling_frame_stops_fast_timer_when_component_reports_no_active_arrivals(surface):
    prepare_layout(surface)
    surface._shown = True
    surface._motion_enabled = True
    surface._history.animation_active = True
    surface._timer_running = True
    surface._timer_interval = _ANIMATION_TIMER_MS

    def settle(now):
        surface._history.animation_active = False
        return True

    surface._history.tick = settle
    surface._tick_history(10.18)
    surface._schedule_timer()
    assert not surface._chat_animating
    assert surface._timer_interval == _IDLE_TIMER_MS


def test_component_failure_is_content_free_and_releases_local_capture(surface):
    prepare_scrolling(surface)
    grab_thumb(surface)
    surface._history_failed(OSError("Private body must not become status text"))
    assert surface._exit and not surface._gui.capture
    assert not surface.controller.snapshot().interface_ready
    assert "Private body" not in surface.controller.snapshot().reason
    assert ("cancel-interaction",) in surface._history.calls


def test_history_close_is_explicit_without_deleting_its_borrowed_font(surface):
    prepare_layout(surface)
    history = surface._history
    surface._font = 60
    surface._close_history()
    assert ("close",) in history.calls
    assert surface._history is None and surface._history_window == 0
    assert ("delete", 60) not in surface._gui.events


def test_real_history_component_integrates_through_its_public_api_with_fake_windows(surface):
    import win32gui

    from desktop_mcp.transcript_chat import native_text
    from desktop_mcp.transcript_chat_native import NativeChatHistory
    from tests.test_desktop_transcript_chat_native import FakeWin32

    prepare_layout(surface)
    native = FakeWin32()
    history = NativeChatHistory(
        on_change=surface._history_changed, on_error=surface._history_failed
    )

    def load():
        history._gui = SimpleNamespace(
            **{name: getattr(native, name) for name in dir(native) if hasattr(win32gui, name)}
        )
        history._api = history._comctl = history._user32 = native
        history._con = win32con
        history._text_callback = history._text_procedure

    history._load_native = load
    surface._history = history
    surface._history_window = history.create(1, 7, HISTORY)
    surface._font = 700
    surface._line_height = native.pitches[700]
    surface._history_font_dirty = True
    move = surface._gui.MoveWindow

    def position(handle, x, y, width, height, repaint):
        move(handle, x, y, width, height, repaint)
        if handle == history.hwnd:
            native.MoveWindow(handle, x, y, width, height, False)

    surface._gui.MoveWindow = position
    try:
        surface._layout()
        surface._shown = True
        surface._motion_enabled = True
        entries = history_entries(8, text="Full message 😀\n" * 100)
        surface._update_history(entries, now=10.0)
        assert not history.animation_active
        assert set(history.window_handles()) <= set(surface.window_handles())
        body = history._bubbles[3].editor
        assert native.GetWindowText(body) == native_text(entries[2][2])
        native.SendMessage(body, win32con.EM_SETSEL, 5, 900)
        history.scroll_to(100)
        view = history.capture_view()
        surface._button(EXPAND)
        restored = history.capture_view()
        assert restored.anchor == view.anchor
        assert restored.messages == view.messages
        next_font = [710]

        def create_font(description):
            next_font[0] += 1
            surface._gui.font_height = -description.lfHeight
            surface._gui.font_face = description.lfFaceName
            native.pitches[next_font[0]] = surface._gui.font_height + 2
            return next_font[0]

        surface._gui.CreateFontIndirect = create_font
        surface._gui.foreground, surface._gui.focused = surface._panel, surface._composer
        surface._user32.GetKeyState = lambda key: 0x8000 if key == win32con.VK_CONTROL else 0
        for key, selected in ((0xBB, 16), (0xBD, 14), (0xBD, 12)):
            press_text_size(surface, key)
            resized = history.capture_view()
            assert resized.anchor == view.anchor and resized.messages == view.messages
            assert surface.layout_status()["font_dip"] == selected
            assert native.GetWindowText(body) == native_text(entries[2][2])
        surface._update_history(
            (*entries, (9, "Assistant", "A complete new reply", "assistant")), now=11.0
        )
        assert history.unread and not history.following
        assert surface._history_unread
        assert native.GetWindowText(body) == native_text(entries[2][2])
        surface._scroll_latest()
        assert history.following and not surface._history_unread
        assert not surface._exit
    finally:
        surface._close_history()
    assert not native.objects and not native.classes and not native.subclasses
    assert 700 not in native.deleted


def fake_scene(surface, monkeypatch, *, waiting=None):
    clock = [1.0]
    monkeypatch.setattr("desktop_mcp.teaching_ui.time.monotonic", lambda: clock[0])
    mark = Mark("laser", "laser", ((20, 20),), "#ffb454", 3, 0.0, 10.0, None)
    snapshot = [TeachingSnapshot(1, (), (mark,) if waiting is None else (), waiting, None)]
    surface.session.snapshot = lambda: snapshot[0]
    surface._api.GetSystemMetrics = lambda code: {76: 0, 77: 0, 78: 1920, 79: 1080}[code]
    bounds_times, renders, uploads, closed = [], [], [], []
    before_return = [lambda: None]

    def bounds(value, desktop, *, now):
        bounds_times.append(now)
        return (0, 0, 40, 40) if value.marks or value.waiting is not None else None

    def render(value, rectangle, *, now):
        renders.append(now)
        before_return[0]()
        return SimpleNamespace(close=lambda: closed.append(now))

    surface._scene_bounds = bounds
    monkeypatch.setattr("desktop_mcp.teaching_render.render_marks", render)
    monkeypatch.setattr("desktop_mcp.layers.upload_rgba", lambda *args: uploads.append(args))
    return SimpleNamespace(
        clock=clock,
        snapshot=snapshot,
        bounds=bounds_times,
        renders=renders,
        uploads=uploads,
        closed=closed,
        before_return=before_return,
    )


def test_root_deactivation_cancels_internal_history_capture_and_settles_arrivals(surface):
    prepare_scrolling(surface)
    surface._history.interacting = True
    surface._history.animation_active = True
    surface._procedure(1, win32con.WM_ACTIVATE, 0, 0)
    assert not surface._history.interacting and not surface._history.animation_active
    assert ("cancel-interaction",) in surface._history.calls


def test_same_wait_snapshot_is_not_a_time_animation_and_new_progress_still_repaints(
    surface, monkeypatch
):
    scene = fake_scene(surface, monkeypatch, waiting=WaitTarget((10, 10), 28, True, 0.1, 1.0))
    control = surface.controller.snapshot()
    surface._refresh_scene(control, scene.snapshot[0], 1.0)
    surface._refresh_scene(control, scene.snapshot[0], 1.016)
    assert not surface._scene_animating
    assert scene.renders == [1.0]
    scene.snapshot[0] = TeachingSnapshot(1, (), (), WaitTarget((10, 10), 28, True, 0.5, 1.1), None)
    surface._refresh_scene(control, scene.snapshot[0], 1.033)
    assert scene.renders == [1.0, 1.033]
    assert scene.closed == scene.renders


@pytest.mark.parametrize(
    "now,animated", [(-0.001, False), (0.0, True), (9.999, True), (10.0, False)]
)
def test_only_live_started_lasers_request_fast_animation_cadence(
    surface, monkeypatch, now, animated
):
    scene = fake_scene(surface, monkeypatch)
    surface._refresh_scene(surface.controller.snapshot(), scene.snapshot[0], now)
    assert surface._scene_animating is animated


def test_repaint_tick_observes_removed_marks_without_rebuilding_chat_or_status(
    surface, monkeypatch
):
    scene = fake_scene(surface, monkeypatch)
    updates = []
    surface._update_history = lambda *args, **kwargs: updates.append("history")
    surface._refresh_status = lambda *args: updates.append("status")
    surface._sync_scrollbars = lambda: None
    surface._on_timer()
    assert len(scene.uploads) == 1
    scene.snapshot[0] = TeachingSnapshot(2, (), (), None, None)
    scene.clock[0] = 1.016
    surface._on_timer()
    assert updates == ["history", "status"]
    assert len(scene.uploads) == 1 and not surface._scene_animating
    assert not surface._gui.IsWindowVisible(2)


@pytest.mark.parametrize("change", ["stop", "rearm", "erase", "capture", "capture-restore"])
def test_frame_is_closed_and_not_uploaded_after_revocation_or_liveness_change(
    surface, monkeypatch, change
):
    scene = fake_scene(surface, monkeypatch)
    control = surface.controller.snapshot()
    pending = []

    def change_during_render():
        if change == "stop":
            surface.controller.stop()
        elif change == "rearm":
            surface.controller.stop()
            surface.controller.arm_local()
        elif change == "erase":
            scene.snapshot[0] = TeachingSnapshot(2, (), (), None, None)
        else:
            request = _Request("hide")
            pending.append(request)
            surface._requests.put(request)
            if change == "capture-restore":
                restore = _Request("restore")
                pending.append(restore)
                surface._requests.put(restore)

    scene.before_return[0] = change_during_render
    surface._refresh_scene(control, scene.snapshot[0], 1.0)
    assert not scene.uploads
    assert scene.closed == [1.0]
    assert surface._scene_snapshot is None and surface._scene_ticket is None
    assert not surface._gui.IsWindowVisible(2)
    for request in pending:
        assert request.done.is_set() and request.error is None
    if change == "capture":
        assert dispatch(surface, "restore").error is None


def test_upload_failure_closes_the_returned_image(surface, monkeypatch):
    scene = fake_scene(surface, monkeypatch)

    def fail(*args):
        raise OSError("Synthetic upload failure")

    monkeypatch.setattr("desktop_mcp.layers.upload_rgba", fail)
    with pytest.raises(OSError, match="upload failure"):
        surface._refresh_scene(surface.controller.snapshot(), scene.snapshot[0], 1.0)
    assert scene.closed == [1.0] and not surface._scene_rendering


def test_heavy_frames_are_coalesced_with_request_service_and_no_catch_up(surface, monkeypatch):
    scene = fake_scene(surface, monkeypatch)
    scene.before_return[0] = lambda: scene.clock.__setitem__(0, scene.clock[0] + 0.05)
    control = surface.controller.snapshot()
    surface._refresh_scene(control, scene.snapshot[0], 1.0)
    assert scene.renders == [1.0]
    assert surface._next_scene_frame == pytest.approx(1.075)
    scene.clock[0] = 1.06
    surface._refresh_scene(control, scene.snapshot[0], scene.clock[0])
    assert scene.renders == [1.0]
    request = _Request("hide")
    surface._requests.put(request)
    surface._procedure(1, win32con.WM_TIMER, 1, 0)
    assert request.done.is_set() and request.error is None
    assert scene.renders == [1.0]
    assert dispatch(surface, "restore").error is None
    scene.clock[0] = 2.0
    surface._refresh_scene(control, scene.snapshot[0], scene.clock[0])
    assert scene.renders == [1.0, 2.0]
    assert scene.closed == scene.renders
    assert scene.bounds[-1] == scene.renders[-1]


def test_manual_chat_refresh_does_not_add_extra_laser_frames_between_animation_wakes(
    surface, monkeypatch
):
    scene = fake_scene(surface, monkeypatch)
    surface._update_history = lambda *args, **kwargs: None
    surface._refresh_status = lambda *args: None
    surface._sync_scrollbars = lambda: None
    surface._on_timer()
    scene.clock[0] = 1.005
    surface._refresh()
    assert scene.renders == [1.0]
    scene.clock[0] = 1.016
    surface._on_timer()
    assert scene.renders == [1.0, 1.016]


def test_input_revision_change_during_render_does_not_rebase_the_old_scene(surface, monkeypatch):
    scene = fake_scene(surface, monkeypatch)
    original = surface.controller.snapshot()
    state = [original]
    monkeypatch.setattr(surface.controller, "snapshot", lambda: state[0])
    scene.before_return[0] = lambda: state.__setitem__(
        0, replace(original, input_revision=original.input_revision + 1)
    )
    surface._refresh_scene(original, scene.snapshot[0], 1.0)
    assert not scene.uploads and scene.closed == [1.0]
    assert surface._scene_ticket is None and surface._scene_snapshot is None


def test_idle_33ms_wakes_do_not_halve_state_polling_and_long_gaps_are_coalesced(
    surface, monkeypatch
):
    clock = [10.0]
    monkeypatch.setattr("desktop_mcp.teaching_ui.time.monotonic", lambda: clock[0])
    polls = []
    surface.session.snapshot = lambda: (
        polls.append(clock[0]) or TeachingSnapshot(1, (), (), None, None)
    )
    surface._update_history = lambda *args, **kwargs: None
    surface._refresh_status = lambda *args: None
    surface._sync_scrollbars = lambda: None
    surface._refresh_scene = lambda *args, **kwargs: None
    for index in range(121):
        clock[0] = 10.0 + index * 0.033
        surface._on_timer()
    assert len(polls) == 121
    clock[0] += 5.0
    surface._on_timer()
    assert len(polls) == 122
    assert clock[0] < surface._next_state_refresh <= clock[0] + 0.033001


def test_16ms_animation_sequence_has_no_periodic_bucket_gap(surface, monkeypatch):
    scene = fake_scene(surface, monkeypatch)
    surface._update_history = lambda *args, **kwargs: None
    surface._refresh_status = lambda *args: None
    surface._sync_scrollbars = lambda: None
    expected = []
    for index in range(121):
        scene.clock[0] = 1.0 + index * 0.016
        expected.append(scene.clock[0])
        surface._on_timer()
    assert scene.renders == expected
    assert scene.closed == expected


def prepare_text_sizes(surface, *, scale=1.0, focused=7):
    prepare_scrolling(surface, scale=scale)
    surface._shown = True
    surface._gui.foreground = surface._panel
    surface._gui.focused = focused
    keys = {win32con.VK_CONTROL: 0x8000}
    surface._user32.GetKeyState = lambda key: keys.get(key, 0)
    return keys


def text_size_message(surface, key, *, kind=win32con.WM_KEYDOWN, repeat=False, window=None):
    return ctypes.wintypes.MSG(
        hWnd=surface._gui.GetFocus() if window is None else window,
        message=kind,
        wParam=key,
        lParam=(1 << 30) if repeat else 0,
    )


def press_text_size(surface, key):
    assert surface._text_size_key(text_size_message(surface, key))
    assert surface._text_size_key(text_size_message(surface, key, kind=win32con.WM_KEYUP))


def test_text_size_has_exactly_three_clamped_steps_and_a_medium_default(surface):
    prepare_text_sizes(surface)
    assert surface.layout_status()["text_size"] == "Medium"
    assert surface.layout_status()["font_dip"] == 14
    sizes = []
    for key in (0xBB, 0xBB, 0xBD, 0xBD, 0xBD, 0xBB, 0xBB):
        press_text_size(surface, key)
        sizes.append(surface.layout_status()["font_dip"])
    assert sizes == [16, 16, 14, 12, 12, 14, 16]
    assert surface._font_face == "Segoe UI Variable Text"
    assert "Text: Large" in surface._gui.texts[surface._history_label]
    assert "Ctrl +/-" in surface._gui.texts[surface._history_label]


@pytest.mark.parametrize(
    "key,shift,expected",
    [(0xBB, False, 16), (0xBB, True, 16), (0x6B, False, 16), (0xBD, False, 12), (0x6D, False, 12)],
)
def test_text_size_accepts_equals_plus_minus_and_keypad(surface, key, shift, expected):
    keys = prepare_text_sizes(surface)
    if shift:
        keys[win32con.VK_SHIFT] = 0x8000
    press_text_size(surface, key)
    assert surface.layout_status()["font_dip"] == expected


@pytest.mark.parametrize("focused", [1, 3, 7, 8, 301, 406, 9001])
def test_text_size_is_local_to_focused_transcript_descendants(surface, focused):
    prepare_text_sizes(surface, focused=focused)
    surface._history.roles[9001] = "transcript-history-text"
    press_text_size(surface, 0xBB)
    assert surface.layout_status()["font_dip"] == 16


@pytest.mark.parametrize(
    "boundary",
    [
        "other-app",
        "other-window",
        "wrong-target",
        "canvas",
        "no-ctrl",
        "altgr",
        "ime-flag",
        "ime-context",
        "hidden",
        "system-key",
    ],
)
def test_text_size_does_not_intercept_focus_modifier_or_ime_boundaries(surface, boundary):
    keys = prepare_text_sizes(surface)
    message = text_size_message(surface, 0xBB)
    if boundary == "other-app":
        surface._gui.foreground = 999
    elif boundary == "other-window":
        surface._gui.focused = 999
        message.hWnd = 999
    elif boundary == "wrong-target":
        message.hWnd = surface._panel
    elif boundary == "canvas":
        surface._gui.focused = surface._canvas
        message.hWnd = surface._canvas
    elif boundary == "no-ctrl":
        keys.clear()
    elif boundary == "altgr":
        keys[win32con.VK_MENU] = 0x8000
    elif boundary == "ime-flag":
        surface._ime_composing = True
    elif boundary == "ime-context":
        surface._composition_active = lambda: True
    elif boundary == "hidden":
        surface._shown = False
    else:
        message.message = win32con.WM_SYSKEYDOWN
    before = surface._font
    assert not surface._text_size_key(message)
    assert surface._font == before and surface.layout_status()["font_dip"] == 14


@pytest.mark.parametrize(
    "key,char",
    [
        (0xBB, ord("=")),
        (0xBB, ord("+")),
        (0x6B, ord("+")),
        (0xBD, ord("-")),
        (0x6D, ord("-")),
        (0xBD, 0x1F),
    ],
)
def test_text_size_consumes_duplicate_key_and_char_events_without_touching_draft(
    surface, key, char
):
    prepare_text_sizes(surface)
    draft = surface._gui.texts[7]
    assert surface._text_size_key(text_size_message(surface, key))
    expected = surface.layout_status()["font_dip"]
    surface._gui.events.clear()
    assert surface._text_size_key(text_size_message(surface, key, repeat=True))
    assert surface._text_size_key(text_size_message(surface, key))
    assert surface._text_size_key(text_size_message(surface, char, kind=win32con.WM_CHAR))
    assert surface.layout_status()["font_dip"] == expected
    assert surface._gui.texts[7] == draft
    assert not any(
        event[0] in {"begin-defer", "focus", "position"} for event in surface._gui.events
    )
    assert surface._text_size_key(text_size_message(surface, key, kind=win32con.WM_KEYUP))
    assert not surface._text_size_key(text_size_message(surface, char, kind=win32con.WM_CHAR))


def test_text_size_choice_survives_modes_hide_resize_and_dpi_without_losing_state(surface):
    prepare_text_sizes(surface)
    surface.controller.stop()
    generation = surface.controller.snapshot().generation
    surface.session.conversation.send_user("Keep this pending correction")
    surface._gui.SetWindowText(7, "Draft 😀\r\nstill being written")
    surface._gui.selections[7] = (6, 14)
    view = surface._history.view
    original_bounds = surface._gui.rect
    surface._gui.events.clear()
    press_text_size(surface, 0x6D)
    assert surface._gui.rect == original_bounds
    assert not any(event[0] in {"focus", "position"} for event in surface._gui.events)
    surface._button(EXPAND)
    surface._apply_visibility(False)
    surface._apply_visibility(True)
    surface._gui.rect = (100, 100, 1000, 500)
    surface._layout()
    surface._button(EXPAND)
    proposed = ctypes.wintypes.RECT(100, 100, 1780, 500)
    surface._gui.chrome = (24, 58)
    surface._procedure(1, 0x02E0, 144 | (144 << 16), ctypes.addressof(proposed))
    status = surface.layout_status()
    assert status["text_size"] == "Small" and status["font_dip"] == 12
    assert status["font_height"] == round(12 * surface._scale)
    assert status["font_face"] == "Segoe UI Variable Text"
    assert surface._gui.texts[7] == "Draft 😀\r\nstill being written"
    assert surface._gui.selections[7] == (6, 14)
    assert surface._history.view is view
    assert surface.session.conversation.status()["pending_messages"] == 1
    assert surface.controller.snapshot().generation == generation
    assert not surface.controller.snapshot().armed


def test_text_shortcut_is_still_discoverable_in_a_narrow_host(surface):
    prepare_text_sizes(surface)
    surface._gui.rect = (0, 0, 380, 340)
    surface._layout()
    label = surface._gui.texts[surface._history_label]
    assert "Medium" in label and "Ctrl +/-" in label
    assert surface._text_width(label) <= surface._gui.positions[surface._history_label][2]


def test_deactivation_and_ime_start_clear_pending_text_shortcut_char_suppression(surface):
    prepare_text_sizes(surface)
    assert surface._text_size_key(text_size_message(surface, 0xBB))
    surface._procedure(1, win32con.WM_ACTIVATE, 0, 0)
    assert not surface._text_size_keys_held
    assert not surface._text_size_key(text_size_message(surface, ord("+"), kind=win32con.WM_CHAR))
    assert surface._text_size_key(text_size_message(surface, 0xBD))
    surface._edit_procedure(7, 0x010D, 0, 0)
    assert not surface._text_size_keys_held
    assert not surface._text_size_key(text_size_message(surface, ord("-"), kind=win32con.WM_CHAR))
