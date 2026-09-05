"""A native floating transcript and separate click-through annotation layer."""

from __future__ import annotations

from contextlib import ExitStack, contextmanager
from collections.abc import Callable
import ctypes
from ctypes import wintypes
from dataclasses import dataclass, field
import queue
import threading
import time
from typing import TYPE_CHECKING, Iterator, Literal
import uuid

from desktop_mcp.contracts import Rect

if TYPE_CHECKING:
    from desktop_mcp.runtime import Controller
    from desktop_mcp.teaching import TeachingSession, TeachingSnapshot

_COMMAND = 0x8000 + 73
_PIN, _TOP, _BOTTOM, _CLEAR, _STOP = range(201, 206)


@dataclass
class _Request:
    command: str
    argument: str = ""
    generation: int | None = None
    done: threading.Event = field(default_factory=threading.Event)
    error: Exception | None = None
    cancelled: threading.Event = field(default_factory=threading.Event)
    lock: threading.Lock = field(default_factory=threading.Lock)


class _DrawItem(ctypes.Structure):
    _fields_ = [
        ("ctl_type", wintypes.UINT),
        ("ctl_id", wintypes.UINT),
        ("item_id", wintypes.UINT),
        ("action", wintypes.UINT),
        ("state", wintypes.UINT),
        ("window", wintypes.HWND),
        ("dc", wintypes.HDC),
        ("rect", wintypes.RECT),
        ("data", ctypes.c_size_t),
    ]


class _MinMaxInfo(ctypes.Structure):
    _fields_ = [
        ("reserved", wintypes.POINT),
        ("max_size", wintypes.POINT),
        ("max_position", wintypes.POINT),
        ("min_track", wintypes.POINT),
        ("max_track", wintypes.POINT),
    ]


