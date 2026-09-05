"""Fast contextual captures through Windows-MCP's existing backend chain."""

from __future__ import annotations

import ctypes
from collections.abc import Callable
from contextlib import AbstractContextManager, nullcontext
from ctypes import wintypes
import time

from desktop_mcp.contracts import CaptureContext, CaptureScope, RawCapture, Rect


def context_identity(context: CaptureContext) -> tuple:
    return context.window_id, context.bounds, context.desktop_bounds, context.display_bounds


class WindowsCapture:
    """Avoid UIA tree extraction on the normal screenshot path."""

    def __init__(
        self,
        *,
        capture_guard: Callable[[], AbstractContextManager] = nullcontext,
        control_windows: Callable[[], tuple[int, ...]] = lambda: (),
    ) -> None:
        import windows_mcp.uia as uia
        from windows_mcp.desktop import screenshot
        from windows_mcp.desktop.utils import repair_surrogates

        self._uia = uia
        self._capture_backend = screenshot
        self._capture_guard = capture_guard
        self._control_windows = control_windows
        self._repair_text = repair_surrogates
        self._user32 = ctypes.WinDLL("user32", use_last_error=True)
        self._user32.GetForegroundWindow.restype = wintypes.HWND
        self._user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
        self._user32.GetWindowRect.restype = wintypes.BOOL
        self._user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
        self._user32.GetWindowTextLengthW.restype = ctypes.c_int
        self._user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        self._user32.GetWindowTextW.restype = ctypes.c_int
        self.last_backend: str | None = None

    def context(self, scope: CaptureScope = "active") -> CaptureContext:
        if scope not in {"active", "desktop"}:
            raise ValueError("scope must be active or desktop.")
        left, top, width, height = self._uia.GetVirtualScreenRect()
        desktop = (left, top, left + width, top + height)
        if width <= 0 or height <= 0:
            raise RuntimeError("No interactive Windows desktop is available.")
        handle = int(self._user32.GetForegroundWindow() or 0)
        title = ""
        if handle:
            length = self._user32.GetWindowTextLengthW(handle)
            buffer = ctypes.create_unicode_buffer(length + 1)
            self._user32.GetWindowTextW(handle, buffer, len(buffer))
            title = self._repair_text(buffer.value)
        bounds = desktop
        if scope == "active":
            if not handle:
                raise RuntimeError("No foreground window is available; request scope='desktop'.")
            if handle in self._control_windows():
                raise RuntimeError(
                    "Minimize the Desktop-MCP control window before observing an app."
                )
            rect = wintypes.RECT()
            if not self._user32.GetWindowRect(handle, ctypes.byref(rect)):
                raise ctypes.WinError(ctypes.get_last_error())
            bounds = (
                max(left, rect.left),
                max(top, rect.top),
                min(left + width, rect.right),
                min(top + height, rect.bottom),
            )
            if bounds[2] <= bounds[0] or bounds[3] <= bounds[1]:
                raise RuntimeError("The foreground window has no visible desktop area.")
        displays = tuple(
            (display.rect.left, display.rect.top, display.rect.right, display.rect.bottom)
            for display in self._uia.GetDisplays()
        )
        return CaptureContext(handle, bounds, desktop, title, displays)

    def capture(self, *, scope: CaptureScope = "active", region: Rect | None = None) -> RawCapture:
        before = self.context(scope)
        bounds = before.bounds if region is None else region
        if len(bounds) != 4 or any(
            isinstance(value, bool) or not isinstance(value, int) for value in bounds
        ):
            raise ValueError("region must be four integer physical desktop coordinates.")
        left, top, right, bottom = bounds
        desktop = before.desktop_bounds
        if not (
            desktop[0] <= left < right <= desktop[2] and desktop[1] <= top < bottom <= desktop[3]
        ):
            raise ValueError("The requested capture region is outside the virtual desktop.")
        if not any(
            max(left, rect[0]) < min(right, rect[2]) and max(top, rect[1]) < min(bottom, rect[3])
            for rect in before.display_bounds
        ):
            raise ValueError("The capture region does not intersect a connected monitor.")
        with self._capture_guard():
            image, self.last_backend = self._capture_backend.capture(self._uia.Rect(*bounds))
            captured_at = time.monotonic()
        after = self.context(scope)
        if context_identity(before) != context_identity(after):
            raise RuntimeError(
                "The window or display layout changed during capture. Observe again."
            )
        if image.size != (right - left, bottom - top):
            raise RuntimeError("The capture backend returned unexpected image dimensions.")
        return RawCapture(image, bounds, after, captured_at)
