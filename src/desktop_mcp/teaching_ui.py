"""A compact native conversation dock and separate click-through annotation layer."""

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
from desktop_mcp.transcript_layout import (
    BOTTOM as _BOTTOM,
    CLEAR as _CLEAR,
    COMPACT_SIZE,
    COMPOSER,
    COMPOSER_LABEL,
    EXPAND as _EXPAND,
    FONT_DIP,
    HISTORY,
    HISTORY_LABEL,
    LATEST as _LATEST,
    PIN as _PIN,
    SEND as _SEND,
    STATUS,
    STOP as _STOP,
    TASKBAR as _TASKBAR,
    TOP as _TOP,
    Dock,
    fit_window,
    layout_client,
    minimum_client_height,
    preferred_size,
    usable_area,
)

if TYPE_CHECKING:
    from desktop_mcp.runtime import Controller
    from desktop_mcp.teaching import TeachingSession, TeachingSnapshot

_COMMAND = 0x8000 + 73
_EMPTY_HISTORY = "Your messages and replies appear here. Ask Copilot to listen with TranscriptRead."


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


@dataclass(frozen=True)
class _EditView:
    anchor: int
    selection: tuple[int, int]
    following: bool = False


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
        self._composer = 0
        self._send = 0
        self._status = 0
        self._history_label = 0
        self._composer_label = 0
        self._buttons: dict[int, int] = {}
        self._child_visibility: dict[int, bool] = {}
        self._hide_count = 0
        self._restore_panel: tuple[bool, bool] = (False, False)
        self._shown = False
        self._minimized = False
        self._pinned = False
        self._last_text: tuple = ()
        self._history_offsets: dict[int, tuple[int, int]] = {}
        self._history_length = 0
        self._history_unread = False
        self._history_view_cache: tuple[_EditView, _EditView] | None = None
        self._last_scene: tuple | None = None
        self._exit = False
        self._font = 0
        self._background = 0
        self._scale = 1.0
        self._dpi_scale = 1.0
        self._font_height = FONT_DIP
        self._compact = True
        self._dock_edge: Dock = "bottom"
        self._mode_sizes: dict[bool, tuple[float, float]] = {}
        self._placement_width: int | None = None
        self._layout_info: dict[str, object] = {"compact": True, "dock": "bottom", "bounds": None}
        self._status_width = 0
        self._last_status: str | None = None
        self._status_key: tuple | None = None
        self._message_error: str | None = None

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
        return tuple(self.window_roles())

    def window_roles(self) -> dict[int, str]:
        """Identify every owned HWND without inspecting chat, drafts or window text."""
        roles = (
            (self._panel, "transcript"),
            (self._canvas, "annotation-overlay"),
            (self._editor, "transcript-history"),
            (self._composer, "transcript-composer"),
            (self._send, "transcript-send"),
            (self._status, "transcript-controls"),
            (self._history_label, "transcript-controls"),
            (self._composer_label, "transcript-controls"),
            *((handle, "transcript-controls") for handle in tuple(self._buttons.values())),
        )
        return {handle: role for handle, role in roles if handle}

    def layout_status(self) -> dict[str, object]:
        """Return content-free layout metadata; bounds and font height are physical pixels."""
        return dict(self._layout_info)

    def _children(self) -> dict[int, int]:
        return {
            HISTORY: self._editor,
            STATUS: self._status,
            COMPOSER: self._composer,
            _SEND: self._send,
            HISTORY_LABEL: self._history_label,
            COMPOSER_LABEL: self._composer_label,
            **self._buttons,
        }

    def show(self, stacking: Literal["unchanged", "front", "back"] = "unchanged") -> None:
        if stacking not in {"unchanged", "front", "back"}:
            raise ValueError("Unknown transcript stacking action.")
        self.session.conversation.ensure_open()
        self._request("show", stacking)

    @property
    def enabled(self) -> bool:
        return self._shown and not self._finished.is_set()

    @property
    def visible(self) -> bool:
        return self.enabled and not self._minimized and not self._hide_count

    def set_visible(self, visible: bool) -> None:
        self.session.conversation.ensure_open()
        self._request("visibility", "on" if visible else "off")

    def toggle_local(self) -> None:
        """Post from the control UI without blocking its global stop message loop."""
        self._post("visibility", "off" if self._shown else "on")

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

    def _post(self, command: str, argument: str = "", generation: int | None = None) -> _Request:
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
        return request

    def _request(self, command: str, argument: str = "", generation: int | None = None) -> None:
        request = self._post(command, argument, generation)
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
        self._user32.IsZoomed.argtypes = [wintypes.HWND]
        self._user32.IsZoomed.restype = wintypes.BOOL
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
        self._user32.GetKeyState.argtypes = [ctypes.c_int]
        self._user32.GetKeyState.restype = ctypes.c_short
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
                "Desktop-MCP - Transcript",
                win32con.WS_OVERLAPPEDWINDOW | win32con.WS_CLIPCHILDREN,
                80,
                80,
                *COMPACT_SIZE,
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
            self._create_controls(instance)
            self._set_font()
            self._resize_mode(initial=True)
            self._refresh()
            self._apply_visibility(True)
            if not self._user32.SetTimer(self._panel, 1, 33, None):
                raise ctypes.WinError(ctypes.get_last_error())
            self._ready.set()
            message = wintypes.MSG()
            while not self._exit:
                while self._user32.PeekMessageW(ctypes.byref(message), None, 0, 0, 1):
                    if message.message == win32con.WM_QUIT:
                        self._exit = True
                        break
                    if self._composer_key(message):
                        continue
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
                self._composer = self._send = 0
                self._history_label = self._composer_label = 0
                self._buttons.clear()
                self._child_visibility.clear()
                self._layout_info = {**self._layout_info, "bounds": None}
                self._font = self._background = 0

    def _create_controls(self, instance: int) -> None:
        gui, con = self._gui, self._con
        child = con.WS_CHILD | con.WS_VISIBLE

        def create(identifier: int, kind: str, text: str, style: int, extra: int = 0) -> int:
            return gui.CreateWindowEx(
                extra,
                kind,
                text,
                child | style,
                0,
                0,
                1,
                1,
                self._panel,
                identifier,
                instance,
                None,
            )

        label_style = con.SS_NOPREFIX | con.SS_CENTERIMAGE | con.SS_ENDELLIPSIS
        self._history_label = create(HISTORY_LABEL, "STATIC", "Conversation", label_style)
        self._editor = create(
            HISTORY,
            "EDIT",
            _EMPTY_HISTORY,
            con.WS_VSCROLL
            | con.WS_TABSTOP
            | con.ES_MULTILINE
            | con.ES_READONLY
            | con.ES_AUTOVSCROLL
            | con.ES_NOHIDESEL,
        )
        self._composer_label = create(
            COMPOSER_LABEL, "STATIC", "Message · Enter sends · Shift+Enter adds a line", label_style
        )
        self._composer = create(
            COMPOSER,
            "EDIT",
            "",
            con.WS_VSCROLL
            | con.WS_TABSTOP
            | con.ES_MULTILINE
            | con.ES_AUTOVSCROLL
            | con.ES_WANTRETURN,
            con.WS_EX_CLIENTEDGE,
        )
        gui.SendMessage(self._composer, con.EM_SETLIMITTEXT, 16_000, 0)
        button_style = con.WS_TABSTOP | con.BS_OWNERDRAW
        self._send = create(_SEND, "BUTTON", "Send", button_style)
        self._status = create(STATUS, "STATIC", "", label_style)
        for identifier, label in (
            (_PIN, "Pin"),
            (_TOP, "Top"),
            (_BOTTOM, "Bottom"),
            (_TASKBAR, "Taskbar edge"),
            (_CLEAR, "Clear ink"),
            (_EXPAND, "Expand"),
            (_STOP, "Stop"),
            (_LATEST, "Latest"),
        ):
            self._buttons[identifier] = create(identifier, "BUTTON", label, button_style)
        self._child_visibility = {identifier: True for identifier in self._children()}

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
                self._minimized = wparam == con.SIZE_MINIMIZED
                if not self._minimized:
                    self._layout()
                return 0
            if message == con.WM_GETMINMAXINFO and handle == self._panel:
                info = ctypes.cast(lparam, ctypes.POINTER(_MinMaxInfo)).contents
                info.min_track.x, info.min_track.y = self._minimum_size(
                    self._dock_area(), self._placement_width
                )
                return 0
            if message == con.WM_SETFOCUS and handle == self._panel and self._editor:
                gui.SetFocus(self._composer or self._editor)
                return 0
            if message == 0x02E0 and handle == self._panel:  # WM_DPICHANGED
                rect = ctypes.cast(lparam, ctypes.POINTER(wintypes.RECT)).contents
                self._dpi_scale = ((wparam & 0xFFFF) or 96) / 96
                self._place_panel((rect.left, rect.top, rect.right, rect.bottom))
                self._fit_current()
                return 0
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
                self._read_dpi()
                self._snap_edge()
                return 0
            if (
                handle == self._panel
                and self._editor
                and (message == 0x007E or (message == 0x001A and wparam == 0x002F))
            ):  # WM_DISPLAYCHANGE / SPI_SETWORKAREA
                self._read_dpi()
                self._fit_current()
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

    def _read_dpi(self) -> None:
        get_dpi = getattr(self._user32, "GetDpiForWindow", None)
        if get_dpi is not None:
            get_dpi.argtypes = [wintypes.HWND]
            get_dpi.restype = wintypes.UINT
            self._dpi_scale = (get_dpi(self._panel) or 96) / 96

    def _set_font(self) -> None:
        self._read_dpi()
        self._scale = self._dpi_scale
        self._font_height = max(1, round(FONT_DIP * self._scale))
        self._replace_font()

    def _replace_font(self):
        gui, con = self._gui, self._con
        description = gui.LOGFONT()
        description.lfFaceName = "Segoe UI"
        description.lfHeight = -self._font_height
        description.lfWeight = 400
        description.lfQuality = con.CLEARTYPE_QUALITY
        font = gui.CreateFontIndirect(description)
        for handle in self._children().values():
            if handle:
                gui.SendMessage(handle, con.WM_SETFONT, font, True)
        old, self._font = self._font, font
        if old:
            gui.DeleteObject(old)

    def _layout(self):
        gui = self._gui
        _, _, width, height = gui.GetClientRect(self._panel)
        if width <= 0 or height <= 0:
            return
        composing = bool(self._composer and self._composition_active())
        views = {
            handle: self._read_view(handle, history=handle == self._editor)
            for handle in (self._editor, self._composer)
            if handle and not (handle == self._composer and composing)
        }
        layout = layout_client(width, height, self._dpi_scale, compact=self._compact)
        self._scale = layout.scale
        if self._font_height != layout.font_height:
            self._font_height = layout.font_height
            if self._font:
                self._replace_font()
        for identifier, handle in self._children().items():
            if not handle:
                continue
            visible = identifier in layout.controls
            if visible:
                left, top, right, bottom = layout.controls[identifier]
                gui.MoveWindow(handle, left, top, right - left, bottom - top, True)
            else:
                gui.MoveWindow(handle, 0, 0, 1, 1, False)
            if self._child_visibility.get(identifier, True) != visible:
                gui.ShowWindow(
                    handle, self._con.SW_SHOWNOACTIVATE if visible else self._con.SW_HIDE
                )
            self._child_visibility[identifier] = visible
        for handle in (self._editor, self._composer):
            if handle:
                margin = max(1, round(4 * self._scale))
                gui.SendMessage(handle, self._con.EM_SETMARGINS, 3, margin | (margin << 16))
        for handle, view in views.items():
            self._restore_view(handle, view)
        if self._composer_label and COMPOSER_LABEL in layout.controls:
            left, _, right, _ = layout.controls[COMPOSER_LABEL]
            label = self._fit_text(
                (
                    "Message · Enter sends · Shift+Enter adds a line",
                    "Message · Enter sends · Shift+Enter: newline",
                    "Message · Enter sends",
                    "Message",
                ),
                right - left,
            )
            gui.SetWindowText(self._composer_label, label)
        left, _, right, _ = layout.controls[STATUS]
        self._status_width = right - left
        self._status_key = None
        self._layout_info = {
            "compact": self._compact,
            "dock": self._dock_edge,
            "bounds": tuple(gui.GetWindowRect(self._panel)),
            "dpi": round(96 * self._dpi_scale),
            "font_height": self._font_height,
            "split": layout.split,
        }

    def _paint_button(self, pointer):
        gui, con, api = self._gui, self._con, self._api
        item = ctypes.cast(pointer, ctypes.POINTER(_DrawItem)).contents
        rectangle = item.rect
        pressed = bool(item.state & 1)
        active = (
            (item.ctl_id == _PIN and self._pinned)
            or (item.ctl_id == _TOP and self._dock_edge == "top")
            or (item.ctl_id == _BOTTOM and self._dock_edge == "bottom")
            or (item.ctl_id == _TASKBAR and self._dock_edge == "taskbar-edge")
            or (item.ctl_id == _LATEST and self._history_unread)
        )
        color = (47, 49, 54)
        if item.ctl_id == _SEND:
            color = (40, 73, 65)
        elif item.ctl_id == _STOP:
            color = (77, 40, 43)
        elif active:
            color = (62, 69, 83)
        brush = gui.CreateSolidBrush(api.RGB(*(34, 35, 39) if pressed else color))
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
                item.dc,
                rectangle.left,
                rectangle.top,
                rectangle.right,
                rectangle.bottom,
                max(2, round(10 * self._scale)),
                max(2, round(10 * self._scale)),
            )
            gui.SetBkMode(item.dc, con.TRANSPARENT)
            gui.SetTextColor(item.dc, api.RGB(241, 242, 245))
            gui.DrawText(
                item.dc,
                gui.GetWindowText(item.window),
                -1,
                (rectangle.left, rectangle.top, rectangle.right, rectangle.bottom),
                con.DT_CENTER | con.DT_VCENTER | con.DT_SINGLELINE | con.DT_NOPREFIX,
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
        if identifier == _SEND:
            self._send_user()
            if gui.GetForegroundWindow() == self._panel:
                gui.SetFocus(self._composer)
        elif identifier == _PIN:
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
        elif identifier in {_TOP, _BOTTOM, _TASKBAR}:
            self._dock({_TOP: "top", _BOTTOM: "bottom", _TASKBAR: "taskbar-edge"}[identifier])
        elif identifier == _EXPAND:
            if self._user32.IsZoomed(self._panel):
                gui.ShowWindow(self._panel, con.SW_RESTORE)
            rect = gui.GetWindowRect(self._panel)
            self._mode_sizes[self._compact] = (
                (rect[2] - rect[0]) / self._dpi_scale,
                (rect[3] - rect[1]) / self._dpi_scale,
            )
            self._compact = not self._compact
            gui.SetWindowText(self._buttons[_EXPAND], "Expand" if self._compact else "Compact")
            self._resize_mode()
        elif identifier == _LATEST:
            self._scroll_latest()
        elif identifier == _CLEAR:
            self.session.clear_local()
        elif identifier == _STOP:
            self.controller.stop("Stopped from the instruction window.")

    def _composition_active(self) -> bool:
        imm = ctypes.WinDLL("imm32", use_last_error=True)
        imm.ImmGetContext.argtypes, imm.ImmGetContext.restype = [wintypes.HWND], wintypes.HANDLE
        imm.ImmReleaseContext.argtypes = [wintypes.HWND, wintypes.HANDLE]
        imm.ImmReleaseContext.restype = wintypes.BOOL
        imm.ImmGetCompositionStringW.argtypes = [
            wintypes.HANDLE,
            wintypes.DWORD,
            ctypes.c_void_p,
            wintypes.DWORD,
        ]
        imm.ImmGetCompositionStringW.restype = ctypes.c_long
        context = imm.ImmGetContext(self._composer)
        if not context:
            return False
        try:
            return imm.ImmGetCompositionStringW(context, 8, None, 0) > 0
        finally:
            imm.ImmReleaseContext(self._composer, context)

    def _composer_key(self, message) -> bool:
        if (
            not self._composer
            or message.hWnd != self._composer
            or message.message != self._con.WM_KEYDOWN
            or message.wParam != self._con.VK_RETURN
        ):
            return False
        if self._user32.GetKeyState(self._con.VK_SHIFT) & 0x8000 or self._composition_active():
            return False
        if not message.lParam & (1 << 30):
            self._send_user()
        return True

    def _send_user(self) -> None:
        draft = self._gui.GetWindowText(self._composer)
        try:
            self.session.conversation.send_user(draft)
        except (ValueError, RuntimeError) as error:
            self._message_error = str(error)
        else:
            self._message_error = None
            self._gui.SetWindowText(self._composer, "")
        self._refresh()

    def _sample_view(self, handle: int, *, history: bool = False) -> _EditView:
        gui, con = self._gui, self._con
        start, end = wintypes.DWORD(), wintypes.DWORD()
        # The packed EM_GETSEL result truncates offsets above 65535 UTF-16 code units.
        gui.SendMessage(handle, con.EM_GETSEL, ctypes.addressof(start), ctypes.addressof(end))
        first_line = gui.SendMessage(handle, con.EM_GETFIRSTVISIBLELINE, 0, 0)
        anchor = max(0, gui.SendMessage(handle, con.EM_LINEINDEX, first_line, 0))
        following = False
        if history:
            _, _, maximum, page, position, _ = gui.GetScrollInfo(
                handle, con.SB_VERT, con.SIF_RANGE | con.SIF_PAGE | con.SIF_POS
            )
            following = not self._last_text or (
                start.value == end.value and position >= maximum - max(1, page) + 1
            )
        return _EditView(anchor, (start.value, end.value), following)

    def _read_view(self, handle: int, *, history: bool = False) -> _EditView:
        actual = self._sample_view(handle, history=history)
        if history and self._history_view_cache is not None:
            desired, previous = self._history_view_cache
            if (actual.anchor, actual.selection) == (previous.anchor, previous.selection):
                # A larger viewport can clamp the scroll range. That is not a user scroll.
                return desired
        return actual

    def _restore_view(self, handle: int, view: _EditView) -> None:
        if handle == self._editor and view.following:
            self._scroll_latest()
            return
        gui, con = self._gui, self._con
        gui.SendMessage(handle, con.EM_SETSEL, *view.selection)
        line = gui.SendMessage(handle, con.EM_LINEFROMCHAR, view.anchor, 0)
        current = gui.SendMessage(handle, con.EM_GETFIRSTVISIBLELINE, 0, 0)
        gui.SendMessage(handle, con.EM_LINESCROLL, 0, line - current)
        if handle == self._editor:
            self._history_view_cache = view, self._sample_view(handle, history=True)

    def _set_unread(self, unread: bool) -> None:
        if unread != self._history_unread:
            self._history_unread = unread
            if handle := self._buttons.get(_LATEST):
                self._gui.SetWindowText(handle, "Latest *" if unread else "Latest")

    def _scroll_latest(self) -> None:
        self._gui.SendMessage(
            self._editor, self._con.EM_SETSEL, self._history_length, self._history_length
        )
        self._gui.SendMessage(self._editor, self._con.EM_SCROLLCARET, 0, 0)
        actual = self._sample_view(self._editor, history=True)
        self._history_view_cache = _EditView(actual.anchor, actual.selection, True), actual
        self._set_unread(False)

    @staticmethod
    def _history_document(entries: tuple) -> tuple[str, dict[int, tuple[int, int]], int]:
        chunks = []
        offsets = {}
        position = 0
        for index, (sequence, title, text, role) in enumerate(entries):
            label = "You" if role == "user" else f"Assistant · {title}"
            chunk = f"{label}: {text}".replace("\r\n", "\n").replace("\r", "\n")
            chunk = chunk.replace("\n", "\r\n")
            if index < len(entries) - 1:
                chunk += "\r\n\r\n"
            length = len(chunk.encode("utf-16-le")) // 2
            offsets[sequence] = position, length
            position += length
            chunks.append(chunk)
        return "".join(chunks), offsets, position

    @staticmethod
    def _remap_offset(
        position: int, old: dict[int, tuple[int, int]], new: dict[int, tuple[int, int]]
    ) -> int:
        for sequence, (start, length) in reversed(old.items()):
            if position >= start:
                if sequence not in new:
                    return 0
                new_start, new_length = new[sequence]
                return new_start + min(position - start, length, new_length)
        return 0

    def _update_history(self, entries: tuple) -> None:
        if entries == self._last_text:
            if self._history_unread and self._read_view(self._editor, history=True).following:
                self._set_unread(False)
            return
        view = self._read_view(self._editor, history=True)
        text, offsets, length = self._history_document(entries)
        if not view.following:
            view = _EditView(
                self._remap_offset(view.anchor, self._history_offsets, offsets),
                tuple(
                    self._remap_offset(p, self._history_offsets, offsets) for p in view.selection
                ),
            )
        self._gui.SetWindowText(self._editor, text or _EMPTY_HISTORY)
        self._last_text = entries
        self._history_offsets, self._history_length = offsets, length
        self._restore_view(self._editor, view)
        self._set_unread(bool(entries) and not view.following)

    def _text_width(self, text: str) -> int:
        gui = self._gui
        dc = gui.GetDC(self._panel)
        with ExitStack() as cleanup:
            cleanup.callback(gui.ReleaseDC, self._panel, dc)
            if self._font:
                old = gui.SelectObject(dc, self._font)
                cleanup.callback(gui.SelectObject, dc, old)
            return gui.GetTextExtentPoint32(dc, text)[0]

    def _fit_text(self, candidates: tuple[str, ...], width: int) -> str:
        if width <= 0:
            return candidates[0]
        for text in candidates:
            if self._text_width(text) <= width:
                return text
        text = candidates[-1]
        while text and self._text_width(text + "…") > width:
            text = text[:-1]
        return text + "…" if text else ""

    def _set_status(self, text: str) -> None:
        if text != self._last_status:
            self._gui.SetWindowText(self._status, text)
            self._last_status = text
        self._status_key = None

    def _refresh_status(self, control, snapshot: TeachingSnapshot) -> None:
        chat = self.session.conversation.status()
        pending = chat["pending_messages"]
        if self._message_error:
            full, short = f"Not sent: {self._message_error} · draft kept", "Not sent · draft kept"
            terse = short
        elif chat["awaiting_reply"]:
            full, short = "Awaiting the agent's reply", "awaiting reply"
            terse = "reply pending"
        elif chat["listener_waiting"]:
            full, short = "Agent listening", "listening"
            terse = short
        elif pending:
            full = f"{pending} queued"
            short = terse = full
            if not chat["listener_connected"]:
                full += " · ask Copilot to listen"
                short += " · no listener"
        elif chat["listener_connected"]:
            full, short = "Agent connected · not waiting", "not listening"
            terse = short
        else:
            full, short = "No listener · ask Copilot to listen", "no listener"
            terse = short
        if pending and (chat["awaiting_reply"] or chat["listener_waiting"]):
            full += f" · {pending} pending"
            short += f" · {pending} pending"
        state = "ready" if control.armed else "paused"
        progress = snapshot.waiting.dwell_progress if snapshot.waiting is not None else None
        full_progress = f" · Your cursor: {progress:.0%}" if progress is not None else ""
        short_progress = f" · Cursor {progress:.0%}" if progress is not None else ""
        candidates = (
            f"Desktop {state} · {full}{full_progress} · Ctrl+Shift+H stops",
            f"{state.capitalize()} · {short}{short_progress} · Ctrl+Shift+H",
            f"{state.capitalize()} · {terse}{short_progress} · Ctrl+Shift+H",
        )
        key = candidates, self._status_width, self._font_height
        if key != self._status_key:
            self._set_status(self._fit_text(candidates, self._status_width))
            self._status_key = key

    def _apply_visibility(self, visible: bool) -> None:
        self._shown = visible
        self._minimized = False
        if self._hide_count:
            self._restore_panel = (visible, False)
            return
        self._gui.ShowWindow(
            self._panel, self._con.SW_SHOWNOACTIVATE if visible else self._con.SW_HIDE
        )

    def _work_area(self):
        monitor = self._api.MonitorFromWindow(self._panel, 2)
        return self._api.GetMonitorInfo(monitor)["Work"]

    def _monitor_area(self) -> Rect:
        monitor = self._api.MonitorFromWindow(self._panel, 2)
        return self._api.GetMonitorInfo(monitor)["Monitor"]

    def _dock_area(self) -> Rect:
        return self._monitor_area() if self._dock_edge == "taskbar-edge" else self._work_area()

    def _chrome_size(self) -> tuple[int, int]:
        adjust = getattr(self._user32, "AdjustWindowRectExForDpi", None)
        if adjust is not None:
            adjust.argtypes = [
                ctypes.POINTER(wintypes.RECT),
                wintypes.DWORD,
                wintypes.BOOL,
                wintypes.DWORD,
                wintypes.UINT,
            ]
            adjust.restype = wintypes.BOOL
            rect = wintypes.RECT()
            if not adjust(
                ctypes.byref(rect),
                self._con.WS_OVERLAPPEDWINDOW,
                False,
                self._con.WS_EX_APPWINDOW | self._con.WS_EX_CONTROLPARENT,
                round(96 * self._dpi_scale),
            ):
                raise ctypes.WinError(ctypes.get_last_error())
            return rect.right - rect.left, rect.bottom - rect.top
        left, top, right, bottom = self._gui.GetWindowRect(self._panel)
        _, _, width, height = self._gui.GetClientRect(self._panel)
        return max(0, right - left - width), max(0, bottom - top - height)

    def _minimum_size(self, area: Rect, window_width: int | None = None) -> tuple[int, int]:
        left, top, right, bottom = usable_area(area, self._dpi_scale, self._dock_edge)
        chrome = self._chrome_size()
        if window_width is None:
            rect = self._gui.GetWindowRect(self._panel)
            window_width = rect[2] - rect[0]
        minimum_width = min(right - left, round(360 * self._dpi_scale) + chrome[0])
        width = min(right - left, max(window_width, minimum_width))
        minimum_height = (
            minimum_client_height(max(1, width - chrome[0]), self._dpi_scale, compact=self._compact)
            + chrome[1]
        )
        return minimum_width, min(bottom - top, minimum_height)

    def _place_panel(self, rectangle: Rect) -> None:
        left, top, right, bottom = rectangle
        previous, self._placement_width = self._placement_width, right - left
        try:
            # Windows asks for minima before publishing the new width to GetWindowRect.
            self._gui.SetWindowPos(
                self._panel,
                0,
                left,
                top,
                right - left,
                bottom - top,
                self._con.SWP_NOZORDER | self._con.SWP_NOACTIVATE,
            )
        finally:
            self._placement_width = previous
        self._layout()
        for identifier in (_TOP, _BOTTOM, _TASKBAR):
            if handle := self._buttons.get(identifier):
                self._gui.InvalidateRect(handle, None, True)

    def _fit_current(self) -> None:
        if self._gui.IsIconic(self._panel):
            return
        if self._user32.IsZoomed(self._panel):
            self._layout()
            return
        rectangle = self._gui.GetWindowRect(self._panel)
        area = self._dock_area()
        self._place_panel(
            fit_window(
                rectangle,
                area,
                self._dpi_scale,
                dock=self._dock_edge,
                minimum=self._minimum_size(area, rectangle[2] - rectangle[0]),
            )
        )

    def _resize_mode(self, *, initial: bool = False) -> None:
        area = self._dock_area()
        width, height = preferred_size(
            area, self._dpi_scale, self._chrome_size(), compact=self._compact, dock=self._dock_edge
        )
        current = self._gui.GetWindowRect(self._panel)
        if not initial:
            saved = self._mode_sizes.get(self._compact)
            if saved is not None:
                width, height = (round(value * self._dpi_scale) for value in saved)
            else:
                width = current[2] - current[0]
        x = (area[0] + area[2] - width) // 2 if initial else current[0]
        rectangle = (x, current[1], x + width, current[1] + height)
        self._place_panel(
            fit_window(
                rectangle,
                area,
                self._dpi_scale,
                dock=self._dock_edge,
                minimum=self._minimum_size(area, width),
            )
        )

    def _dock(self, edge: Dock) -> None:
        if edge not in {"top", "bottom", "taskbar-edge"}:
            raise ValueError("Unknown transcript dock.")
        if self._user32.IsZoomed(self._panel):
            self._gui.ShowWindow(self._panel, self._con.SW_RESTORE)
        self._dock_edge = edge
        self._fit_current()

    def _snap_edge(self):
        _, top, _, bottom = self._work_area()
        _, y, _, lower = self._gui.GetWindowRect(self._panel)
        threshold = 28 * self._dpi_scale
        if self._dock_edge == "taskbar-edge" and abs(self._monitor_area()[3] - lower) <= threshold:
            self._dock_edge = "taskbar-edge"
        elif abs(y - top) <= threshold:
            self._dock_edge = "top"
        elif abs(bottom - lower) <= threshold:
            self._dock_edge = "bottom"
        else:
            self._dock_edge = "floating"
        self._fit_current()

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
                elif request.command == "visibility":
                    if self._exit or self.controller.snapshot().state == "closed":
                        raise RuntimeError("The transcript is closing.")
                    if request.argument not in {"on", "off"}:
                        raise ValueError("Visibility must be on or off.")
                    self._apply_visibility(request.argument == "on")
                elif request.command == "show":
                    if self._exit or self.controller.snapshot().state == "closed":
                        raise RuntimeError("The transcript is closing.")
                    if self._hide_count:
                        raise RuntimeError(
                            "A capture is in progress; show the transcript afterward."
                        )
                    if request.argument == "back" and self._pinned:
                        raise RuntimeError("The transcript is pinned by the user.")
                    if not self._shown or self._minimized or request.argument == "front":
                        self._apply_visibility(True)
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
        self._minimized = iconic

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
        entries = tuple(
            (entry.sequence, entry.title, entry.text, entry.role) for entry in snapshot.entries
        )
        self._update_history(entries)
        self._refresh_status(control, snapshot)
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
            self._set_status("Guidance scene too large. Erase older marks.")
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
