"""A compact native conversation dock and separate click-through annotation layer."""

from __future__ import annotations

from contextlib import ExitStack, closing, contextmanager
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
from desktop_mcp.conversation import MAX_TEXT
from desktop_mcp.transcript_chat import BACKGROUND, TEXT_COLOR
from desktop_mcp.transcript_layout import (
    BOTTOM as _BOTTOM,
    CLEAR as _CLEAR,
    COMPACT_SIZE,
    COMPOSER,
    COMPOSER_LABEL,
    COMPOSER_SCROLL,
    EXPAND as _EXPAND,
    FONT_DIP,
    FONT_SIZES,
    HISTORY,
    HISTORY_LABEL,
    HISTORY_SCROLL,
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
from desktop_mcp.transcript_scroll import (
    ScrollState,
    dragged_position,
    thumb_geometry,
    wheel_movement,
)

if TYPE_CHECKING:
    from desktop_mcp.runtime import Controller
    from desktop_mcp.teaching import TeachingSession, TeachingSnapshot
    from desktop_mcp.transcript_chat_native import NativeChatHistory

_COMMAND = 0x8000 + 73
_REFLOW = 0x8000 + 74
_EDIT_SUBCLASS = 1
_FONT_FACES = ("Segoe UI Variable Text", "Segoe UI")
_CLIENT_AREA_ANIMATION = 0x1042
_IDLE_TIMER_MS = 33
_STATE_REFRESH_SECONDS = _IDLE_TIMER_MS / 1000
_STATE_DEADLINE_EPSILON = 0.000001
_ANIMATION_TIMER_MS = 16
_TEXT_SIZE_NAMES = ("Small", "Medium", "Large")
_TEXT_SIZE_KEYS = {0xBB: 1, 0x6B: 1, 0xBD: -1, 0x6D: -1}


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


class _TrackMouseEvent(ctypes.Structure):
    _fields_ = [
        ("size", wintypes.DWORD),
        ("flags", wintypes.DWORD),
        ("window", wintypes.HWND),
        ("hover_time", wintypes.DWORD),
    ]


class _TextMetric(ctypes.Structure):
    _fields_ = (
        [
            (name, wintypes.LONG)
            for name in (
                "height",
                "ascent",
                "descent",
                "internal_leading",
                "external_leading",
                "average_width",
                "maximum_width",
                "weight",
                "overhang",
                "aspect_x",
                "aspect_y",
            )
        ]
        + [(name, wintypes.WCHAR) for name in ("first", "last", "default", "break_char")]
        + [
            (name, wintypes.BYTE)
            for name in ("italic", "underlined", "struck_out", "pitch", "charset")
        ]
    )


@dataclass(frozen=True)
class _ScrollDrag:
    window: int
    fraction: float
    origin: tuple[int, int] | None = None


@dataclass(frozen=True)
class _EditView:
    anchor: int
    selection: tuple[int, int]


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
        self._history_window = 0
        self._history: NativeChatHistory | None = None
        self._composer = 0
        self._send = 0
        self._status = 0
        self._history_label = 0
        self._composer_label = 0
        self._buttons: dict[int, int] = {}
        self._scrollbars: dict[int, int] = {}
        self._scroll_states: dict[int, ScrollState] = {}
        self._scroll_drag: _ScrollDrag | None = None
        self._scroll_hover = 0
        self._wheel_remainders: dict[int, int] = {}
        self._ime_composing = False
        self._edit_callback = self._edit_procedure
        self._line_height = 0
        self._scroll_syncing = False
        self._programmatic_depth = 0
        self._layout_busy = False
        self._layout_hold = 0
        self._layout_posted = False
        self._layout_serial = 0
        self._child_visibility: dict[int, bool] = {}
        self._hide_count = 0
        self._capture_serial = 0
        self._restore_panel: tuple[bool, bool] = (False, False)
        self._shown = False
        self._minimized = False
        self._pinned = False
        self._last_text: tuple = ()
        self._history_unread = False
        self._last_scene: tuple | None = None
        self._scene_snapshot: TeachingSnapshot | None = None
        self._scene_ticket: tuple[int, int] | None = None
        self._scene_animating = False
        self._scene_rendering = False
        self._next_scene_frame = 0.0
        self._chat_animating = False
        self._next_state_refresh = 0.0
        self._timer_interval = 0
        self._timer_running = False
        self._exit = False
        self._font = 0
        self._retired_fonts: list[int] = []
        self._history_font_dirty = False
        self._background = 0
        self._scale = 1.0
        self._dpi_scale = 1.0
        self._font_height = FONT_DIP
        self._text_size_index = 1
        self._text_size_keys_held: set[int] = set()
        self._font_face = _FONT_FACES[0]
        self._font_face_verified = False
        self._motion_enabled = False
        self._compact = True
        self._dock_edge: Dock = "bottom"
        self._mode_sizes: dict[bool, tuple[float, float]] = {}
        self._placement_width: int | None = None
        self._layout_info: dict[str, object] = {
            "compact": True,
            "dock": "bottom",
            "bounds": None,
            "text_size": "Medium",
            "font_dip": FONT_DIP,
        }
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
        scroll_roles = {
            HISTORY_SCROLL: "transcript-history-scrollbar",
            COMPOSER_SCROLL: "transcript-composer-scrollbar",
        }
        roles = (
            (self._panel, "transcript"),
            (self._canvas, "annotation-overlay"),
            (self._history_window, "transcript-history"),
            (self._composer, "transcript-composer"),
            (self._send, "transcript-send"),
            (self._status, "transcript-controls"),
            (self._history_label, "transcript-controls"),
            (self._composer_label, "transcript-controls"),
            *((handle, "transcript-controls") for handle in tuple(self._buttons.values())),
            *(
                (handle, scroll_roles.get(identifier, "transcript-controls"))
                for identifier, handle in tuple(self._scrollbars.items())
            ),
        )
        result = {handle: role for handle, role in roles if handle}
        if self._history is not None:
            result.update(self._history.window_roles())
        return result

    def layout_status(self) -> dict[str, object]:
        """Return content-free layout metadata; bounds and font height are physical pixels."""
        return dict(self._layout_info)

    def _children(self) -> dict[int, int]:
        return {
            HISTORY: self._history_window,
            STATUS: self._status,
            COMPOSER: self._composer,
            _SEND: self._send,
            HISTORY_LABEL: self._history_label,
            COMPOSER_LABEL: self._composer_label,
            **self._buttons,
            **self._scrollbars,
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
        self._user32.BeginDeferWindowPos.argtypes = [ctypes.c_int]
        self._user32.BeginDeferWindowPos.restype = wintypes.HANDLE
        self._user32.DeferWindowPos.argtypes = [
            wintypes.HANDLE,
            wintypes.HWND,
            wintypes.HWND,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            wintypes.UINT,
        ]
        self._user32.DeferWindowPos.restype = wintypes.HANDLE
        self._user32.EndDeferWindowPos.argtypes = [wintypes.HANDLE]
        self._user32.EndDeferWindowPos.restype = wintypes.BOOL
        self._user32.TrackMouseEvent.argtypes = [ctypes.POINTER(_TrackMouseEvent)]
        self._user32.TrackMouseEvent.restype = wintypes.BOOL
        self._user32.SystemParametersInfoW.argtypes = [
            wintypes.UINT,
            wintypes.UINT,
            ctypes.c_void_p,
            wintypes.UINT,
        ]
        self._user32.SystemParametersInfoW.restype = wintypes.BOOL
        self._comctl = ctypes.WinDLL("comctl32", use_last_error=True)
        subclass_proc = ctypes.WINFUNCTYPE(
            ctypes.c_ssize_t,
            wintypes.HWND,
            wintypes.UINT,
            wintypes.WPARAM,
            wintypes.LPARAM,
            ctypes.c_size_t,
            ctypes.c_size_t,
        )
        self._edit_callback = subclass_proc(self._edit_procedure)
        self._comctl.SetWindowSubclass.argtypes = [
            wintypes.HWND,
            subclass_proc,
            ctypes.c_size_t,
            ctypes.c_size_t,
        ]
        self._comctl.SetWindowSubclass.restype = wintypes.BOOL
        self._comctl.RemoveWindowSubclass.argtypes = [
            wintypes.HWND,
            subclass_proc,
            ctypes.c_size_t,
        ]
        self._comctl.RemoveWindowSubclass.restype = wintypes.BOOL
        self._comctl.DefSubclassProc.argtypes = [
            wintypes.HWND,
            wintypes.UINT,
            wintypes.WPARAM,
            wintypes.LPARAM,
        ]
        self._comctl.DefSubclassProc.restype = ctypes.c_ssize_t
        self._gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
        self._gdi32.GetTextMetricsW.argtypes = [wintypes.HDC, ctypes.POINTER(_TextMetric)]
        self._gdi32.GetTextMetricsW.restype = wintypes.BOOL
        self._gdi32.GetTextFaceW.argtypes = [wintypes.HDC, ctypes.c_int, wintypes.LPWSTR]
        self._gdi32.GetTextFaceW.restype = ctypes.c_int
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
            self._background = win32gui.CreateSolidBrush(win32api.RGB(*BACKGROUND))
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
            self._motion_enabled = self._client_animations_enabled()
            self._create_controls(instance)
            self._set_font()
            self._resize_mode(initial=True)
            self._refresh()
            self._apply_visibility(True)
            if not self._user32.SetTimer(self._panel, 1, _IDLE_TIMER_MS, None):
                raise ctypes.WinError(ctypes.get_last_error())
            self._timer_interval = _IDLE_TIMER_MS
            self._timer_running = True
            self._ready.set()
            message = wintypes.MSG()
            while not self._exit:
                while self._user32.PeekMessageW(ctypes.byref(message), None, 0, 0, 1):
                    if message.message == win32con.WM_QUIT:
                        self._exit = True
                        break
                    if self._text_size_key(message) or self._composer_key(message):
                        continue
                    if not self._user32.IsDialogMessageW(self._panel, ctypes.byref(message)):
                        self._user32.TranslateMessage(ctypes.byref(message))
                        self._user32.DispatchMessageW(ctypes.byref(message))
                self._finished.wait(0.004)
        finally:
            try:
                self._exit = True
                with ExitStack() as cleanup:
                    cleanup.callback(self._user32.SetThreadDpiAwarenessContext, previous_dpi)
                    if registered:
                        cleanup.callback(win32gui.UnregisterClass, class_name, instance)
                    elif self._background:
                        cleanup.callback(win32gui.DeleteObject, self._background)
                    if self._font:
                        cleanup.callback(win32gui.DeleteObject, self._font)
                    for font in self._retired_fonts:
                        cleanup.callback(win32gui.DeleteObject, font)
                    for handle in (self._panel, self._canvas):
                        if handle and win32gui.IsWindow(handle):
                            cleanup.callback(win32gui.DestroyWindow, handle)
                    cleanup.callback(self._cancel_scroll_drag)
                    cleanup.callback(self._close_history)
                    if self._panel and win32gui.IsWindow(self._panel):
                        self._user32.KillTimer(self._panel, 1)
                    self._timer_running = False
            finally:
                self._canvas = self._panel = self._history_window = self._status = 0
                self._composer = self._send = 0
                self._history_label = self._composer_label = 0
                self._buttons.clear()
                self._scrollbars.clear()
                self._scroll_states.clear()
                self._wheel_remainders.clear()
                self._ime_composing = False
                self._text_size_keys_held.clear()
                self._child_visibility.clear()
                self._layout_info = {**self._layout_info, "bounds": None}
                self._font = self._background = 0
                self._retired_fonts.clear()

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
        self._history = self._new_history()
        self._history_window = self._history.create(self._panel, instance, HISTORY)
        self._history_font_dirty = True
        scroll_class = gui.GetClassName(self._panel)
        self._scrollbars[HISTORY_SCROLL] = create(
            HISTORY_SCROLL, scroll_class, "Conversation scroll", con.WS_TABSTOP
        )
        self._composer_label = create(
            COMPOSER_LABEL, "STATIC", "Message · Enter sends · Shift+Enter adds a line", label_style
        )
        self._composer = create(
            COMPOSER,
            "EDIT",
            "",
            con.WS_TABSTOP | con.ES_MULTILINE | con.ES_AUTOVSCROLL | con.ES_WANTRETURN,
            con.WS_EX_CLIENTEDGE,
        )
        self._scrollbars[COMPOSER_SCROLL] = create(
            COMPOSER_SCROLL, scroll_class, "Message scroll", con.WS_TABSTOP
        )
        if not self._comctl.SetWindowSubclass(
            self._composer, self._edit_callback, _EDIT_SUBCLASS, 0
        ):
            raise ctypes.WinError(ctypes.get_last_error())
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

    def _new_history(self) -> NativeChatHistory:
        from desktop_mcp.transcript_chat_native import NativeChatHistory

        return NativeChatHistory(
            on_change=self._history_changed,
            on_error=self._history_failed,
        )

    def _close_history(self) -> None:
        history, self._history = self._history, None
        self._history_window = 0
        if history is not None:
            history.close()

    def _history_changed(self) -> None:
        if self._history is None or self._exit:
            return
        self._set_unread(self._history.unread)
        self._chat_animating = self._motion_enabled and self._history.animation_active
        self._sync_scrollbars()
        self._schedule_timer()

    def _history_failed(self, error: Exception) -> None:
        if self._exit:
            return
        self._error = error
        self.controller.set_interface_ready(
            False, f"Transcript history failed: {type(error).__name__}"
        )
        self._exit = True
        self._cancel_modal()

    def _procedure(self, handle, message, wparam, lparam):
        gui, con = self._gui, self._con
        try:
            if handle in self._scrollbars.values():
                return self._scrollbar_procedure(handle, message, wparam, lparam)
            if message == _COMMAND:
                self._drain_requests()
                return 0
            if message == con.WM_CANCELMODE and handle == self._panel:
                self._cancel_scroll_drag()
            if message == _REFLOW:
                self._layout_posted = False
                if not self._exit:
                    self._layout()
                return 0
            if self._exit:
                return gui.DefWindowProc(handle, message, wparam, lparam)
            if message == con.WM_TIMER and handle == self._panel:
                self._drain_requests()
                if not self._exit and not self._layout_busy and not self._programmatic_depth:
                    self._on_timer()
                return 0
            if message == con.WM_CLOSE and handle == self._panel:
                try:
                    self.controller.stop("Desktop-MCP is quitting.")
                finally:
                    if self._on_exit is not None:
                        self._on_exit()
                    else:
                        self._exit = True
                    self._cancel_modal()
                return 0
            if message == con.WM_SIZE and handle == self._panel and self._history_window:
                self._minimized = wparam == con.SIZE_MINIMIZED
                if self._minimized:
                    self._text_size_keys_held.clear()
                    self._cancel_scroll_drag()
                    if self._history is not None:
                        self._history.cancel_interaction()
                        self._history_changed()
                else:
                    self._layout()
                return 0
            if message == con.WM_GETMINMAXINFO and handle == self._panel:
                info = ctypes.cast(lparam, ctypes.POINTER(_MinMaxInfo)).contents
                info.min_track.x, info.min_track.y = self._minimum_size(
                    self._dock_area(), self._placement_width
                )
                return 0
            if message == con.WM_SETFOCUS and handle == self._panel and self._composer:
                gui.SetFocus(self._composer)
                return 0
            if message == con.WM_ACTIVATE and handle == self._panel and (wparam & 0xFFFF) == 0:
                self._text_size_keys_held.clear()
                self._cancel_scroll_drag()
                if self._history is not None:
                    self._history.cancel_interaction()
                    self._history_changed()
            if message == con.WM_ERASEBKGND and handle == self._panel:
                gui.FillRect(wparam, gui.GetClientRect(handle), self._background)
                return 1
            if message == 0x02E0 and handle == self._panel:  # WM_DPICHANGED
                rect = ctypes.cast(lparam, ctypes.POINTER(wintypes.RECT)).contents
                self._dpi_scale = ((wparam & 0xFFFF) or 96) / 96
                self._layout_hold += 1
                try:
                    self._place_panel((rect.left, rect.top, rect.right, rect.bottom))
                    self._fit_current()
                finally:
                    self._layout_hold -= 1
                self._layout()
                return 0
            if message in (con.WM_CTLCOLORSTATIC, con.WM_CTLCOLOREDIT):
                gui.SetTextColor(wparam, self._api.RGB(*TEXT_COLOR))
                gui.SetBkColor(wparam, self._api.RGB(*BACKGROUND))
                return self._background
            if message == con.WM_DRAWITEM:
                self._paint_button(lparam)
                return 1
            if (
                message == con.WM_COMMAND
                and lparam == self._composer
                and (wparam >> 16) in (con.EN_CHANGE, con.EN_VSCROLL)
            ):
                self._sync_scrollbars()
                return 0
            if message == con.WM_COMMAND and (wparam >> 16) == con.BN_CLICKED:
                self._button(wparam & 0xFFFF)
                return 0
            if message == 0x0232 and handle == self._panel:  # WM_EXITSIZEMOVE
                self._read_dpi()
                self._snap_edge()
                return 0
            if (
                handle == self._panel
                and self._history_window
                and (message == 0x007E or (message == 0x001A and wparam == 0x002F))
            ):  # WM_DISPLAYCHANGE / SPI_SETWORKAREA
                self._read_dpi()
                self._fit_current()
                return 0
            if message == 0x001A and handle == self._panel:
                self._update_motion_preference()
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
        self._font_height = max(1, round(FONT_SIZES[self._text_size_index] * self._scale))
        self._replace_font()

    def _replace_font(self):
        gui, con = self._gui, self._con
        candidates = (self._font_face,) if self._font_face_verified else _FONT_FACES
        for face in candidates:
            description = gui.LOGFONT()
            description.lfFaceName = face
            description.lfHeight = -self._font_height
            description.lfWeight = 400
            description.lfQuality = con.CLEARTYPE_QUALITY
            font = gui.CreateFontIndirect(description)
            try:
                actual_face = face if self._font_face_verified else self._font_name(font)
            except Exception:
                gui.DeleteObject(font)
                raise
            if face != candidates[-1] and not actual_face.casefold().startswith(
                "segoe ui variable"
            ):
                gui.DeleteObject(font)
                continue
            self._font_face, self._font_face_verified = actual_face, True
            break
        for identifier, handle in self._children().items():
            if handle and identifier != HISTORY:
                gui.SendMessage(handle, con.WM_SETFONT, font, False)
        old, self._font = self._font, font
        self._line_height = 0
        self._history_font_dirty = True
        if old:
            if self._history is not None:
                self._retired_fonts.append(old)
            else:
                gui.DeleteObject(old)

    def _font_name(self, font: int) -> str:
        gui = self._gui
        dc = gui.GetDC(self._panel)
        with ExitStack() as cleanup:
            cleanup.callback(gui.ReleaseDC, self._panel, dc)
            old = gui.SelectObject(dc, font)
            cleanup.callback(gui.SelectObject, dc, old)
            name = ctypes.create_unicode_buffer(64)
            if not self._gdi32.GetTextFaceW(dc, len(name), name):
                raise ctypes.WinError(ctypes.get_last_error())
            return name.value

    def _client_animations_enabled(self) -> bool:
        enabled = wintypes.BOOL()
        success = self._user32.SystemParametersInfoW(
            _CLIENT_AREA_ANIMATION, 0, ctypes.byref(enabled), 0
        )
        return bool(success and enabled.value)

    def _update_motion_preference(self) -> None:
        self._motion_enabled = self._client_animations_enabled()
        if not self._motion_enabled and self._history is not None:
            self._history.cancel_animation()
            self._history_changed()

    def _style_history(self) -> None:
        if self._history is not None and self._font and self._history_font_dirty:
            self._history.set_font(
                self._font, line_height=self._font_line_height(), scale=self._scale
            )
            self._history_font_dirty = False
            old_fonts, self._retired_fonts = self._retired_fonts, []
            for font in old_fonts:
                self._gui.DeleteObject(font)

    @contextmanager
    def _redraw_editors(self) -> Iterator[None]:
        composing = bool(self._composer and (self._ime_composing or self._composition_active()))
        self._programmatic_depth += 1
        try:
            with ExitStack() as cleanup:
                if self._composer and not composing and self._gui.IsWindowVisible(self._composer):
                    # The history component owns its paint transaction; never suppress its host.
                    cleanup.callback(
                        self._gui.SendMessage, self._composer, self._con.WM_SETREDRAW, True, 0
                    )
                    self._gui.SendMessage(self._composer, self._con.WM_SETREDRAW, False, 0)
                yield
        finally:
            self._programmatic_depth -= 1

    def _redraw_panel(self) -> None:
        if self._panel:
            con = self._con
            self._gui.RedrawWindow(
                self._panel,
                None,
                None,
                con.RDW_INVALIDATE
                | con.RDW_ERASE
                | con.RDW_ALLCHILDREN
                | con.RDW_FRAME
                | con.RDW_UPDATENOW,
            )

    def _position_children(self, controls: dict[int, Rect]) -> None:
        con = self._con
        children = [
            (identifier, handle) for identifier, handle in self._children().items() if handle
        ]
        batch = self._user32.BeginDeferWindowPos(len(children))
        if not batch:
            raise ctypes.WinError(ctypes.get_last_error())
        for identifier, handle in children:
            left, top, right, bottom = controls.get(identifier, (0, 0, 1, 1))
            flags = con.SWP_NOZORDER | con.SWP_NOACTIVATE | con.SWP_NOREDRAW | con.SWP_NOCOPYBITS
            visible = identifier in controls
            if self._child_visibility.get(identifier, True) != visible:
                flags |= con.SWP_SHOWWINDOW if visible else con.SWP_HIDEWINDOW
            batch = self._user32.DeferWindowPos(
                batch, handle, 0, left, top, right - left, bottom - top, flags
            )
            if not batch:
                raise ctypes.WinError(ctypes.get_last_error())
        if not self._user32.EndDeferWindowPos(batch):
            raise ctypes.WinError(ctypes.get_last_error())
        self._child_visibility.update(
            (identifier, identifier in controls) for identifier, _ in children
        )

    def _layout(self) -> None:
        if self._layout_hold:
            return
        if self._layout_busy:
            if not self._layout_posted and not self._exit:
                self._layout_posted = True
                self._gui.PostMessage(self._panel, _REFLOW, 0, 0)
            return
        _, _, width, height = self._gui.GetClientRect(self._panel)
        if width <= 0 or height <= 0:
            return
        self._layout_busy = True
        try:
            self._cancel_scroll_drag()
            if self._history is not None:
                self._history.cancel_interaction()
            with self._redraw_editors():
                self._layout_controls(width, height)
            self._layout_serial += 1
        finally:
            self._layout_busy = False
            self._redraw_panel()
        self._sync_scrollbars()

    def _layout_controls(self, width: int, height: int) -> None:
        gui = self._gui
        composing = bool(self._composer and (self._ime_composing or self._composition_active()))
        history_view = self._history.capture_view() if self._history is not None else None
        composer_view = (
            self._read_view(self._composer) if self._composer and not composing else None
        )
        font_dip = FONT_SIZES[self._text_size_index]
        layout = layout_client(
            width, height, self._dpi_scale, compact=self._compact, font_dip=font_dip
        )
        if self._scale != layout.scale:
            self._history_font_dirty = True
        self._scale = layout.scale
        if self._font_height != layout.font_height:
            self._font_height = layout.font_height
            if self._font:
                self._replace_font()
        self._position_children(layout.controls)
        if self._composer:
            margin = max(1, round(4 * self._scale))
            gui.SendMessage(self._composer, self._con.EM_SETMARGINS, 3, margin | (margin << 16))
        if self._history is not None:
            self._style_history()
            _, _, history_width, history_height = gui.GetClientRect(self._history_window)
            self._history.reflow(history_width, history_height)
            if history_view is not None:
                self._history.restore_view(history_view)
        if composer_view is not None:
            self._restore_view(self._composer, composer_view)
        size_name = _TEXT_SIZE_NAMES[self._text_size_index]
        if self._history_label:
            left, _, right, _ = layout.controls[HISTORY_LABEL]
            label = self._fit_text(
                (
                    f"Conversation · Text: {size_name} · Ctrl +/-",
                    f"Text: {size_name} · Ctrl +/-",
                    f"{size_name} · Ctrl +/-",
                    f"Text: {size_name}",
                ),
                right - left,
            )
            gui.SetWindowText(self._history_label, label)
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
            "font_face": self._font_face,
            "text_size": size_name,
            "font_dip": font_dip,
            "split": layout.split,
            "scrollbar_width": layout.scrollbar_width,
        }
        self._sync_scrollbars(force=True)

    def _font_line_height(self) -> int:
        if not self._line_height:
            gui = self._gui
            dc = gui.GetDC(self._composer or self._panel)
            with ExitStack() as cleanup:
                cleanup.callback(gui.ReleaseDC, self._composer or self._panel, dc)
                if self._font:
                    old = gui.SelectObject(dc, self._font)
                    cleanup.callback(gui.SelectObject, dc, old)
                metrics = _TextMetric()
                if not self._gdi32.GetTextMetricsW(dc, ctypes.byref(metrics)):
                    raise ctypes.WinError(ctypes.get_last_error())
                self._line_height = max(1, metrics.height)
        return self._line_height

    def _scroll_state(self, editor: int) -> ScrollState:
        if editor == self._history_window and self._history is not None:
            return self._history.scroll_state()
        if editor != self._composer:
            raise RuntimeError("The scroll target is unavailable.")
        gui, con = self._gui, self._con
        lines = gui.SendMessage(editor, con.EM_GETLINECOUNT, 0, 0)
        first = gui.SendMessage(editor, con.EM_GETFIRSTVISIBLELINE, 0, 0)
        formatting = wintypes.RECT()
        gui.SendMessage(editor, con.EM_GETRECT, 0, ctypes.addressof(formatting))
        # No WS_VSCROLL: GetScrollInfo on this EDIT correctly fails with ERROR_NO_SCROLLBARS.
        return ScrollState.from_edit(
            lines, first, formatting.bottom - formatting.top, self._font_line_height()
        )

    def _scroll_target(self, scrollbar: int) -> int:
        if scrollbar == self._scrollbars.get(HISTORY_SCROLL):
            return self._history_window
        if scrollbar == self._scrollbars.get(COMPOSER_SCROLL):
            return self._composer
        return 0

    def _sync_scrollbars(self, *, force: bool = False) -> None:
        if (
            self._exit
            or self._scroll_syncing
            or (not force and (self._layout_busy or self._programmatic_depth))
        ):
            return
        self._scroll_syncing = True
        try:
            for scrollbar in self._scrollbars.values():
                editor = self._scroll_target(scrollbar)
                if editor:
                    state = self._scroll_state(editor)
                    if self._scroll_states.get(scrollbar) != state:
                        self._scroll_states[scrollbar] = state
                        self._gui.InvalidateRect(scrollbar, None, False)
        finally:
            self._scroll_syncing = False

    def _record_user_view(self, editor: int) -> None:
        if editor == self._history_window:
            self._history_changed()
        self._sync_scrollbars()

    def _scroll_to(self, editor: int, position: int) -> None:
        if editor == self._history_window and self._history is not None:
            self._history.scroll_to(position)
            self._history_changed()
            return
        state = self._scroll_state(editor)
        current = self._gui.SendMessage(editor, self._con.EM_GETFIRSTVISIBLELINE, 0, 0)
        self._gui.SendMessage(editor, self._con.EM_LINESCROLL, 0, state.clamp(position) - current)
        self._record_user_view(editor)

    def _scroll_command(self, editor: int, command: int) -> bool:
        if editor == self._history_window and self._history is not None:
            handled = self._history.scroll_command(command)
            self._history_changed()
            return handled
        con = self._con
        state = self._scroll_state(editor)
        targets = {
            con.SB_LINEUP: state.position - 1,
            con.SB_LINEDOWN: state.position + 1,
            con.SB_PAGEUP: state.position - state.page_step,
            con.SB_PAGEDOWN: state.position + state.page_step,
            con.SB_TOP: 0,
            con.SB_BOTTOM: state.maximum,
        }
        if command not in targets:
            return False
        self._scroll_to(editor, targets[command])
        return True

    def _wheel_scroll(self, editor: int, wparam: int) -> None:
        delta = ctypes.c_short((wparam >> 16) & 0xFFFF).value
        lines = wintypes.UINT(3)
        if not self._user32.SystemParametersInfoW(0x0068, 0, ctypes.byref(lines), 0):
            lines.value = 3
        if editor == self._history_window and self._history is not None:
            self._history.wheel(delta, lines_per_notch=lines.value)
            self._history_changed()
            return
        state = self._scroll_state(editor)
        movement, remainder = wheel_movement(
            self._wheel_remainders.get(editor, 0), delta, lines.value, state.page
        )
        self._wheel_remainders[editor] = remainder
        if movement:
            self._scroll_to(editor, state.position + movement)

    def _edit_procedure(self, handle, message, wparam, lparam, subclass_id=0, data=0):
        con = self._con
        try:
            if handle == self._composer:
                if message == 0x010D:  # WM_IME_STARTCOMPOSITION, including an empty preedit.
                    self._ime_composing = True
                    self._text_size_keys_held.clear()
                elif message in (0x010E, con.WM_NCDESTROY):
                    self._ime_composing = False
            if message == con.WM_NCDESTROY:
                self._comctl.RemoveWindowSubclass(handle, self._edit_callback, _EDIT_SUBCLASS)
                return self._comctl.DefSubclassProc(handle, message, wparam, lparam)
            if self._exit:
                return self._comctl.DefSubclassProc(handle, message, wparam, lparam)
            if message == con.WM_MOUSEWHEEL:
                self._wheel_scroll(handle, wparam)
                return 0
            if (
                message == con.WM_VSCROLL
                and not self._programmatic_depth
                and self._scroll_command(handle, wparam & 0xFFFF)
            ):
                return 0
            mouse_selection = message in (
                con.WM_LBUTTONDOWN,
                con.WM_LBUTTONDBLCLK,
                con.WM_LBUTTONUP,
            ) or (message == con.WM_MOUSEMOVE and wparam & con.MK_LBUTTON)
            navigation = mouse_selection or (
                message == con.WM_KEYDOWN
                and wparam
                in (
                    con.VK_UP,
                    con.VK_DOWN,
                    con.VK_LEFT,
                    con.VK_RIGHT,
                    con.VK_PRIOR,
                    con.VK_NEXT,
                    con.VK_HOME,
                    con.VK_END,
                )
            )
            result = self._comctl.DefSubclassProc(handle, message, wparam, lparam)
            if navigation:
                self._record_user_view(handle)
            elif message in (
                con.WM_CHAR,
                con.WM_SETTEXT,
                con.WM_SIZE,
                con.WM_PASTE,
                con.WM_CUT,
                con.WM_CLEAR,
                con.WM_UNDO,
                con.EM_LINESCROLL,
                con.EM_SCROLLCARET,
            ):
                self._sync_scrollbars()
            return result
        except Exception as error:
            self._error = error
            self.controller.set_interface_ready(
                False, f"Transcript scrolling failed: {type(error).__name__}"
            )
            self._exit = True
            self._cancel_modal()
            return 0

    def _cancel_scroll_drag(self, handle: int | None = None, *, repaint: bool = True) -> None:
        drag = self._scroll_drag
        if drag is None or (handle is not None and handle != drag.window):
            return
        self._scroll_drag = None
        try:
            if self._gui.GetCapture() == drag.window:
                self._gui.ReleaseCapture()
        finally:
            if (
                self._history is not None
                and self._scroll_target(drag.window) == self._history_window
            ):
                self._history.set_interacting(False)
                self._history_changed()
            if repaint and not self._exit:
                self._gui.InvalidateRect(drag.window, None, False)

    def _drag_scroll(self, handle: int, y: int) -> None:
        drag = self._scroll_drag
        if drag is None or drag.window != handle:
            return
        if self._gui.GetCapture() != handle:
            self._cancel_scroll_drag(handle)
            return
        editor = self._scroll_target(handle)
        state = self._scroll_state(editor)
        height = self._gui.GetClientRect(handle)[3]
        thumb = thumb_geometry(state, height, self._scale)
        self._scroll_to(
            editor, dragged_position(state, thumb, y, drag.fraction, origin=drag.origin)
        )

    def _scrollbar_procedure(self, handle, message, wparam, lparam):
        gui, con = self._gui, self._con
        if message in (con.WM_CANCELMODE, con.WM_DESTROY, con.WM_NCDESTROY, 0x0215):
            self._cancel_scroll_drag(
                handle, repaint=message not in (con.WM_DESTROY, con.WM_NCDESTROY)
            )
            if self._scroll_hover == handle:
                self._scroll_hover = 0
            if message == con.WM_NCDESTROY:
                self._scroll_states.pop(handle, None)
                for identifier, window in tuple(self._scrollbars.items()):
                    if window == handle:
                        self._scrollbars.pop(identifier)
                        self._child_visibility.pop(identifier, None)
            return gui.DefWindowProc(handle, message, wparam, lparam)
        if self._exit:
            return gui.DefWindowProc(handle, message, wparam, lparam)
        if message == con.WM_ERASEBKGND:
            return 1  # WM_PAINT fills the complete control once.
        if message == con.WM_PAINT:
            self._paint_scrollbar(handle)
            return 0
        if message in (con.WM_SETFOCUS, con.WM_KILLFOCUS):
            if message == con.WM_KILLFOCUS:
                self._cancel_scroll_drag(handle)
            gui.InvalidateRect(handle, None, False)
        keys = {
            con.VK_UP: con.SB_LINEUP,
            con.VK_DOWN: con.SB_LINEDOWN,
            con.VK_PRIOR: con.SB_PAGEUP,
            con.VK_NEXT: con.SB_PAGEDOWN,
            con.VK_HOME: con.SB_TOP,
            con.VK_END: con.SB_BOTTOM,
        }
        if message == con.WM_GETDLGCODE:
            return con.DLGC_WANTARROWS | (con.DLGC_WANTALLKEYS if wparam in keys else 0)
        if message == con.WM_KEYDOWN and wparam in keys:
            self._scroll_command(self._scroll_target(handle), keys[wparam])
            return 0
        if message == con.WM_MOUSEWHEEL:
            self._wheel_scroll(self._scroll_target(handle), wparam)
            return 0
        if message == 0x02A3:  # WM_MOUSELEAVE
            if self._scroll_hover == handle:
                self._scroll_hover = 0
            gui.InvalidateRect(handle, None, False)
            return 0
        x = ctypes.c_short(lparam & 0xFFFF).value
        y = ctypes.c_short((lparam >> 16) & 0xFFFF).value
        if message == con.WM_MOUSEMOVE:
            _, _, width, height = gui.GetClientRect(handle)
            hovered = handle if 0 <= x < width and 0 <= y < height else 0
            if hovered != self._scroll_hover:
                previous = self._scroll_hover
                self._scroll_hover = hovered
                if previous:
                    gui.InvalidateRect(previous, None, False)
                tracking = _TrackMouseEvent(ctypes.sizeof(_TrackMouseEvent), 2, handle, 0)
                self._user32.TrackMouseEvent(ctypes.byref(tracking))
                gui.InvalidateRect(handle, None, False)
            if (
                self._scroll_drag is not None
                and self._scroll_drag.window == handle
                and not wparam & con.MK_LBUTTON
            ):
                self._cancel_scroll_drag(handle)
                self._record_user_view(self._scroll_target(handle))
            else:
                self._drag_scroll(handle, y)
            return 0
        if message == con.WM_LBUTTONDOWN:
            _, _, width, height = gui.GetClientRect(handle)
            if not (0 <= x < width and 0 <= y < height):
                return 0
            if gui.GetForegroundWindow() == self._panel:
                gui.SetFocus(handle)
            editor = self._scroll_target(handle)
            state = self._scroll_state(editor)
            thumb = thumb_geometry(state, height, self._scale)
            if not state.maximum:
                return 0
            if thumb.top <= y < thumb.bottom:
                self._cancel_scroll_drag()
                if gui.GetCapture() not in (0, handle):
                    return 0
                gui.SetCapture(handle)
                if gui.GetCapture() == handle:
                    self._scroll_drag = _ScrollDrag(
                        handle, thumb.grab_fraction(y), (y, state.position)
                    )
                    if editor == self._history_window and self._history is not None:
                        self._history.set_interacting(True)
                    self._record_user_view(editor)
                    gui.InvalidateRect(handle, None, False)
            else:
                self._scroll_to(
                    editor,
                    state.position + (-state.page_step if y < thumb.top else state.page_step),
                )
            return 0
        if (
            message == con.WM_LBUTTONUP
            and self._scroll_drag is not None
            and self._scroll_drag.window == handle
        ):
            try:
                self._drag_scroll(handle, y)
            finally:
                self._cancel_scroll_drag(handle)
            self._record_user_view(self._scroll_target(handle))
            return 0
        return gui.DefWindowProc(handle, message, wparam, lparam)

    def _paint_scrollbar(self, handle: int) -> None:
        gui, con = self._gui, self._con
        dc, paint = gui.BeginPaint(handle)
        try:
            rectangle = gui.GetClientRect(handle)
            gui.FillRect(dc, rectangle, self._background)
            width, height = rectangle[2:]
            state = self._scroll_states.get(handle, ScrollState(1, 1, 0))
            thumb = thumb_geometry(state, height, self._scale)
            active = self._scroll_hover == handle or (
                self._scroll_drag is not None and self._scroll_drag.window == handle
            )
            inset = min(max(0, round(self._scale)), (width - 1) // 2)

            def rounded(top: int, bottom: int, color: tuple[int, int, int]) -> None:
                with ExitStack() as cleanup:
                    brush = gui.CreateSolidBrush(self._api.RGB(*color))
                    cleanup.callback(gui.DeleteObject, brush)
                    pen = gui.CreatePen(con.PS_SOLID, 1, self._api.RGB(*color))
                    cleanup.callback(gui.DeleteObject, pen)
                    old_brush = gui.SelectObject(dc, brush)
                    cleanup.callback(gui.SelectObject, dc, old_brush)
                    old_pen = gui.SelectObject(dc, pen)
                    cleanup.callback(gui.SelectObject, dc, old_pen)
                    radius = max(1, width - 2 * inset)
                    gui.RoundRect(dc, inset, top, width - inset, bottom, radius, radius)

            rounded(thumb.track_top, thumb.track_top + thumb.track_length, (34, 37, 43))
            if state.maximum:
                color = (142, 155, 174) if active else (96, 108, 126)
                rounded(thumb.top, thumb.bottom, color)
            if gui.GetFocus() == handle:
                gui.DrawFocusRect(dc, (0, 0, width, height))
        finally:
            gui.EndPaint(handle, paint)

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
        if self._ime_composing:
            return True
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

    def _text_size_key(self, message) -> bool:
        con, gui = self._con, self._gui
        kind, key = message.message, int(message.wParam)
        if kind == con.WM_KEYUP and key in (con.VK_CONTROL, 0xA2, 0xA3):
            self._text_size_keys_held.clear()
            return False
        if kind not in (con.WM_KEYDOWN, con.WM_KEYUP, con.WM_CHAR):
            return False
        if kind == con.WM_KEYDOWN and key not in _TEXT_SIZE_KEYS:
            return False
        if kind == con.WM_KEYUP and key not in self._text_size_keys_held:
            return False
        if kind == con.WM_CHAR and not self._text_size_keys_held:
            return False
        if not self.visible or gui.GetForegroundWindow() != self._panel:
            self._text_size_keys_held.clear()
            return False
        focused = gui.GetFocus()
        if (
            not focused
            or focused != message.hWnd
            or focused == self._canvas
            or focused not in self.window_roles()
        ):
            self._text_size_keys_held.clear()
            return False
        if kind == con.WM_KEYUP:
            handled = key in self._text_size_keys_held
            self._text_size_keys_held.discard(key)
            return handled
        if self._ime_composing or (focused == self._composer and self._composition_active()):
            self._text_size_keys_held.clear()
            return False
        if kind == con.WM_CHAR:
            return any(
                key in ((ord("+"), ord("=")) if _TEXT_SIZE_KEYS[held] > 0 else (ord("-"), 0x1F))
                for held in self._text_size_keys_held
            )
        if (
            key not in _TEXT_SIZE_KEYS
            or not self._user32.GetKeyState(con.VK_CONTROL) & 0x8000
            or self._user32.GetKeyState(con.VK_MENU) & 0x8000
        ):
            return False
        repeated = key in self._text_size_keys_held or bool(message.lParam & (1 << 30))
        self._text_size_keys_held.add(key)
        if not repeated:
            target = max(0, min(len(FONT_SIZES) - 1, self._text_size_index + _TEXT_SIZE_KEYS[key]))
            if target != self._text_size_index:
                self._text_size_index = target
                self._history_font_dirty = True
                self._layout()
        return True

    def _composer_key(self, message) -> bool:
        if (
            not self._composer
            or message.hWnd != self._composer
            or message.message != self._con.WM_KEYDOWN
            or message.wParam != self._con.VK_RETURN
        ):
            return False
        if (
            self._user32.GetKeyState(self._con.VK_SHIFT) & 0x8000
            or self._ime_composing
            or self._composition_active()
        ):
            return False
        if not message.lParam & (1 << 30):
            self._send_user()
        return True

    def _send_user(self) -> None:
        try:
            length = self._gui.SendMessage(self._composer, self._con.WM_GETTEXTLENGTH, 0, 0)
            if not 0 <= length <= 2 * MAX_TEXT:
                raise ValueError("The message is too long; shorten it before sending.")
            draft = ctypes.create_unicode_buffer(length + 1)
            copied = self._gui.SendMessage(
                self._composer, self._con.WM_GETTEXT, len(draft), ctypes.addressof(draft)
            )
            if copied != length:
                raise RuntimeError("The complete draft could not be read. Try Send again.")
            self.session.conversation.send_user(draft.value)
        except (ValueError, RuntimeError) as error:
            self._message_error = str(error)
        else:
            self._message_error = None
            self._gui.SetWindowText(self._composer, "")
        self._refresh()

    def _sample_view(self, handle: int) -> _EditView:
        if handle != self._composer:
            raise RuntimeError("Native EDIT views belong only to the composer.")
        gui, con = self._gui, self._con
        start, end = wintypes.DWORD(), wintypes.DWORD()
        # The packed EM_GETSEL result truncates offsets above 65535 UTF-16 code units.
        gui.SendMessage(handle, con.EM_GETSEL, ctypes.addressof(start), ctypes.addressof(end))
        first_line = gui.SendMessage(handle, con.EM_GETFIRSTVISIBLELINE, 0, 0)
        anchor = max(0, gui.SendMessage(handle, con.EM_LINEINDEX, first_line, 0))
        return _EditView(anchor, (start.value, end.value))

    def _read_view(self, handle: int) -> _EditView:
        return self._sample_view(handle)

    def _restore_view(self, handle: int, view: _EditView) -> None:
        if handle != self._composer:
            raise RuntimeError("Native EDIT views belong only to the composer.")
        gui, con = self._gui, self._con
        gui.SendMessage(handle, con.EM_SETSEL, *view.selection)
        line = gui.SendMessage(handle, con.EM_LINEFROMCHAR, view.anchor, 0)
        current = gui.SendMessage(handle, con.EM_GETFIRSTVISIBLELINE, 0, 0)
        gui.SendMessage(handle, con.EM_LINESCROLL, 0, line - current)

    def _set_unread(self, unread: bool) -> None:
        if unread != self._history_unread:
            self._history_unread = unread
            if handle := self._buttons.get(_LATEST):
                self._gui.SetWindowText(handle, "Latest *" if unread else "Latest")

    def _scroll_latest(self) -> None:
        if self._history is not None:
            self._history.latest()
            self._history_changed()

    def _update_history(self, entries: tuple, *, now: float | None = None) -> None:
        if self._history is None:
            if entries:
                raise RuntimeError("The history component is unavailable.")
            return
        if entries != self._last_text:
            self._history.set_entries(
                entries,
                now=time.monotonic() if now is None else now,
                animate=self.visible and self._motion_enabled and not self._layout_busy,
            )
            self._last_text = entries
        self._history_changed()

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
            terse = "Not sent; kept"
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
        if not visible:
            self._text_size_keys_held.clear()
            self._cancel_scroll_drag()
            if self._history is not None:
                self._history.cancel_interaction()
                self._history_changed()
        self._shown = visible
        self._minimized = False
        if self._hide_count:
            self._restore_panel = (visible, False)
            return
        self._gui.ShowWindow(
            self._panel, self._con.SW_SHOWNOACTIVATE if visible else self._con.SW_HIDE
        )
        self._schedule_timer()

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
        layout_serial = self._layout_serial
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
                self._con.SWP_NOZORDER
                | self._con.SWP_NOACTIVATE
                | self._con.SWP_NOCOPYBITS
                | (self._con.SWP_NOREDRAW if self._layout_hold else 0),
            )
        finally:
            self._placement_width = previous
        if self._layout_serial == layout_serial:
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
                        self._capture_serial += 1
                        self._cancel_scroll_drag()
                        if self._history is not None:
                            self._history.cancel_interaction()
                            self._history_changed()
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
                    self._schedule_timer()
                elif request.command == "restore":
                    if self._hide_count <= 0:
                        raise RuntimeError("Unbalanced guidance capture guard.")
                    self._hide_count -= 1
                    if self._hide_count == 0:
                        self._capture_serial += 1
                        self._restore_panel_visibility()
                        self._last_scene = None
                        self._next_state_refresh = 0.0
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
        self._text_size_keys_held.clear()
        try:
            try:
                self._cancel_scroll_drag()
            finally:
                if self._history is not None:
                    self._history.cancel_interaction()
        finally:
            if self._panel:
                self._gui.SendMessage(self._panel, self._con.WM_CANCELMODE, 0, 0)

    @staticmethod
    def _scene_bounds(
        snapshot: TeachingSnapshot, desktop: Rect, *, now: float | None = None
    ) -> Rect | None:
        from desktop_mcp.teaching_render import validate_scene

        return validate_scene(snapshot, now=time.monotonic() if now is None else now, clip=desktop)

    def _schedule_timer(self) -> None:
        animated = not self._hide_count and (
            self._scene_animating or (self.visible and self._chat_animating)
        )
        interval = _ANIMATION_TIMER_MS if animated else _IDLE_TIMER_MS
        if self._timer_running and not self._exit and interval != self._timer_interval:
            if not self._user32.SetTimer(self._panel, 1, interval, None):
                raise ctypes.WinError(ctypes.get_last_error())
            self._timer_interval = interval

    def _on_timer(self) -> None:
        if self._scene_rendering:
            return
        now = time.monotonic()
        control = self.controller.snapshot()
        ticket = control.generation, control.input_revision
        if (
            self._scene_snapshot is None
            or ticket != self._scene_ticket
            or now + _STATE_DEADLINE_EPSILON >= self._next_state_refresh
        ):
            self._refresh(now=now, animate_scene=True)
        elif self._hide_count or not control.armed:
            self._clear_scene()
        elif self._scene_animating and now >= self._next_scene_frame:
            snapshot = self.session.snapshot()
            confirmed = self.controller.snapshot()
            if ticket != (confirmed.generation, confirmed.input_revision):
                self._clear_scene(invalidate=True)
            else:
                self._scene_snapshot, self._scene_ticket = snapshot, ticket
                self._refresh_scene(confirmed, snapshot, now)
        self._tick_history(now)
        self._schedule_timer()

    def _tick_history(self, now: float) -> None:
        if self._history is None:
            self._chat_animating = False
            return
        if not self.visible or not self._motion_enabled:
            if self._history.animation_active:
                self._history.cancel_animation()
            self._chat_animating = False
        elif self._history.animation_active:
            self._history.tick(now)
            self._chat_animating = self._history.animation_active
        else:
            self._chat_animating = False

    def _advance_state_deadline(self, now: float) -> None:
        if self._next_state_refresh <= 0:
            self._next_state_refresh = now + _STATE_REFRESH_SECONDS
        elif now + _STATE_DEADLINE_EPSILON >= self._next_state_refresh:
            missed = (
                int(
                    (now - self._next_state_refresh + _STATE_DEADLINE_EPSILON)
                    // _STATE_REFRESH_SECONDS
                )
                + 1
            )
            self._next_state_refresh += max(1, missed) * _STATE_REFRESH_SECONDS

    def _refresh(self, *, now: float | None = None, animate_scene: bool = False) -> None:
        now = time.monotonic() if now is None else now
        control = self.controller.snapshot()
        snapshot = self.session.snapshot()
        ticket = control.generation, control.input_revision
        confirmed = self.controller.snapshot()
        valid = ticket == (confirmed.generation, confirmed.input_revision)
        self._scene_snapshot = snapshot if valid else None
        self._scene_ticket = ticket if valid else None
        self._advance_state_deadline(now)
        entries = tuple(
            (entry.sequence, entry.title, entry.text, entry.role) for entry in snapshot.entries
        )
        self._update_history(entries, now=now)
        self._refresh_status(control, snapshot)
        self._sync_scrollbars()
        if valid:
            self._refresh_scene(confirmed, snapshot, now, animate=animate_scene)
        else:
            self._clear_scene(invalidate=True)
        self._schedule_timer()

    def _clear_scene(self, *, invalidate: bool = False) -> None:
        if self._canvas:
            self._gui.ShowWindow(self._canvas, self._con.SW_HIDE)
        self._last_scene = None
        self._scene_animating = False
        if invalidate:
            self._scene_snapshot = None
            self._scene_ticket = None
            self._next_state_refresh = 0.0

    def _scene_authorized(self, ticket: tuple[int, int], capture_serial: int) -> bool:
        control = self.controller.snapshot()
        return (
            not self._exit
            and not self._hide_count
            and self._capture_serial == capture_serial
            and control.armed
            and (control.generation, control.input_revision) == ticket
        )

    @staticmethod
    def _same_scene_context(before: TeachingSnapshot, after: TeachingSnapshot) -> bool:
        if before.marks != after.marks:
            return False
        if before.waiting is None or after.waiting is None:
            return before.waiting is after.waiting
        return (
            before.waiting.center == after.waiting.center
            and before.waiting.radius == after.waiting.radius
        )

    def _refresh_scene(
        self, control, snapshot: TeachingSnapshot, now: float, *, animate: bool = True
    ) -> None:
        from desktop_mcp.layers import upload_rgba
        from desktop_mcp.teaching_render import SceneTooLarge, render_marks

        gui, con = self._gui, self._con
        self._scene_animating = False
        ticket = control.generation, control.input_revision
        capture_serial = self._capture_serial
        if self._hide_count or not control.armed:
            # Canvas visibility does not invalidate the independent chat polling deadline.
            self._clear_scene()
            return
        if not self._scene_authorized(ticket, capture_serial):
            self._clear_scene(invalidate=True)
            return
        left, top = self._api.GetSystemMetrics(76), self._api.GetSystemMetrics(77)
        desktop = (
            left,
            top,
            left + self._api.GetSystemMetrics(78),
            top + self._api.GetSystemMetrics(79),
        )
        try:
            bounds = self._scene_bounds(snapshot, desktop, now=now)
        except SceneTooLarge:
            self._clear_scene()
            self._set_status("Guidance scene too large. Erase older marks.")
            return
        if bounds is None:
            self._clear_scene()
            return
        self._scene_animating = any(
            mark.kind == "laser"
            and mark.created_at
            <= now
            < (mark.expires_at if mark.expires_at is not None else mark.created_at + 2.0)
            for mark in snapshot.marks
        )
        signature = snapshot.revision, bounds, snapshot.waiting
        if (
            self._scene_rendering
            or now < self._next_scene_frame
            or (signature == self._last_scene and not (animate and self._scene_animating))
        ):
            return
        self._scene_rendering = True
        started = time.monotonic()
        try:
            with closing(render_marks(snapshot, bounds, now=now)) as image:
                # Rendering can outlast a Stop, capture request or annotation-only update.
                self._drain_requests()
                if not self._scene_authorized(ticket, capture_serial):
                    self._clear_scene(invalidate=True)
                    return
                live = self.session.snapshot()
                if not self._same_scene_context(snapshot, live) or not self._scene_authorized(
                    ticket, capture_serial
                ):
                    self._clear_scene(invalidate=True)
                    return
                upload_rgba(self._canvas, (bounds[0], bounds[1]), image)
            if not self._scene_authorized(ticket, capture_serial):
                self._clear_scene(invalidate=True)
                return
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
        finally:
            finished = time.monotonic()
            elapsed = max(0.0, finished - started)
            # Fast scenes use every wake. Slow scenes leave bounded UI service time, not a backlog.
            self._next_scene_frame = (
                finished + min(0.05, elapsed * 0.5) if elapsed > _ANIMATION_TIMER_MS / 1000 else 0.0
            )
            self._scene_rendering = False