class TeachingSurface:
    """Native presentation only; input authority remains with the shared controller."""

    def __init__(
        self,
        controller: Controller,
        session: TeachingSession,
        *,
        on_exit: Callable[[], None] | None = None,
    ) -> None:
        self.controller = controller
        self.session = session
        self._on_exit = on_exit
        self._ready = threading.Event()
        self._finished = threading.Event()
        self._requests: queue.Queue[_Request] = queue.Queue()
        self._thread: threading.Thread | None = None
        self._error: Exception | None = None
        self._panel = 0
        self._canvas = 0
        self._editor = 0
        self._status = 0
        self._buttons: dict[int, int] = {}
        self._hide_count = 0
        self._restore_panel: tuple[bool, bool] = (False, False)
        self._shown = False
        self._pinned = False
        self._last_text: tuple = ()
        self._last_scene: tuple | None = None
        self._exit = False
        self._font = 0
        self._background = 0
        self._scale = 1.0
        self._status_lines = 2
        self._compact_status = False

    def start(self) -> None:
        if self._thread is not None:
            if self._error is not None:
                raise RuntimeError("The teaching interface failed.") from self._error
            return
        self._thread = threading.Thread(target=self._run, name="Desktop-MCP guidance", daemon=True)
        self._thread.start()
        if not self._ready.wait(8):
            self._exit = True
            self.controller.stop("The teaching interface did not start.")
            raise RuntimeError("The teaching interface did not start in time.")
        if self._error is not None or self._finished.is_set():
            raise RuntimeError("The teaching interface could not start.") from self._error

    def window_handles(self) -> tuple[int, ...]:
        return tuple(
            handle
            for handle in (
                self._panel,
                self._canvas,
                self._editor,
                self._status,
                *self._buttons.values(),
            )
            if handle
        )

    def show(self, stacking: Literal["unchanged", "front", "back"] = "unchanged") -> None:
        if stacking not in {"unchanged", "front", "back"}:
            raise ValueError("Unknown transcript stacking action.")
        generation = self.controller.snapshot().generation
        self.controller.checkpoint()
        self._request("show", stacking, generation=generation)

    def close(self) -> None:
        thread = self._thread
        if thread is None or not thread.is_alive():
            return
        try:
            if self._panel and not self._finished.is_set():
                self._request("close")
        finally:
            self._exit = True
            thread.join(3)
            if thread.is_alive():
                self.controller.set_interface_ready(False, "The teaching interface did not stop.")
                raise RuntimeError("The teaching interface did not shut down.")

    def _request(self, command: str, argument: str = "", generation: int | None = None) -> None:
        if self._thread is None or self._finished.is_set() or not self._panel:
            raise RuntimeError("The teaching interface is unavailable.")
        request = _Request(command, argument, generation)
        self._requests.put(request)
        import win32gui

        try:
            win32gui.PostMessage(self._panel, _COMMAND, 0, 0)
        except Exception as error:
            with request.lock:
                if command not in {"restore", "close"}:
                    request.cancelled.set()
            self.controller.set_interface_ready(False, "The teaching interface is unavailable.")
            raise RuntimeError("The guidance request could not be posted.") from error
        deadline = time.monotonic() + 3.0
        while not request.done.wait(0.02):
            if self._finished.is_set() or time.monotonic() >= deadline:
                with request.lock:
                    if request.done.is_set():
                        break
                    if command not in {"restore", "close"}:
                        request.cancelled.set()
                self.controller.set_interface_ready(
                    False, "The teaching interface is unresponsive."
                )
                raise RuntimeError("The teaching interface did not acknowledge the request.")
        if request.error is not None:
            raise RuntimeError(str(request.error)) from request.error

    @contextmanager
    def capture_guard(self) -> Iterator[None]:
        self._request("hide")
        try:
            yield
        finally:
            self._request("restore")

    def _run(self) -> None:
        try:
            self._run_windows()
        except Exception as error:
            self._error = error
            self.controller.set_interface_ready(
                False, f"Guidance interface failed: {type(error).__name__}"
            )
        finally:
            self._ready.set()
            self._finished.set()
            while not self._requests.empty():
                request = self._requests.get_nowait()
                request.error = RuntimeError("The guidance interface closed.")
                request.done.set()

    def _run_windows(self) -> None:
        import win32api
        import win32con
        import win32gui
        import pywintypes

        self._api, self._con, self._gui = win32api, win32con, win32gui
        self._native_error = pywintypes.error
        self._user32 = ctypes.WinDLL("user32", use_last_error=True)
        self._user32.SetThreadDpiAwarenessContext.argtypes = [ctypes.c_void_p]
        self._user32.SetThreadDpiAwarenessContext.restype = ctypes.c_void_p
        self._user32.SetWindowDisplayAffinity.argtypes = [wintypes.HWND, wintypes.DWORD]
        self._user32.SetWindowDisplayAffinity.restype = wintypes.BOOL
        self._user32.SetTimer.argtypes = [
            wintypes.HWND,
            ctypes.c_size_t,
            wintypes.UINT,
            ctypes.c_void_p,
        ]
        self._user32.SetTimer.restype = ctypes.c_size_t
        self._user32.KillTimer.argtypes = [wintypes.HWND, ctypes.c_size_t]
        self._user32.KillTimer.restype = wintypes.BOOL
        self._user32.PeekMessageW.argtypes = [
            ctypes.POINTER(wintypes.MSG),
            wintypes.HWND,
            wintypes.UINT,
            wintypes.UINT,
            wintypes.UINT,
        ]
        self._user32.PeekMessageW.restype = wintypes.BOOL
        self._user32.IsDialogMessageW.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.MSG)]
        self._user32.IsDialogMessageW.restype = wintypes.BOOL
        self._user32.TranslateMessage.argtypes = [ctypes.POINTER(wintypes.MSG)]
        self._user32.TranslateMessage.restype = wintypes.BOOL
        self._user32.DispatchMessageW.argtypes = [ctypes.POINTER(wintypes.MSG)]
        self._user32.DispatchMessageW.restype = ctypes.c_ssize_t
        self._dwm = ctypes.WinDLL("dwmapi", use_last_error=True)
        self._dwm.DwmFlush.restype = ctypes.c_long
        self._dwm.DwmSetWindowAttribute.argtypes = [
            wintypes.HWND,
            wintypes.DWORD,
            ctypes.c_void_p,
            wintypes.DWORD,
        ]
        self._dwm.DwmSetWindowAttribute.restype = ctypes.c_long
        instance = win32api.GetModuleHandle(None)
        class_name = f"DesktopMCPGuidance{uuid.uuid4().hex}"
        registered = False
        previous_dpi = self._user32.SetThreadDpiAwarenessContext(ctypes.c_void_p(-4))
        if not previous_dpi:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            self._background = win32gui.CreateSolidBrush(win32api.RGB(23, 24, 27))
            cls = win32gui.WNDCLASS()
            cls.hInstance = instance
            cls.lpszClassName = class_name
            cls.lpfnWndProc = self._procedure
            cls.hbrBackground = self._background
            cls.hCursor = win32gui.LoadCursor(0, win32con.IDC_ARROW)
            win32gui.RegisterClass(cls)
            registered = True
            self._panel = win32gui.CreateWindowEx(
                win32con.WS_EX_APPWINDOW | win32con.WS_EX_CONTROLPARENT,
                class_name,
                "Desktop-MCP - Instructions",
                win32con.WS_OVERLAPPEDWINDOW,
                80,
                620,
                680,
                290,
                0,
                0,
                instance,
                None,
            )
            self._canvas = win32gui.CreateWindowEx(
                win32con.WS_EX_LAYERED
                | win32con.WS_EX_TRANSPARENT
                | win32con.WS_EX_TOPMOST
                | win32con.WS_EX_TOOLWINDOW
                | 0x08000000,
                class_name,
                "Desktop-MCP - Guidance overlay",
                win32con.WS_POPUP,
                0,
                0,
                1,
                1,
                0,
                0,
                instance,
                None,
            )
            for handle in (self._panel, self._canvas):
                # Capture guards also hide/flush; affinity is an additional OS optimization.
                self._user32.SetWindowDisplayAffinity(handle, 0x11)
            enabled, rounded = ctypes.c_int(1), ctypes.c_int(2)
            self._dwm.DwmSetWindowAttribute(self._panel, 20, ctypes.byref(enabled), 4)
            self._dwm.DwmSetWindowAttribute(self._panel, 33, ctypes.byref(rounded), 4)
            self._editor = win32gui.CreateWindowEx(
                0,
                "EDIT",
                "Instructions will appear here when the agent publishes a step.",
                win32con.WS_CHILD
                | win32con.WS_VISIBLE
                | win32con.WS_VSCROLL
                | win32con.WS_TABSTOP
                | win32con.ES_MULTILINE
                | win32con.ES_READONLY
                | win32con.ES_AUTOVSCROLL,
                16,
                16,
                620,
                170,
                self._panel,
                301,
                instance,
                None,
            )
            self._status = win32gui.CreateWindowEx(
                0,
                "STATIC",
                "Ctrl+Shift+H stops the session.",
                win32con.WS_CHILD | win32con.WS_VISIBLE,
                16,
                190,
                500,
                20,
                self._panel,
                302,
                instance,
                None,
            )
            for identifier, label in (
                (_PIN, "Pin"),
                (_TOP, "Top"),
                (_BOTTOM, "Bottom"),
                (_CLEAR, "Clear ink"),
                (_STOP, "Stop"),
            ):
                self._buttons[identifier] = win32gui.CreateWindowEx(
                    0,
                    "BUTTON",
                    label,
                    win32con.WS_CHILD
                    | win32con.WS_VISIBLE
                    | win32con.WS_TABSTOP
                    | win32con.BS_OWNERDRAW,
                    0,
                    0,
                    80,
                    30,
                    self._panel,
                    identifier,
                    instance,
                    None,
                )
            self._set_font()
            self._dock("bottom")
            self._layout()
            self._refresh()
            win32gui.ShowWindow(self._panel, win32con.SW_SHOWNOACTIVATE)
            self._shown = True
            if not self._user32.SetTimer(self._panel, 1, 33, None):
                raise ctypes.WinError(ctypes.get_last_error())
            self._ready.set()
            message = wintypes.MSG()
            while not self._exit:
                while self._user32.PeekMessageW(ctypes.byref(message), None, 0, 0, 1):
                    if message.message == win32con.WM_QUIT:
                        self._exit = True
                        break
                    if not self._user32.IsDialogMessageW(self._panel, ctypes.byref(message)):
                        self._user32.TranslateMessage(ctypes.byref(message))
                        self._user32.DispatchMessageW(ctypes.byref(message))
                self._finished.wait(0.004)
        finally:
            try:
                with ExitStack() as cleanup:
                    cleanup.callback(self._user32.SetThreadDpiAwarenessContext, previous_dpi)
                    if registered:
                        cleanup.callback(win32gui.UnregisterClass, class_name, instance)
                    elif self._background:
                        cleanup.callback(win32gui.DeleteObject, self._background)
                    if self._font:
                        cleanup.callback(win32gui.DeleteObject, self._font)
                    for handle in (self._panel, self._canvas):
                        if handle and win32gui.IsWindow(handle):
                            cleanup.callback(win32gui.DestroyWindow, handle)
                    if self._panel and win32gui.IsWindow(self._panel):
                        self._user32.KillTimer(self._panel, 1)
            finally:
                self._canvas = self._panel = self._editor = self._status = 0
                self._buttons.clear()
                self._font = self._background = 0

    def _procedure(self, handle, message, wparam, lparam):
        gui, con = self._gui, self._con
        try:
            if message == _COMMAND:
                self._drain_requests()
                return 0
            if self._exit:
                return gui.DefWindowProc(handle, message, wparam, lparam)
            if message == con.WM_TIMER and handle == self._panel:
                self._drain_requests()
                if not self._exit:
                    self._refresh()
                return 0
            if message == con.WM_CLOSE and handle == self._panel:
                try:
                    self.controller.stop("Desktop-MCP is quitting.")
                finally:
                    if self._on_exit is not None:
                        self._on_exit()
                    else:
                        self._exit = True
                    gui.SendMessage(handle, con.WM_CANCELMODE, 0, 0)
                return 0
            if message == con.WM_SIZE and handle == self._panel and self._editor:
                self._layout()
                return 0
            if message == con.WM_GETMINMAXINFO and handle == self._panel:
                info = ctypes.cast(lparam, ctypes.POINTER(_MinMaxInfo)).contents
                info.min_track.x, info.min_track.y = self._minimum_size(self._work_area())
                return 0
            if message == con.WM_SETFOCUS and handle == self._panel and self._editor:
                gui.SetFocus(self._editor)
                return 0
            if message == 0x02E0 and handle == self._panel:  # WM_DPICHANGED
                rect = ctypes.cast(lparam, ctypes.POINTER(wintypes.RECT)).contents
                gui.SetWindowPos(
                    handle,
                    0,
                    rect.left,
                    rect.top,
                    rect.right - rect.left,
                    rect.bottom - rect.top,
                    con.SWP_NOZORDER | con.SWP_NOACTIVATE,
                )
                self._set_font()
                self._layout()
                return 0
            if message == con.WM_MOUSEACTIVATE and handle == self._panel:
                if gui.GetForegroundWindow() != handle:
                    return con.MA_NOACTIVATE
            if message in (con.WM_CTLCOLORSTATIC, con.WM_CTLCOLOREDIT):
                gui.SetTextColor(wparam, self._api.RGB(238, 239, 241))
                gui.SetBkColor(wparam, self._api.RGB(23, 24, 27))
                return self._background
            if message == con.WM_DRAWITEM:
                self._paint_button(lparam)
                return 1
            if message == con.WM_COMMAND and (wparam >> 16) == con.BN_CLICKED:
                self._button(wparam & 0xFFFF)
                return 0
            if message == 0x0232 and handle == self._panel:  # WM_EXITSIZEMOVE
                self._snap_edge()
                return 0
        except Exception as error:
            self._error = error
            self.controller.set_interface_ready(
                False, f"Guidance interface failed: {type(error).__name__}"
            )
            self._exit = True
            self._cancel_modal()
            return 0
        return gui.DefWindowProc(handle, message, wparam, lparam)

    def _set_font(self):
        gui, con = self._gui, self._con
        get_dpi = getattr(self._user32, "GetDpiForWindow", None)
        if get_dpi is not None:
            get_dpi.argtypes = [wintypes.HWND]
            get_dpi.restype = wintypes.UINT
            self._scale = (get_dpi(self._panel) or 96) / 96
        description = gui.LOGFONT()
        description.lfFaceName = "Segoe UI"
        description.lfHeight = -round(16 * self._scale)
        description.lfWeight = 400
        description.lfQuality = con.CLEARTYPE_QUALITY
        font = gui.CreateFontIndirect(description)
        for handle in (self._editor, self._status, *self._buttons.values()):
            if handle:
                gui.SendMessage(handle, con.WM_SETFONT, font, True)
        old, self._font = self._font, font
        if old:
            gui.DeleteObject(old)

    def _layout(self):
        gui = self._gui
        _, _, width, height = gui.GetClientRect(self._panel)
        preferred_pad = round(14 * self._scale)
        button_height = round(32 * self._scale)
        line_height = round(24 * self._scale)
        editor_minimum = line_height
        status_lines = 2
        if height < button_height + 2 * line_height + editor_minimum:
            line_height = round(20 * self._scale)
            editor_minimum = line_height
            button_height = round(24 * self._scale)
        if height < button_height + 2 * line_height + editor_minimum:
            status_lines = 1
            button_height = line_height
        if height < button_height + line_height + editor_minimum:
            status_lines = 0
        button_height = min(button_height, max(1, height))
        status_height = status_lines * line_height
        pad = max(
            0, min(preferred_pad, (height - button_height - status_height - editor_minimum) // 4)
        )
        button_y = max(0, height - pad - button_height)
        status_y = max(pad, button_y - pad - status_height)
        body_height = max(1, status_y - 2 * pad)
        self._status_lines = status_lines
        self._compact_status = width - 2 * pad < round(320 * self._scale) or status_lines < 2
        gui.MoveWindow(self._editor, pad, pad, max(40, width - 2 * pad), body_height, True)
        gui.MoveWindow(
            self._status,
            pad,
            status_y,
            max(40, width - 2 * pad),
            status_height,
            True,
        )
        gap = min(round(8 * self._scale), pad)
        button_width = max(30, (width - 2 * pad - 4 * gap) // 5)
        y = button_y
        for index, handle in enumerate(self._buttons.values()):
            gui.MoveWindow(
                handle,
                pad + index * (button_width + gap),
                y,
                button_width,
                button_height,
                True,
            )

    def _paint_button(self, pointer):
        gui, con, api = self._gui, self._con, self._api
        item = ctypes.cast(pointer, ctypes.POINTER(_DrawItem)).contents
        rectangle = item.rect
        pressed = bool(item.state & 1)
        brush = gui.CreateSolidBrush(api.RGB(*(34, 35, 39) if pressed else (47, 49, 54)))
        pen = gui.CreatePen(con.PS_SOLID, 1, api.RGB(86, 89, 96))
        old_brush, old_pen = gui.SelectObject(item.dc, brush), gui.SelectObject(item.dc, pen)
        old_font = gui.SelectObject(item.dc, self._font) if self._font else None
        try:
            gui.FillRect(
                item.dc,
                (rectangle.left, rectangle.top, rectangle.right, rectangle.bottom),
                self._background,
            )
            gui.RoundRect(
                item.dc, rectangle.left, rectangle.top, rectangle.right, rectangle.bottom, 12, 12
            )
            gui.SetBkMode(item.dc, con.TRANSPARENT)
            gui.SetTextColor(item.dc, api.RGB(241, 242, 245))
            gui.DrawText(
                item.dc,
                gui.GetWindowText(item.window),
                -1,
                (rectangle.left, rectangle.top, rectangle.right, rectangle.bottom),
                con.DT_CENTER | con.DT_VCENTER | con.DT_SINGLELINE,
            )
            if item.state & 0x10:
                gui.DrawFocusRect(
                    item.dc,
                    (
                        rectangle.left + 4,
                        rectangle.top + 4,
                        rectangle.right - 4,
                        rectangle.bottom - 4,
                    ),
                )
        finally:
            if old_font is not None:
                gui.SelectObject(item.dc, old_font)
            gui.SelectObject(item.dc, old_brush)
            gui.SelectObject(item.dc, old_pen)
            gui.DeleteObject(brush)
            gui.DeleteObject(pen)

    def _button(self, identifier):
        gui, con = self._gui, self._con
        if identifier == _PIN:
            self._pinned = not self._pinned
            gui.SetWindowText(self._buttons[_PIN], "Unpin" if self._pinned else "Pin")
            gui.SetWindowPos(
                self._panel,
                con.HWND_TOPMOST if self._pinned else con.HWND_NOTOPMOST,
                0,
                0,
                0,
                0,
                con.SWP_NOMOVE | con.SWP_NOSIZE | con.SWP_NOACTIVATE,
            )
        elif identifier in {_TOP, _BOTTOM}:
            self._dock("top" if identifier == _TOP else "bottom")
        elif identifier == _CLEAR:
            self.session.clear_local()
        elif identifier == _STOP:
            self.controller.stop("Stopped from the instruction window.")

    def _work_area(self):
        monitor = self._api.MonitorFromWindow(self._panel, 2)
        return self._api.GetMonitorInfo(monitor)["Work"]

    def _minimum_size(self, work):
        margin = round(12 * self._scale)
        return (
            max(1, min(round(460 * self._scale), work[2] - work[0] - 2 * margin)),
            max(1, min(round(240 * self._scale), work[3] - work[1] - 2 * margin)),
        )

    def _dock(self, edge):
        gui, con = self._gui, self._con
        left, top, right, bottom = self._work_area()
        x, y, old_right, old_bottom = gui.GetWindowRect(self._panel)
        margin = round(12 * self._scale)
        minimum_width, minimum_height = self._minimum_size((left, top, right, bottom))
        width = min(max(old_right - x, minimum_width), right - left - margin * 2)
        height = min(max(old_bottom - y, minimum_height), bottom - top - margin * 2)
        x = max(left + margin, min(x, right - width - margin))
        y = top + margin if edge == "top" else bottom - height - margin
        gui.SetWindowPos(self._panel, 0, x, y, width, height, con.SWP_NOZORDER | con.SWP_NOACTIVATE)

    def _snap_edge(self):
        _, top, _, bottom = self._work_area()
        _, y, _, lower = self._gui.GetWindowRect(self._panel)
        if abs(y - top) <= 28 * self._scale:
            self._dock("top")
        elif abs(bottom - lower) <= 28 * self._scale:
            self._dock("bottom")

    def _drain_requests(self):
        gui, con = self._gui, self._con
        while not self._requests.empty():
            request = self._requests.get_nowait()
            request.error = RuntimeError("The guidance command did not complete.")
            try:
                if request.cancelled.is_set():
                    raise RuntimeError("The guidance request was cancelled.")
                if request.generation is not None:
                    state = self.controller.snapshot()
                    if not state.armed or state.generation != request.generation:
                        raise RuntimeError("The presentation request was revoked.")
                if request.command == "close":
                    self._exit = True
                    self._cancel_modal()
                elif request.command == "show":
                    if self._hide_count:
                        raise RuntimeError(
                            "A capture is in progress; show the transcript afterward."
                        )
                    if request.argument == "back" and self._pinned:
                        raise RuntimeError("The transcript is pinned by the user.")
                    if not self._shown or request.argument == "front":
                        gui.ShowWindow(self._panel, con.SW_SHOWNOACTIVATE)
                    if request.argument != "unchanged":
                        target = (
                            con.HWND_BOTTOM
                            if request.argument == "back"
                            else con.HWND_TOPMOST
                            if self._pinned
                            else con.HWND_TOP
                        )
                        gui.SetWindowPos(
                            self._panel,
                            target,
                            0,
                            0,
                            0,
                            0,
                            con.SWP_NOMOVE | con.SWP_NOSIZE | con.SWP_NOACTIVATE,
                        )
                    self._shown = True
                elif request.command == "hide":
                    if self._hide_count == 0:
                        self._restore_panel = (
                            bool(gui.IsWindowVisible(self._panel)),
                            bool(gui.IsIconic(self._panel)),
                        )
                        try:
                            gui.ShowWindow(self._panel, con.SW_HIDE)
                            gui.ShowWindow(self._canvas, con.SW_HIDE)
                            if self._dwm.DwmFlush() < 0:
                                raise RuntimeError(
                                    "Could not flush guidance overlays before capture."
                                )
                            if request.cancelled.is_set():
                                raise RuntimeError("The guidance capture request was cancelled.")
                        except OSError, RuntimeError, self._native_error:
                            self._restore_panel_visibility()
                            self._last_scene = None
                            raise
                    self._hide_count += 1
                elif request.command == "restore":
                    if self._hide_count <= 0:
                        raise RuntimeError("Unbalanced guidance capture guard.")
                    self._hide_count -= 1
                    if self._hide_count == 0:
                        self._restore_panel_visibility()
                        self._last_scene = None
                else:
                    raise ValueError("Unknown guidance UI command.")
                request.error = None
            except (OSError, RuntimeError, ValueError, self._native_error) as error:
                request.error = error
            finally:
                with request.lock:
                    if request.cancelled.is_set() and request.error is None:
                        if request.command == "hide":
                            self._hide_count -= 1
                            if self._hide_count == 0:
                                self._restore_panel_visibility()
                            self._last_scene = None
                        request.error = RuntimeError("The guidance request was cancelled.")
                    request.done.set()

    def _restore_panel_visibility(self):
        visible, iconic = self._restore_panel
        if visible:
            self._gui.ShowWindow(
                self._panel,
                self._con.SW_SHOWMINNOACTIVE if iconic else self._con.SW_SHOWNOACTIVATE,
            )

    def _cancel_modal(self):
        if self._panel:
            self._gui.SendMessage(self._panel, self._con.WM_CANCELMODE, 0, 0)

    @staticmethod
    def _scene_bounds(
        snapshot: TeachingSnapshot, desktop: Rect, *, now: float | None = None
    ) -> Rect | None:
        from desktop_mcp.teaching_render import validate_scene

        return validate_scene(snapshot, now=time.monotonic() if now is None else now, clip=desktop)

    def _refresh(self):
        from desktop_mcp.layers import upload_rgba
        from desktop_mcp.teaching_render import SceneTooLarge, render_marks

        gui, con = self._gui, self._con
        control = self.controller.snapshot()
        snapshot = self.session.snapshot()
        entries = tuple((entry.sequence, entry.title, entry.text) for entry in snapshot.entries)
        if entries != self._last_text:
            text = "\r\n\r\n".join(f"{entry.title}\r\n{entry.text}" for entry in snapshot.entries)
            gui.SetWindowText(self._editor, text)
            length = len(text.encode("utf-16-le")) // 2
            gui.SendMessage(self._editor, con.EM_SETSEL, length, length)
            gui.SendMessage(self._editor, con.EM_SCROLLCARET, 0, 0)
            self._last_text = entries
        if self._compact_status:
            if snapshot.waiting is not None:
                progress = f"{snapshot.waiting.dwell_progress:.0%}"
                status = (
                    f"Ctrl+Shift+H | {progress}"
                    if self._status_lines < 2
                    else f"Ctrl+Shift+H stops\r\n{control.mode.title()}: cursor {progress}"
                )
            else:
                status = (
                    "Ctrl+Shift+H stops"
                    if self._status_lines < 2
                    else f"{control.mode.title()} | {control.state.title()}\r\nCtrl+Shift+H stops"
                )
        else:
            status = f"{control.mode.title()} | {control.state.title()} | Ctrl+Shift+H stops"
            if snapshot.waiting is not None:
                status += f"\r\nWaiting for your cursor ({snapshot.waiting.dwell_progress:.0%})"
        if gui.GetWindowText(self._status) != status:
            gui.SetWindowText(self._status, status)
        if self._hide_count or not control.armed:
            gui.ShowWindow(self._canvas, con.SW_HIDE)
            self._last_scene = None
            return
        left, top = self._api.GetSystemMetrics(76), self._api.GetSystemMetrics(77)
        desktop = (
            left,
            top,
            left + self._api.GetSystemMetrics(78),
            top + self._api.GetSystemMetrics(79),
        )
        now = time.monotonic()
        try:
            bounds = self._scene_bounds(snapshot, desktop, now=now)
        except SceneTooLarge:
            gui.ShowWindow(self._canvas, con.SW_HIDE)
            gui.SetWindowText(self._status, "Guidance scene too large. Erase older marks.")
            self._last_scene = None
            return
        if bounds is None:
            gui.ShowWindow(self._canvas, con.SW_HIDE)
            self._last_scene = None
            return
        animated = (
            any(mark.kind == "laser" for mark in snapshot.marks) or snapshot.waiting is not None
        )
        signature = (snapshot.revision, bounds, round(now * 30) if animated else 0)
        if signature != self._last_scene:
            with render_marks(snapshot, bounds, now=now) as image:
                upload_rgba(self._canvas, (bounds[0], bounds[1]), image)
            gui.SetWindowPos(
                self._canvas,
                con.HWND_TOPMOST,
                0,
                0,
                0,
                0,
                con.SWP_NOMOVE | con.SWP_NOSIZE | con.SWP_NOACTIVATE | con.SWP_SHOWWINDOW,
            )
            self._last_scene = signature
