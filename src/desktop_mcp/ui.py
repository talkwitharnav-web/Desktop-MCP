"""Local-only native control window, global stop, and physical-pointer overlay.

Importing this module does not load Win32 libraries or create GUI resources.
The UI thread owns every HWND, hook, hotkey, and GDI object. Cross-thread capture
requests are acknowledged only after both windows are hidden and DWM is flushed.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from collections.abc import Callable
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, field, replace
from enum import IntEnum
from typing import Iterator, Literal

from desktop_mcp.contracts import INJECTED_INPUT_TAG, ControlSnapshot, LocalControl, Point, Rect
from desktop_mcp.cursor import CursorSprite, premultiplied_bgra, render_cursor

logger = logging.getLogger(__name__)

_START_TIMEOUT = 5.0
_REQUEST_TIMEOUT = 3.0
_CLOSE_TIMEOUT = 5.0
_REFRESH_MS = 8
_WAKE_MESSAGE = 0x8000 + 41
_EXIT_SIZE_MOVE = 0x0232
_HOTKEY_ID = 0x444D
_STOP_MODIFIERS = 0x0002 | 0x0004 | 0x4000  # Control, Shift, no repeat.
_PANEL_STYLE = 0x02CA0000  # Caption, system menu, minimize, clip native children.
_PANEL_EX_STYLE = 0x00040000 | 0x00010000  # App window, control parent.
_OVERLAY_EX_STYLE = 0x00080000 | 0x00000020 | 0x00000008 | 0x00000080 | 0x08000000
_SHORTCUT_HINT = "Ctrl + Shift + H   ·   Global latched stop"
_PANEL_WIDTH = 456
_PANEL_HEIGHT = 348


class _LocalCommand(IntEnum):
    ARM = 1001
    STOP = 1002
    TAKEOVER = 1003
    TRANSCRIPT = 1004


@dataclass(frozen=True)
class _PanelModel:
    state: str
    heading: str
    detail: str
    action: str
    arm_enabled: bool
    stop_enabled: bool
    human_takeover: bool
    settings_enabled: bool
    transcript_visible: bool = True


def _fit_layout_scale(work: Rect, dpi: int, chrome: Point) -> float:
    width = work[2] - work[0] - chrome[0]
    height = work[3] - work[1] - chrome[1]
    if width <= 0 or height <= 0:
        raise OSError("The monitor work area cannot contain the local control interface.")
    return min(dpi / 96, width / _PANEL_WIDTH, height / _PANEL_HEIGHT)


def _panel_model(snapshot: ControlSnapshot, *, transcript_visible: bool = True) -> _PanelModel:
    headings = {
        "stopped": "Stopped · local approval required",
        "ready": "Ready · guidance and control",
        "running": "Working · guidance and control",
        "error": "Error · control is stopped",
        "closed": "Closed · control is unavailable",
    }
    if snapshot.awaiting_user:
        headings["running"] = "Your turn · waiting for your cursor"
    return _PanelModel(
        state=snapshot.state,
        heading=headings[snapshot.state],
        detail=snapshot.last_error or snapshot.reason or "Arm locally when you are ready.",
        action=snapshot.action or "No action running",
        arm_enabled=(
            snapshot.interface_ready and not snapshot.armed and snapshot.state != "closed"
        ),
        stop_enabled=snapshot.state != "closed",
        human_takeover=snapshot.human_takeover,
        settings_enabled=snapshot.interface_ready and snapshot.state != "closed",
        transcript_visible=transcript_visible,
    )


def _is_physical_input(kind: str, flags: int, extra_info: int) -> bool:
    injected_mask = 0x12 if kind == "keyboard" else 0x03
    return extra_info != INJECTED_INPUT_TAG and not flags & injected_mask


@dataclass
class _Request:
    operation: str
    token: object | None = None
    done: threading.Event = field(default_factory=threading.Event)
    cancelled: threading.Event = field(default_factory=threading.Event)
    error: BaseException | None = None


class ControlSurface:
    """Own an Alt-Tab-visible local control panel and a click-through cursor.

    Only native local button/keyboard interaction calls ``arm_local``. A stopped
    interface cannot be re-armed through ``start``, ``show``, or capture requests.
    """

    def __init__(
        self,
        controller: LocalControl,
        *,
        control_windows: Callable[[], tuple[int, ...]] | None = None,
        on_exit: Callable[[], None] | None = None,
        transcript_visible: Callable[[], bool] | None = None,
        on_transcript_toggle: Callable[[], None] | None = None,
    ) -> None:
        self._controller = controller
        self._control_windows = control_windows or self.window_handles
        self._on_exit = on_exit
        self._transcript_visible = transcript_visible or (lambda: True)
        self._on_transcript_toggle = on_transcript_toggle
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._adapter: _Win32Adapter | None = None
        self._ready = threading.Event()
        self._closed = threading.Event()
        self._closing = threading.Event()
        self._requests: queue.SimpleQueue[_Request] = queue.SimpleQueue()
        self._error: BaseException | None = None
        self._shutdown_error: BaseException | None = None
        self._handles: tuple[int, ...] = ()
        self._captures: set[object] = set()
        self._model: _PanelModel | None = None
        self._local_rejection: tuple[int, str] | None = None
        self._show_pending = False
        self._servicing_requests = False

    def start(self) -> None:
        """Start disarmed and wait boundedly for windows, hooks, and global stop.

        Raises:
            RuntimeError: Startup failed, timed out, or this surface was closed.
        """
        thread_error: Exception | None = None
        with self._lock:
            if self._closing.is_set() or self._closed.is_set():
                raise RuntimeError("The Desktop-MCP control surface is closed")
            if self._thread is None:
                self._thread = threading.Thread(
                    target=self._run, name="desktop-mcp-control", daemon=False
                )
                try:
                    self._thread.start()
                except Exception as error:
                    self._thread = None
                    self._closing.set()
                    thread_error = error
        if thread_error is not None:
            self._record_failure(thread_error)
            self._closed.set()
            self._ready.set()
            raise RuntimeError(f"Local control thread could not start: {thread_error}") from (
                thread_error
            )
        if not self._ready.wait(_START_TIMEOUT):
            error = TimeoutError("The local control interface did not become ready")
            self._record_failure(error)
            self._wake()
            raise RuntimeError(str(error)) from error
        with self._lock:
            error = self._error
        if error is not None:
            raise RuntimeError(f"Local control interface failed: {error}") from error
        if self._closing.is_set() or self._closed.is_set():
            raise RuntimeError("The local control interface closed during startup")

    def close(self) -> None:
        """Stop control and join the owning UI thread after native cleanup."""
        with self._lock:
            first_close = not self._closing.is_set()
            self._closing.set()
            thread = self._thread
        if first_close:
            try:
                self._controller.stop("Local control interface is closing")
            except Exception as error:
                self._record_failure(error)
        self._wake()
        if thread is None:
            try:
                self._controller.set_interface_ready(
                    False, error=str(self._error) if self._error is not None else None
                )
            finally:
                self._closed.set()
            return
        if thread is threading.current_thread():
            return
        thread.join(_CLOSE_TIMEOUT)
        if thread.is_alive():
            error = TimeoutError("The local control UI thread did not shut down")
            self._record_failure(error)
            raise RuntimeError(str(error)) from error
        if self._shutdown_error is not None:
            raise RuntimeError(f"Local control cleanup failed: {self._shutdown_error}") from (
                self._shutdown_error
            )

    def show(self) -> None:
        """Reveal the panel without activating it or granting control."""
        self._request("show")

    def window_handles(self) -> tuple[int, ...]:
        """Return owned top-level and child HWNDs for input-target exclusion."""
        with self._lock:
            return self._handles

    def window_roles(self) -> dict[int, str]:
        """Content-free roles for capture exclusion and protected-target diagnostics."""
        handles = self.window_handles()
        return {
            handle: "main-control"
            if index == 0
            else "cursor-overlay"
            if index == 1
            else "main-controls"
            for index, handle in enumerate(handles)
        }

    @contextmanager
    def capture_guard(self) -> Iterator[None]:
        """Hide both windows, wait for acknowledgement, and restore on exit.

        Nested/concurrent guards share one hidden interval. A closing or failed
        UI fails closed instead of allowing an unacknowledged capture. Shutdown
        destroys the windows rather than restoring them beneath an active guard.
        """
        token = object()
        self._request("capture_begin", token)
        body_error: BaseException | None = None
        try:
            yield
        except BaseException as error:
            body_error = error
            raise
        finally:
            if not self._closing.is_set() and not self._closed.is_set():
                try:
                    self._request("capture_end", token)
                except Exception as error:
                    if body_error is None:
                        raise
                    body_error.add_note(f"Control surface restoration also failed: {error}")

    def _wake(self) -> None:
        adapter = self._adapter
        if adapter is not None:
            try:
                adapter.wake()
            except Exception as error:
                if not self._closing.is_set():
                    self._record_failure(error)

    def _request(self, operation: str, token: object | None = None) -> None:
        if (
            not self._ready.is_set()
            or self._closing.is_set()
            or self._closed.is_set()
            or self._adapter is None
        ):
            raise RuntimeError("The local control interface is not available")
        request = _Request(operation, token)
        if self._thread is threading.current_thread():
            if self._servicing_requests:
                raise RuntimeError("A native UI request is already being serviced")
            self._servicing_requests = True
            try:
                self._execute(request)
                if self._closing.is_set():
                    raise RuntimeError("The local control interface is closing")
            except Exception as error:
                self._record_failure(error)
                raise RuntimeError(f"Local control {operation} failed: {error}") from error
            finally:
                self._servicing_requests = False
                self._continue_dispatch()
            return
        self._requests.put(request)
        self._wake()
        deadline = time.monotonic() + _REQUEST_TIMEOUT
        while not request.done.wait(min(0.05, max(0, deadline - time.monotonic()))):
            if self._closing.is_set() or self._closed.is_set():
                request.cancelled.set()
                raise RuntimeError("The local control interface closed during the request")
            if time.monotonic() >= deadline:
                request.cancelled.set()
                error = TimeoutError(f"Local control {operation} acknowledgement timed out")
                self._record_failure(error)
                self._wake()
                raise RuntimeError(str(error)) from error
        if request.error is not None:
            raise RuntimeError(f"Local control {operation} failed: {request.error}") from (
                request.error
            )
        if self._closing.is_set() or self._closed.is_set():
            raise RuntimeError("The local control interface closed during the request")

    def _execute(self, request: _Request) -> None:
        adapter = self._adapter
        if adapter is None:
            raise RuntimeError("The native interface is unavailable")
        if request.operation == "capture_begin":
            if request.token not in self._captures:
                self._captures.add(request.token)
                if len(self._captures) == 1:
                    adapter.hide_for_capture()
        elif request.operation == "capture_end":
            self._captures.discard(request.token)
            if not self._captures:
                adapter.restore_after_capture()
                if self._show_pending:
                    self._show_pending = False
                    adapter.show_panel()
                self._refresh()
        elif request.operation == "show":
            if self._captures:
                self._show_pending = True
            else:
                adapter.show_panel()
        else:
            raise ValueError(f"Unknown local UI request: {request.operation}")

    def _drain_requests(self) -> None:
        for _ in range(128):
            try:
                request = self._requests.get_nowait()
            except queue.Empty:
                return
            try:
                if not request.cancelled.is_set():
                    if self._closing.is_set():
                        raise RuntimeError("The local control interface is closing")
                    self._execute(request)
            except Exception as error:
                request.error = error
                self._record_failure(error)
            finally:
                request.done.set()
            if self._closing.is_set():
                return

    def _continue_dispatch(self) -> None:
        adapter = self._adapter
        if adapter is not None:
            if self._closing.is_set():
                adapter.cancel_modal()
            elif not self._requests.empty():
                adapter.wake()

    def _service_requests(self) -> None:
        if self._servicing_requests:
            return
        self._servicing_requests = True
        try:
            if not self._closing.is_set():
                self._drain_requests()
            if not self._closing.is_set():
                self._refresh()
        finally:
            self._servicing_requests = False
            self._continue_dispatch()

    def _refresh(self) -> None:
        adapter = self._adapter
        if adapter is None:
            return
        # Never take a controller snapshot while holding the lifecycle lock.
        snapshot = self._controller.snapshot()
        adapter.remember_foreground(self._control_windows())
        model = _panel_model(snapshot, transcript_visible=self._transcript_visible())
        if self._local_rejection is not None:
            generation, message = self._local_rejection
            if (
                generation != snapshot.generation
                or snapshot.armed
                or not snapshot.interface_ready
                or snapshot.state == "closed"
            ):
                self._local_rejection = None
            else:
                model = replace(model, detail=message)
        if model != self._model:
            adapter.render_panel(model)
            self._model = model
        if (
            snapshot.armed
            and snapshot.input_active
            and not self._captures
            and not self._closing.is_set()
        ):
            adapter.track_cursor(adapter.cursor_position())
            current = self._controller.snapshot()
            if self._closing.is_set() or not current.armed or not current.input_active:
                adapter.hide_cursor()
        else:
            adapter.hide_cursor()

    def _local_command(self, command: _LocalCommand) -> None:
        if command == _LocalCommand.STOP:
            self._stop_local("Stopped with the local Stop button")
            return
        if not self._ready.is_set() or self._closing.is_set() or self._captures:
            return
        snapshot = self._controller.snapshot()
        if command == _LocalCommand.ARM:
            if _panel_model(snapshot).arm_enabled:
                try:
                    self._controller.arm_local()
                except Exception as error:
                    # An arm rejection is a control outcome, not a native UI failure.
                    self._local_rejection = (
                        self._controller.snapshot().generation,
                        f"Arm not granted: {str(error) or type(error).__name__}",
                    )
                    self._refresh()
                    return
                self._local_rejection = None
                self._refresh()
                if (
                    self._controller.snapshot().armed
                    and not self._closing.is_set()
                    and self._adapter is not None
                ):
                    self._adapter.minimize_panel()
                return
        elif command == _LocalCommand.TAKEOVER:
            self._controller.set_human_takeover(not snapshot.human_takeover)
        elif command == _LocalCommand.TRANSCRIPT and self._on_transcript_toggle is not None:
            self._on_transcript_toggle()
        self._refresh()

    def _stop_local(self, reason: str) -> None:
        self._local_rejection = None
        try:
            self._controller.stop(reason)
        finally:
            if self._adapter is not None:
                self._adapter.hide_cursor()
        self._refresh()

    def _hotkey(self) -> None:
        self._stop_local("Stopped with Ctrl+Shift+H")

    def _panel_close(self) -> None:
        try:
            self._stop_local("Desktop-MCP is quitting.")
        finally:
            if self._on_exit is not None:
                self._on_exit()
            else:
                self.close()

    def _human_input(
        self,
        source: str,
        flags: int,
        extra_info: int,
        *,
        kind: Literal["move", "button", "key"],
        position: Point | None = None,
    ) -> None:
        if (
            not self._ready.is_set()
            or self._closing.is_set()
            or not _is_physical_input(source, flags, extra_info)
        ):
            return
        try:
            # The controller owns interruption policy and physical-cursor history.
            self._controller.notify_human_input(kind=kind, position=position)
        finally:
            snapshot = self._controller.snapshot()
            if self._adapter is not None and (not snapshot.armed or not snapshot.input_active):
                self._adapter.hide_cursor()

    def _record_failure(self, error: BaseException) -> None:
        with self._lock:
            first_failure = self._error is None
            if first_failure:
                self._error = error
            self._closing.set()
        if not first_failure:
            return
        # Stop never waits for the serial action lock; this is part of LocalControl.
        try:
            self._controller.stop("Local control interface failed")
        except Exception:
            logger.exception("Desktop-MCP could not report the local interface stop")
        try:
            self._controller.set_interface_ready(False, error=str(error))
        except Exception:
            logger.exception("Desktop-MCP could not report the local interface failure")
        logger.error("Desktop-MCP local control failed: %s", error)
        self._wake()

    def _run(self) -> None:
        adapter: _Win32Adapter | None = None
        try:
            self._controller.set_interface_ready(False)
            self._controller.stop("Arm / Resume enables guidance and desktop control together.")
            self._controller.set_human_takeover(True)
            if self._closing.is_set():
                return
            adapter = _Win32Adapter()
            self._adapter = adapter
            adapter.initialize(self)
            with self._lock:
                self._handles = adapter.window_handles()
            if self._closing.is_set():
                return
            self._controller.set_interface_ready(True)
            self._refresh()
            self._ready.set()
            while not self._closing.is_set():
                self._service_requests()
                if self._closing.is_set():
                    break
                adapter.pump(_REFRESH_MS)
        except Exception as error:
            self._record_failure(error)
        finally:
            self._closing.set()
            try:
                self._controller.stop("Local control interface closed")
                self._controller.set_interface_ready(
                    False, error=str(self._error) if self._error is not None else None
                )
            except Exception as error:
                self._record_failure(error)
            if adapter is not None:
                try:
                    adapter.shutdown()
                except Exception as error:
                    self._shutdown_error = error
                    self._record_failure(error)
                with self._lock:
                    self._handles = adapter.window_handles()
            self._captures.clear()
            while True:
                try:
                    request = self._requests.get_nowait()
                except queue.Empty:
                    break
                request.error = self._error or RuntimeError("The local interface is closed")
                request.done.set()
            self._closed.set()
            self._ready.set()


class _Win32Adapter:
    """Win32 implementation; constructed and used on the dedicated UI thread."""

    def __init__(self) -> None:
        import ctypes
        import os
        from ctypes import wintypes
        from types import SimpleNamespace

        if os.name != "nt":
            raise OSError("The Desktop-MCP native control surface requires Windows")

        import win32api
        import win32con
        import win32gui

        self.ctypes = ctypes
        self.wintypes = wintypes
        self.api = win32api
        self.con = win32con
        self.gui = win32gui
        self.user32 = ctypes.WinDLL("user32", use_last_error=True)
        self.gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
        self.dwmapi = ctypes.WinDLL("dwmapi", use_last_error=True)
        self._surface: ControlSurface | None = None
        self._panel = 0
        self._overlay = 0
        self._buttons: dict[_LocalCommand, int] = {}
        self._labels: dict[str, int] = {}
        self._hooks: list[int] = []
        self._hook_callbacks: list[object] = []
        self._hotkey_registered = False
        self._registered_class = False
        self._class_name = f"DesktopMCPControl_{id(self):x}"
        self._instance = int(self.api.GetModuleHandle(None))
        self._old_dpi_context = None
        self._background = 0
        self._detail_background = 0
        self._fonts: dict[str, int] = {}
        self._dpi = 96
        self._layout_scale = 1.0
        self._layout_ready = False
        self._laying_out = False
        self._sprite: CursorSprite | None = None
        self._sprite_dpi = 0
        self._cursor_visible = False
        self._cursor_at: Point | None = None
        self._capturing = False
        self._panel_restore = (False, False)
        self._view: _PanelModel | None = None
        self._destroying = False
        self._affinity: dict[int, bool] = {}
        self._wake_posted = False
        self._return_window = 0

        hook_proc = ctypes.WINFUNCTYPE(
            ctypes.c_ssize_t, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM
        )

        class KeyboardInput(ctypes.Structure):
            _fields_ = [
                ("vkCode", wintypes.DWORD),
                ("scanCode", wintypes.DWORD),
                ("flags", wintypes.DWORD),
                ("time", wintypes.DWORD),
                ("dwExtraInfo", ctypes.c_size_t),
            ]

        class MouseInput(ctypes.Structure):
            _fields_ = [
                ("pt", wintypes.POINT),
                ("mouseData", wintypes.DWORD),
                ("flags", wintypes.DWORD),
                ("time", wintypes.DWORD),
                ("dwExtraInfo", ctypes.c_size_t),
            ]

        class DrawItem(ctypes.Structure):
            _fields_ = [
                ("CtlType", wintypes.UINT),
                ("CtlID", wintypes.UINT),
                ("itemID", wintypes.UINT),
                ("itemAction", wintypes.UINT),
                ("itemState", wintypes.UINT),
                ("hwndItem", wintypes.HWND),
                ("hDC", wintypes.HDC),
                ("rcItem", wintypes.RECT),
                ("itemData", ctypes.c_size_t),
            ]

        class BitmapHeader(ctypes.Structure):
            _fields_ = [
                ("biSize", wintypes.DWORD),
                ("biWidth", wintypes.LONG),
                ("biHeight", wintypes.LONG),
                ("biPlanes", wintypes.WORD),
                ("biBitCount", wintypes.WORD),
                ("biCompression", wintypes.DWORD),
                ("biSizeImage", wintypes.DWORD),
                ("biXPelsPerMeter", wintypes.LONG),
                ("biYPelsPerMeter", wintypes.LONG),
                ("biClrUsed", wintypes.DWORD),
                ("biClrImportant", wintypes.DWORD),
            ]

        class BitmapInfo(ctypes.Structure):
            _fields_ = [("header", BitmapHeader), ("colors", wintypes.DWORD * 3)]

        class Size(ctypes.Structure):
            _fields_ = [("cx", wintypes.LONG), ("cy", wintypes.LONG)]

        class Blend(ctypes.Structure):
            _fields_ = [
                ("operation", ctypes.c_ubyte),
                ("flags", ctypes.c_ubyte),
                ("alpha", ctypes.c_ubyte),
                ("format", ctypes.c_ubyte),
            ]

        self.types = SimpleNamespace(
            HookProc=hook_proc,
            KeyboardInput=KeyboardInput,
            MouseInput=MouseInput,
            DrawItem=DrawItem,
            BitmapHeader=BitmapHeader,
            BitmapInfo=BitmapInfo,
            Size=Size,
            Blend=Blend,
        )

        def bind(
            library: object, name: str, result: object, *arguments: object, optional: bool = False
        ) -> object:
            function = getattr(library, name, None)
            if function is None:
                if optional:
                    return None
                raise OSError(f"Required Windows function is unavailable: {name}")
            function.restype = result
            function.argtypes = list(arguments)
            return function

        pointer = ctypes.POINTER
        bind(
            self.user32,
            "RegisterHotKey",
            wintypes.BOOL,
            wintypes.HWND,
            ctypes.c_int,
            wintypes.UINT,
            wintypes.UINT,
        )
        bind(self.user32, "UnregisterHotKey", wintypes.BOOL, wintypes.HWND, ctypes.c_int)
        bind(
            self.user32,
            "SetWindowsHookExW",
            wintypes.HANDLE,
            ctypes.c_int,
            hook_proc,
            wintypes.HINSTANCE,
            wintypes.DWORD,
        )
        bind(self.user32, "UnhookWindowsHookEx", wintypes.BOOL, wintypes.HANDLE)
        bind(
            self.user32,
            "CallNextHookEx",
            ctypes.c_ssize_t,
            wintypes.HANDLE,
            ctypes.c_int,
            wintypes.WPARAM,
            wintypes.LPARAM,
        )
        bind(self.user32, "GetCursorPos", wintypes.BOOL, pointer(wintypes.POINT))
        bind(
            self.user32,
            "PostMessageW",
            wintypes.BOOL,
            wintypes.HWND,
            wintypes.UINT,
            wintypes.WPARAM,
            wintypes.LPARAM,
        )
        bind(
            self.user32,
            "PeekMessageW",
            wintypes.BOOL,
            pointer(wintypes.MSG),
            wintypes.HWND,
            wintypes.UINT,
            wintypes.UINT,
            wintypes.UINT,
        )
        bind(self.user32, "TranslateMessage", wintypes.BOOL, pointer(wintypes.MSG))
        bind(self.user32, "DispatchMessageW", ctypes.c_ssize_t, pointer(wintypes.MSG))
        bind(self.user32, "IsDialogMessageW", wintypes.BOOL, wintypes.HWND, pointer(wintypes.MSG))
        bind(
            self.user32,
            "MsgWaitForMultipleObjectsEx",
            wintypes.DWORD,
            wintypes.DWORD,
            pointer(wintypes.HANDLE),
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.DWORD,
        )
        bind(self.user32, "GetDC", wintypes.HDC, wintypes.HWND)
        bind(self.user32, "ReleaseDC", ctypes.c_int, wintypes.HWND, wintypes.HDC)
        bind(
            self.user32,
            "UpdateLayeredWindow",
            wintypes.BOOL,
            wintypes.HWND,
            wintypes.HDC,
            pointer(wintypes.POINT),
            pointer(Size),
            wintypes.HDC,
            pointer(wintypes.POINT),
            wintypes.DWORD,
            pointer(Blend),
            wintypes.DWORD,
        )
        bind(self.gdi32, "CreateCompatibleDC", wintypes.HDC, wintypes.HDC)
        bind(
            self.gdi32,
            "CreateDIBSection",
            wintypes.HBITMAP,
            wintypes.HDC,
            pointer(BitmapInfo),
            wintypes.UINT,
            pointer(ctypes.c_void_p),
            wintypes.HANDLE,
            wintypes.DWORD,
        )
        bind(self.gdi32, "SelectObject", wintypes.HANDLE, wintypes.HDC, wintypes.HANDLE)
        bind(self.gdi32, "DeleteObject", wintypes.BOOL, wintypes.HANDLE)
        bind(self.gdi32, "DeleteDC", wintypes.BOOL, wintypes.HDC)
        bind(self.dwmapi, "DwmFlush", ctypes.c_long)
        bind(
            self.dwmapi,
            "DwmSetWindowAttribute",
            ctypes.c_long,
            wintypes.HWND,
            wintypes.DWORD,
            ctypes.c_void_p,
            wintypes.DWORD,
        )
        self._set_affinity = bind(
            self.user32,
            "SetWindowDisplayAffinity",
            wintypes.BOOL,
            wintypes.HWND,
            wintypes.DWORD,
            optional=True,
        )
        self._set_thread_dpi = bind(
            self.user32,
            "SetThreadDpiAwarenessContext",
            wintypes.HANDLE,
            wintypes.HANDLE,
            optional=True,
        )
        self._get_window_dpi = bind(self.user32, "GetDpiForWindow", wintypes.UINT, wintypes.HWND)
        self._adjust_rect = bind(
            self.user32,
            "AdjustWindowRectExForDpi",
            wintypes.BOOL,
            pointer(wintypes.RECT),
            wintypes.DWORD,
            wintypes.BOOL,
            wintypes.DWORD,
            wintypes.UINT,
        )
        # pywin32 retains its trampoline; keep the bound Python callback alive too.
        self._wndproc_callback = self._wndproc

    def _check(self, result: object, operation: str) -> object:
        if not result:
            code = self.ctypes.get_last_error()
            raise OSError(code, f"{operation} failed: {self.ctypes.FormatError(code).strip()}")
        return result

    def _scale(self, value: int) -> int:
        return round(value * getattr(self, "_layout_scale", self._dpi / 96))

    def _rect(self, left: int, top: int, right: int, bottom: int) -> tuple[int, ...]:
        return tuple(self._scale(value) for value in (left, top, right, bottom))

    def initialize(self, surface: ControlSurface) -> None:
        self._surface = surface
        if self._set_thread_dpi is not None:
            self._old_dpi_context = self._set_thread_dpi(self.ctypes.c_void_p(-4))
            if not self._old_dpi_context:
                self._old_dpi_context = self._set_thread_dpi(self.ctypes.c_void_p(-3))
            self._check(self._old_dpi_context, "SetThreadDpiAwarenessContext")
        else:
            raise OSError(
                "Per-monitor thread DPI awareness is required for physical cursor tracking"
            )

        pointer = self.cursor_position()
        monitor = self.api.MonitorFromPoint(pointer, self.con.MONITOR_DEFAULTTONEAREST)
        work = self.api.GetMonitorInfo(monitor)["Work"]
        self._register_window_class()
        self._fit_to_work_area(work)
        width, height = self._outer_size()
        x = work[0] + max(0, (work[2] - work[0] - width) // 2)
        y = work[1] + max(0, (work[3] - work[1] - height) // 2)
        self._panel = self.gui.CreateWindowEx(
            _PANEL_EX_STYLE,
            self._class_name,
            "Desktop-MCP · Stopped",
            _PANEL_STYLE,
            x,
            y,
            width,
            height,
            0,
            0,
            self._instance,
            None,
        )
        self._overlay = self.gui.CreateWindowEx(
            _OVERLAY_EX_STYLE,
            self._class_name,
            "Desktop-MCP cursor",
            self.con.WS_POPUP,
            0,
            0,
            1,
            1,
            0,
            0,
            self._instance,
            None,
        )
        self._dpi = self._get_window_dpi(self._panel)
        self._check(self._dpi, "GetDpiForWindow")
        self._fit_to_work_area(work)
        self._make_fonts()
        labels = {
            # Click/Space arms on release; an Alt mnemonic would re-stop on key-up.
            _LocalCommand.ARM: "Arm / Resume",
            _LocalCommand.STOP: "&Stop",
            _LocalCommand.TAKEOVER: "&Human takeover",
            _LocalCommand.TRANSCRIPT: "Transcript: On",
        }
        for command, label in labels.items():
            self._buttons[command] = self.gui.CreateWindowEx(
                0,
                "BUTTON",
                label,
                self.con.WS_CHILD
                | self.con.WS_VISIBLE
                | self.con.WS_TABSTOP
                | self.con.BS_OWNERDRAW,
                0,
                0,
                1,
                1,
                self._panel,
                int(command),
                self._instance,
                None,
            )
        for name, identifier in (("detail", 1101), ("action", 1102)):
            self._labels[name] = self.gui.CreateWindowEx(
                0,
                "STATIC",
                "",
                self.con.WS_CHILD
                | self.con.WS_VISIBLE
                | self.con.SS_LEFTNOWORDWRAP
                | self.con.SS_NOPREFIX
                | self.con.SS_ENDELLIPSIS,
                0,
                0,
                1,
                1,
                self._panel,
                identifier,
                self._instance,
                None,
            )
        self._set_label_fonts()
        self._layout_buttons()
        width, height = self._outer_size()
        self.gui.SetWindowPos(
            self._panel,
            0,
            work[0] + max(0, (work[2] - work[0] - width) // 2),
            work[1] + max(0, (work[3] - work[1] - height) // 2),
            width,
            height,
            self.con.SWP_NOACTIVATE | self.con.SWP_NOZORDER,
        )
        self._layout_ready = True
        self.gui.EnableWindow(self._buttons[_LocalCommand.ARM], False)
        for window in (self._panel, self._overlay):
            self._affinity[window] = bool(
                self._set_affinity is not None and self._set_affinity(window, 0x11)
            )
        # Affinity is best-effort; capture_guard always performs acknowledged hiding.
        dark = self.ctypes.c_int(1)
        for attribute in (20, 19):
            if (
                self.dwmapi.DwmSetWindowAttribute(
                    self._panel, attribute, self.ctypes.byref(dark), self.ctypes.sizeof(dark)
                )
                >= 0
            ):
                break
        rounded = self.ctypes.c_int(2)
        self.dwmapi.DwmSetWindowAttribute(
            self._panel, 33, self.ctypes.byref(rounded), self.ctypes.sizeof(rounded)
        )
        self._register_safety_controls()
        self.gui.ShowWindow(self._panel, self.con.SW_SHOWNOACTIVATE)

    def _register_window_class(self) -> None:
        self._background = self.gui.CreateSolidBrush(self.api.RGB(21, 21, 21))
        self._detail_background = self.gui.CreateSolidBrush(self.api.RGB(33, 33, 33))
        window_class = self.gui.WNDCLASS()
        window_class.hInstance = self._instance
        window_class.lpszClassName = self._class_name
        window_class.lpfnWndProc = self._wndproc_callback
        window_class.hCursor = self.gui.LoadCursor(0, self.con.IDC_ARROW)
        window_class.hIcon = self.gui.LoadIcon(0, self.con.IDI_APPLICATION)
        # WM_PAINT owns this brush; transferring it to the class would delete it twice.
        window_class.hbrBackground = 0
        self.gui.RegisterClass(window_class)
        self._registered_class = True

    def _register_safety_controls(self) -> None:
        if not self.user32.RegisterHotKey(self._panel, _HOTKEY_ID, _STOP_MODIFIERS, ord("H")):
            code = self.ctypes.get_last_error()
            raise OSError(
                code,
                "Cannot register global Ctrl+Shift+H; Desktop-MCP remains stopped. "
                "Another application may already own this shortcut.",
            )
        self._hotkey_registered = True
        for kind, identifier, structure in (
            ("keyboard", 13, self.types.KeyboardInput),
            ("mouse", 14, self.types.MouseInput),
        ):

            def callback(
                code: int,
                message: int,
                address: int,
                *,
                _kind: str = kind,
                _structure: object = structure,
            ) -> int:
                if code >= 0:
                    try:
                        event = self.ctypes.cast(address, self.ctypes.POINTER(_structure)).contents
                        if _kind == "mouse":
                            self._surface._human_input(
                                _kind,
                                int(event.flags),
                                int(event.dwExtraInfo),
                                kind="move" if message == 0x0200 else "button",
                                position=(int(event.pt.x), int(event.pt.y)),
                            )
                            self.wake()
                        else:
                            self._surface._human_input(
                                _kind, int(event.flags), int(event.dwExtraInfo), kind="key"
                            )
                    except Exception as error:
                        self._surface._record_failure(error)
                return self.user32.CallNextHookEx(None, code, message, address)

            reference = self.types.HookProc(callback)
            self._hook_callbacks.append(reference)
            handle = self.user32.SetWindowsHookExW(identifier, reference, self._instance, 0)
            self._check(handle, f"SetWindowsHookExW({kind})")
            self._hooks.append(handle)

    def _outer_size(self) -> tuple[int, int]:
        rectangle = self.wintypes.RECT(0, 0, self._scale(_PANEL_WIDTH), self._scale(_PANEL_HEIGHT))
        self._check(
            self._adjust_rect(
                self.ctypes.byref(rectangle), _PANEL_STYLE, False, _PANEL_EX_STYLE, self._dpi
            ),
            "AdjustWindowRectExForDpi",
        )
        return rectangle.right - rectangle.left, rectangle.bottom - rectangle.top

    def _fit_to_work_area(self, work: Rect) -> None:
        rectangle = self.wintypes.RECT()
        self._check(
            self._adjust_rect(
                self.ctypes.byref(rectangle), _PANEL_STYLE, False, _PANEL_EX_STYLE, self._dpi
            ),
            "AdjustWindowRectExForDpi",
        )
        self._layout_scale = _fit_layout_scale(
            work,
            self._dpi,
            (rectangle.right - rectangle.left, rectangle.bottom - rectangle.top),
        )

    def _work_area(self, rectangle: Rect | None = None) -> Rect:
        if rectangle is None:
            monitor = self.api.MonitorFromWindow(self._panel, self.con.MONITOR_DEFAULTTONEAREST)
        else:
            monitor = self.api.MonitorFromRect(rectangle, self.con.MONITOR_DEFAULTTONEAREST)
        return self.api.GetMonitorInfo(monitor)["Work"]

    def _reflow_panel(self, work: Rect, origin: Point | None = None) -> None:
        if not self._layout_ready or self._laying_out:
            return
        self._laying_out = True
        try:
            previous_scale = self._layout_scale
            self._fit_to_work_area(work)
            if self._layout_scale != previous_scale:
                self._make_fonts()
            width, height = self._outer_size()
            if origin is None:
                rectangle = self.gui.GetWindowRect(self._panel)
                origin = rectangle[0], rectangle[1]
            x = max(work[0], min(origin[0], work[2] - width))
            y = max(work[1], min(origin[1], work[3] - height))
            self.gui.SetWindowPos(
                self._panel,
                0,
                x,
                y,
                width,
                height,
                self.con.SWP_NOACTIVATE | self.con.SWP_NOZORDER,
            )
            self._layout_buttons()
            self.gui.InvalidateRect(self._panel, None, False)
        finally:
            self._laying_out = False

    def _set_label_fonts(self) -> None:
        for name, handle in self._labels.items():
            role = "small" if name == "detail" else "body"
            self.gui.SendMessage(handle, self.con.WM_SETFONT, self._fonts[role], True)

    def _make_fonts(self) -> None:
        old_fonts = self._fonts
        self._fonts = {}
        try:
            for role, height, weight in (
                ("title", 23, 600),
                ("heading", 16, 600),
                ("body", 14, 400),
                ("small", 12, 400),
                ("button", 14, 600),
            ):
                description = self.gui.LOGFONT()
                description.lfFaceName = "Segoe UI"
                description.lfHeight = -self._scale(height)
                description.lfWeight = weight
                description.lfQuality = self.con.CLEARTYPE_QUALITY
                self._fonts[role] = self.gui.CreateFontIndirect(description)
            self._set_label_fonts()
        finally:
            for font in old_fonts.values():
                self.gui.DeleteObject(font)

    def _layout_buttons(self) -> None:
        for command, rectangle in (
            (_LocalCommand.ARM, (22, 224, 284, 264)),
            (_LocalCommand.STOP, (296, 224, 434, 264)),
            (_LocalCommand.TAKEOVER, (22, 277, 434, 302)),
            (_LocalCommand.TRANSCRIPT, (276, 20, 434, 48)),
        ):
            left, top, right, bottom = self._rect(*rectangle)
            self.gui.SetWindowPos(
                self._buttons[command],
                0,
                left,
                top,
                right - left,
                bottom - top,
                self.con.SWP_NOACTIVATE | self.con.SWP_NOZORDER,
            )
        for name, rectangle in (
            ("detail", (36, 120, 420, 145)),
            ("action", (23, 191, 434, 213)),
        ):
            left, top, right, bottom = self._rect(*rectangle)
            self.gui.SetWindowPos(
                self._labels[name],
                0,
                left,
                top,
                right - left,
                bottom - top,
                self.con.SWP_NOACTIVATE | self.con.SWP_NOZORDER,
            )

    def window_handles(self) -> tuple[int, ...]:
        return tuple(
            handle
            for handle in (
                self._panel,
                self._overlay,
                *self._buttons.values(),
                *self._labels.values(),
            )
            if handle
        )

    def wake(self) -> None:
        panel = self._panel
        if panel and not self._wake_posted:
            self._wake_posted = True
            if not self.user32.PostMessageW(panel, _WAKE_MESSAGE, 0, 0):
                self._wake_posted = False
                if not self._destroying:
                    self._check(False, "PostMessageW")

    def cancel_modal(self) -> None:
        if self._panel:
            self.gui.SendMessage(self._panel, self.con.WM_CANCELMODE, 0, 0)

    def pump(self, timeout_ms: int) -> None:
        result = self.user32.MsgWaitForMultipleObjectsEx(0, None, timeout_ms, 0x04FF, 0x0004)
        if result == 0xFFFFFFFF:
            self._check(False, "MsgWaitForMultipleObjectsEx")
        message = self.wintypes.MSG()
        for _ in range(256):
            if not self.user32.PeekMessageW(self.ctypes.byref(message), None, 0, 0, 1):
                break
            if message.message == self.con.WM_QUIT:
                self._surface.close()
                break
            if not self.user32.IsDialogMessageW(self._panel, self.ctypes.byref(message)):
                self.user32.TranslateMessage(self.ctypes.byref(message))
                self.user32.DispatchMessageW(self.ctypes.byref(message))
            if self._surface._closing.is_set():
                break

    def _wndproc(self, hwnd: int, message: int, wparam: int, lparam: int) -> int:
        try:
            if message == _WAKE_MESSAGE:
                self._wake_posted = False
                # Windows also dispatches this inside title-bar and menu modal loops.
                self._surface._service_requests()
                return 0
            if hwnd == self._overlay:
                if message == self.con.WM_NCHITTEST:
                    return self.con.HTTRANSPARENT
                if message == self.con.WM_MOUSEACTIVATE:
                    return self.con.MA_NOACTIVATE
            if hwnd == self._panel:
                if message == self.con.WM_CTLCOLORSTATIC and lparam in self._labels.values():
                    detail = lparam == self._labels["detail"]
                    grey, background = (170, 33) if detail else (215, 21)
                    self.gui.SetTextColor(wparam, self.api.RGB(grey, grey, grey))
                    self.gui.SetBkColor(wparam, self.api.RGB(background, background, background))
                    return self._detail_background if detail else self._background
                if message in (self.con.WM_SIZE, self.con.WM_DISPLAYCHANGE, _EXIT_SIZE_MOVE) or (
                    message == self.con.WM_SETTINGCHANGE and wparam == self.con.SPI_SETWORKAREA
                ):
                    if self._layout_ready and not self.gui.IsIconic(self._panel):
                        self._reflow_panel(self._work_area())
                    return 0
                if message == self.con.WM_CLOSE:
                    self._surface._panel_close()
                    return 0
                if message == self.con.WM_HOTKEY and wparam == _HOTKEY_ID:
                    self._surface._hotkey()
                    return 0
                if message == self.con.WM_COMMAND and (wparam >> 16) == self.con.BN_CLICKED:
                    command = _LocalCommand._value2member_map_.get(wparam & 0xFFFF)
                    if command is not None and self._buttons.get(command) == lparam:
                        self._surface._local_command(command)
                    return 0
                if message == self.con.WM_PAINT:
                    self._paint()
                    return 0
                if message == self.con.WM_ERASEBKGND:
                    return 1
                if message == self.con.WM_DRAWITEM:
                    item = self.ctypes.cast(
                        lparam, self.ctypes.POINTER(self.types.DrawItem)
                    ).contents
                    self._draw_button(item)
                    return 1
                if message == self.con.WM_DESTROY and not self._destroying:
                    self._surface.close()
                    return 0
                if message == self.con.WM_QUERYENDSESSION:
                    self._surface._stop_local("Windows session is ending")
                    return 1
                if message == self.con.WM_ENDSESSION and wparam:
                    self._surface.close()
                    return 0
            if message == 0x02E0:  # WM_DPICHANGED
                self._sprite_dpi = 0
                if hwnd == self._panel:
                    self._dpi = wparam & 0xFFFF
                    suggested = self.ctypes.cast(
                        lparam, self.ctypes.POINTER(self.wintypes.RECT)
                    ).contents
                    self._reflow_panel(
                        self._work_area(
                            (suggested.left, suggested.top, suggested.right, suggested.bottom)
                        ),
                        (suggested.left, suggested.top),
                    )
                return 0
            return self.gui.DefWindowProc(hwnd, message, wparam, lparam)
        except Exception as error:
            self._surface._record_failure(error)
            return 0

    def render_panel(self, model: _PanelModel) -> None:
        self._view = model
        self.gui.SetWindowText(self._panel, f"Desktop-MCP · {model.state.title()}")
        self.gui.SetWindowText(
            self._buttons[_LocalCommand.TAKEOVER],
            f"&Pause on interruption: {'On' if model.human_takeover else 'Off'}",
        )
        self.gui.SetWindowText(
            self._buttons[_LocalCommand.TRANSCRIPT],
            f"Transcript: {'On' if model.transcript_visible else 'Off'}",
        )
        self.gui.SetWindowText(self._labels["detail"], model.detail)
        self.gui.SetWindowText(self._labels["action"], f"Current action: {model.action}")
        self.gui.EnableWindow(self._buttons[_LocalCommand.ARM], model.arm_enabled)
        self.gui.EnableWindow(self._buttons[_LocalCommand.STOP], model.stop_enabled)
        self.gui.EnableWindow(self._buttons[_LocalCommand.TAKEOVER], model.settings_enabled)
        self.gui.EnableWindow(self._buttons[_LocalCommand.TRANSCRIPT], model.settings_enabled)
        self.gui.InvalidateRect(self._panel, None, False)
        for button in self._buttons.values():
            self.gui.InvalidateRect(button, None, False)
        for label in self._labels.values():
            self.gui.InvalidateRect(label, None, True)

    def _text(
        self,
        dc: int,
        text: str,
        rectangle: tuple[int, ...],
        *,
        role: str = "body",
        grey: int = 225,
        flags: int | None = None,
    ) -> None:
        if flags is None:
            flags = self.con.DT_LEFT | self.con.DT_SINGLELINE | self.con.DT_END_ELLIPSIS
        previous = self.gui.SelectObject(dc, self._fonts[role])
        try:
            self.gui.SetBkMode(dc, self.con.TRANSPARENT)
            self.gui.SetTextColor(dc, self.api.RGB(grey, grey, grey))
            self.gui.DrawText(dc, text, -1, rectangle, flags | self.con.DT_NOPREFIX)
        finally:
            self.gui.SelectObject(dc, previous)

    def _rounded_box(self, dc: int, rectangle: tuple[int, ...], grey: int, radius: int = 9) -> None:
        with ExitStack() as resources:
            brush = self.gui.CreateSolidBrush(self.api.RGB(grey, grey, grey))
            resources.callback(self.gui.DeleteObject, brush)
            pen = self.gui.CreatePen(self.con.PS_SOLID, 1, self.api.RGB(grey, grey, grey))
            resources.callback(self.gui.DeleteObject, pen)
            previous_brush = self.gui.SelectObject(dc, brush)
            resources.callback(self.gui.SelectObject, dc, previous_brush)
            previous_pen = self.gui.SelectObject(dc, pen)
            resources.callback(self.gui.SelectObject, dc, previous_pen)
            self.gui.RoundRect(dc, *rectangle, self._scale(radius * 2), self._scale(radius * 2))

    def _paint(self) -> None:
        dc, paint = self.gui.BeginPaint(self._panel)
        try:
            self.gui.FillRect(dc, self.gui.GetClientRect(self._panel), self._background)
            if self._view is None:
                return
            self._text(dc, "Desktop-MCP", self._rect(22, 17, 242, 46), role="title")
            self._text(
                dc,
                "GUIDE, EXPLAIN & ACT  /  ONE SESSION",
                self._rect(23, 49, 434, 68),
                role="small",
                grey=145,
            )
            self._rounded_box(dc, self._rect(22, 78, 434, 155), 33, radius=11)
            self._text(dc, self._view.heading, self._rect(36, 91, 420, 113), role="heading")
            self._text(dc, "CURRENT ACTION", self._rect(23, 170, 434, 188), role="small", grey=135)
            self._text(dc, _SHORTCUT_HINT, self._rect(23, 321, 434, 341), role="small", grey=158)
        finally:
            self.gui.EndPaint(self._panel, paint)

    def _draw_button(self, item: object) -> None:
        if self._view is None:
            return
        command = _LocalCommand(item.CtlID)
        rc = item.rcItem
        rectangle = (rc.left, rc.top, rc.right, rc.bottom)
        self.gui.FillRect(item.hDC, rectangle, self._background)
        flags = self.con.DT_CENTER | self.con.DT_VCENTER | self.con.DT_SINGLELINE
        if command == _LocalCommand.TRANSCRIPT:
            self._rounded_box(item.hDC, rectangle, 65 if self._view.transcript_visible else 42, 7)
            self._text(
                item.hDC,
                f"Transcript: {'On' if self._view.transcript_visible else 'Off'}",
                rectangle,
                role="small",
                flags=flags,
            )
        elif command == _LocalCommand.TAKEOVER:
            checked = self._view.human_takeover
            self._rounded_box(item.hDC, self._rect(1, 5, 17, 21), 188 if checked else 65, 3)
            if checked:
                self._text(
                    item.hDC, "✓", self._rect(1, 3, 18, 23), role="small", grey=20, flags=flags
                )
            self._text(
                item.hDC,
                "Pause when I interrupt automated input",
                (self._scale(27), rc.top, rc.right, rc.bottom),
                role="small",
                grey=195,
                flags=self.con.DT_VCENTER | self.con.DT_SINGLELINE,
            )
        else:
            disabled = bool(item.itemState & self.con.ODS_DISABLED)
            selected = bool(item.itemState & self.con.ODS_SELECTED)
            if command == _LocalCommand.ARM:
                fill, ink = (48, 120) if disabled else (205 if selected else 232, 21)
                label = "Arm / Resume"
            else:
                fill, ink = (48 if selected else 65, 235)
                label = "Stop"
            self._rounded_box(item.hDC, rectangle, fill)
            self._text(item.hDC, label, rectangle, role="button", grey=ink, flags=flags)
        if item.itemState & self.con.ODS_FOCUS:
            inset = self._scale(3)
            self.gui.DrawFocusRect(
                item.hDC, (rc.left + inset, rc.top + inset, rc.right - inset, rc.bottom - inset)
            )

    def cursor_position(self) -> Point:
        point = self.wintypes.POINT()
        self._check(self.user32.GetCursorPos(self.ctypes.byref(point)), "GetCursorPos")
        return point.x, point.y

    def track_cursor(self, point: Point) -> None:
        if self._capturing:
            return
        if point != self._cursor_at or self._sprite is None:
            x, y = self._sprite.hotspot if self._sprite is not None else (0, 0)
            self.gui.SetWindowPos(
                self._overlay,
                self.con.HWND_TOPMOST,
                point[0] - x,
                point[1] - y,
                0,
                0,
                self.con.SWP_NOSIZE | self.con.SWP_NOACTIVATE,
            )
        # GetDpiForMonitor is process-awareness-dependent. Query the actual
        # per-monitor-aware overlay window after positioning it instead.
        dpi = self._get_window_dpi(self._overlay)
        self._check(dpi, "GetDpiForWindow(cursor)")
        if self._sprite is None or self._sprite_dpi != dpi:
            self._sprite = render_cursor(dpi)
            self._sprite_dpi = dpi
            self._upload_cursor(point, self._sprite)
        if not self._cursor_visible:
            self.gui.ShowWindow(self._overlay, self.con.SW_SHOWNOACTIVATE)
            self._cursor_visible = True
        self._cursor_at = point

    def hide_cursor(self) -> None:
        if self._overlay and self._cursor_visible:
            self.gui.ShowWindow(self._overlay, self.con.SW_HIDE)
            self._cursor_visible = False

    def _upload_cursor(self, point: Point, sprite: CursorSprite) -> None:
        ctypes = self.ctypes
        with ExitStack() as resources:
            screen_dc = self.user32.GetDC(None)
            self._check(screen_dc, "GetDC")
            resources.callback(
                lambda: self._check(self.user32.ReleaseDC(None, screen_dc), "ReleaseDC")
            )
            memory_dc = self.gdi32.CreateCompatibleDC(screen_dc)
            self._check(memory_dc, "CreateCompatibleDC")
            resources.callback(lambda: self._check(self.gdi32.DeleteDC(memory_dc), "DeleteDC"))
            info = self.types.BitmapInfo()
            info.header.biSize = ctypes.sizeof(self.types.BitmapHeader)
            info.header.biWidth, height = sprite.image.size
            info.header.biHeight = -height
            info.header.biPlanes = 1
            info.header.biBitCount = 32
            pixels = ctypes.c_void_p()
            bitmap = self.gdi32.CreateDIBSection(
                screen_dc, ctypes.byref(info), 0, ctypes.byref(pixels), None, 0
            )
            self._check(bitmap, "CreateDIBSection")
            resources.callback(
                lambda: self._check(self.gdi32.DeleteObject(bitmap), "DeleteObject(cursor bitmap)")
            )
            self._check(pixels.value, "CreateDIBSection pixels")
            data = premultiplied_bgra(sprite.image)
            ctypes.memmove(pixels, data, len(data))
            previous = self.gdi32.SelectObject(memory_dc, bitmap)
            self._check(previous, "SelectObject")
            resources.callback(
                lambda: self._check(
                    self.gdi32.SelectObject(memory_dc, previous), "Restore cursor bitmap selection"
                )
            )
            destination = self.wintypes.POINT(
                point[0] - sprite.hotspot[0], point[1] - sprite.hotspot[1]
            )
            size = self.types.Size(*sprite.image.size)
            origin = self.wintypes.POINT(0, 0)
            blend = self.types.Blend(0, 0, 255, 1)
            self._check(
                self.user32.UpdateLayeredWindow(
                    self._overlay,
                    screen_dc,
                    ctypes.byref(destination),
                    ctypes.byref(size),
                    memory_dc,
                    ctypes.byref(origin),
                    0,
                    ctypes.byref(blend),
                    2,
                ),
                "UpdateLayeredWindow",
            )

    def hide_for_capture(self) -> None:
        self._panel_restore = (
            bool(self.gui.IsWindowVisible(self._panel)),
            bool(self.gui.IsIconic(self._panel)),
        )
        self._capturing = True
        self.hide_cursor()
        self.gui.ShowWindow(self._panel, self.con.SW_HIDE)
        result = self.dwmapi.DwmFlush()
        if result < 0:
            raise OSError(f"DwmFlush failed before capture (HRESULT {result:#x})")

    def restore_after_capture(self) -> None:
        if not self._capturing:
            return
        self._capturing = False
        visible, minimized = self._panel_restore
        if visible:
            self.gui.ShowWindow(
                self._panel,
                self.con.SW_SHOWMINNOACTIVE if minimized else self.con.SW_SHOWNOACTIVATE,
            )

    def show_panel(self) -> None:
        self.gui.ShowWindow(self._panel, self.con.SW_SHOWNOACTIVATE)

    def remember_foreground(self, control_windows: tuple[int, ...]) -> None:
        foreground = self.gui.GetForegroundWindow()
        if foreground and foreground not in control_windows:
            self._return_window = foreground

    def minimize_panel(self) -> None:
        if self._capturing:
            self._panel_restore = (True, True)
        else:
            self.gui.ShowWindow(self._panel, self.con.SW_MINIMIZE)
            own = self._surface._control_windows()
            target = self._return_window
            foreground = self.gui.GetForegroundWindow()
            if (
                target
                and target not in own
                and self.gui.IsWindow(target)
                and (not foreground or foreground in own)
            ):
                self.gui.SetForegroundWindow(target)

    def shutdown(self) -> None:
        self._destroying = True
        errors = []

        def clean(operation: object, *arguments: object) -> bool:
            try:
                operation(*arguments)
                return True
            except Exception as error:
                errors.append(error)
                return False

        for hook in tuple(self._hooks):
            if clean(
                lambda handle: self._check(
                    self.user32.UnhookWindowsHookEx(handle), "UnhookWindowsHookEx"
                ),
                hook,
            ):
                self._hooks.remove(hook)
        if self._hotkey_registered:
            if clean(
                lambda: self._check(
                    self.user32.UnregisterHotKey(self._panel, _HOTKEY_ID), "UnregisterHotKey"
                )
            ):
                self._hotkey_registered = False
        if self._overlay and clean(self.gui.DestroyWindow, self._overlay):
            self._overlay = 0
            self._cursor_visible = False
        if self._panel and clean(self.gui.DestroyWindow, self._panel):
            self._panel = 0
            self._buttons.clear()
            self._labels.clear()
        if self._registered_class and not self._panel and not self._overlay:
            if clean(self.gui.UnregisterClass, self._class_name, self._instance):
                self._registered_class = False
        for role, font in tuple(self._fonts.items()):
            if clean(self.gui.DeleteObject, font):
                del self._fonts[role]
        if self._background and not self._registered_class:
            if clean(self.gui.DeleteObject, self._background):
                self._background = 0
        if self._detail_background and not self._registered_class:
            if clean(self.gui.DeleteObject, self._detail_background):
                self._detail_background = 0
        if self._old_dpi_context:
            if clean(
                lambda: self._check(
                    self._set_thread_dpi(self._old_dpi_context), "Restore thread DPI awareness"
                )
            ):
                self._old_dpi_context = None
        if errors:
            raise ExceptionGroup("Native control resource cleanup failed", errors)
