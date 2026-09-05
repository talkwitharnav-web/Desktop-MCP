"""Checked SendInput calls using the retained Windows-MCP Win32 primitives."""

from __future__ import annotations

import ctypes
from collections.abc import Callable, Sequence
from ctypes import wintypes
from pathlib import Path
import subprocess

from desktop_mcp.actions import Button
from desktop_mcp.contracts import INJECTED_INPUT_TAG, Point, Rect

_BUTTON_FLAGS: dict[Button, tuple[int, int, int]] = {
    "left": (0x0002, 0x0004, 0),
    "right": (0x0008, 0x0010, 0),
    "middle": (0x0020, 0x0040, 0),
    "x1": (0x0080, 0x0100, 1),
    "x2": (0x0080, 0x0100, 2),
}
_EXTENDED_KEYS = {
    0x21,
    0x22,
    0x23,
    0x24,
    0x25,
    0x26,
    0x27,
    0x28,
    0x2C,
    0x2D,
    0x2E,
    0x5B,
    0x5C,
    0x5D,
    0x6F,
    0x90,
    0xA3,
    0xA5,
}


def contains(rect: Rect, point: Point) -> bool:
    return rect[0] <= point[0] < rect[2] and rect[1] <= point[1] < rect[3]


def normalize_absolute(point: Point, bounds: Rect) -> Point:
    """SendInput uses 65536 bins across the entire virtual desktop."""
    width, height = bounds[2] - bounds[0], bounds[3] - bounds[1]
    if width <= 0 or height <= 0 or not contains(bounds, point):
        raise ValueError("The pointer coordinate is outside the virtual desktop.")
    return (
        min(65535, ((2 * (point[0] - bounds[0]) + 1) * 65536) // (2 * width)),
        min(65535, ((2 * (point[1] - bounds[1]) + 1) * 65536) // (2 * height)),
    )


class WindowsInput:
    """Fast native input with checked return values and injected-event tagging."""

    def __init__(self, *, control_windows: Callable[[], tuple[int, ...]] = lambda: ()) -> None:
        import windows_mcp.uia as uia
        from windows_mcp.uia.enums import INPUT

        self._uia = uia
        self._input_type = INPUT
        self._control_windows = control_windows
        self._pending_releases = []
        self._user32 = ctypes.WinDLL("user32", use_last_error=True)
        self._user32.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int]
        self._user32.SendInput.restype = wintypes.UINT
        self._user32.GetForegroundWindow.restype = wintypes.HWND
        self._user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
        self._user32.GetWindowRect.restype = wintypes.BOOL
        self._user32.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
        self._user32.GetWindowLongW.restype = wintypes.LONG
        self._user32.IsWindowVisible.argtypes = [wintypes.HWND]
        self._user32.IsWindowVisible.restype = wintypes.BOOL
        self._user32.IsIconic.argtypes = [wintypes.HWND]
        self._user32.IsIconic.restype = wintypes.BOOL
        self._user32.SetForegroundWindow.argtypes = [wintypes.HWND]
        self._user32.SetForegroundWindow.restype = wintypes.BOOL
        self._user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
        self._user32.ShowWindow.restype = wintypes.BOOL
        self._user32.IsWindow.argtypes = [wintypes.HWND]
        self._user32.IsWindow.restype = wintypes.BOOL

    def set_control_windows(self, getter: Callable[[], tuple[int, ...]]) -> None:
        self._control_windows = getter

    def bounds(self) -> Rect:
        left, top, width, height = self._uia.GetVirtualScreenRect()
        return left, top, left + width, top + height

    def displays(self) -> tuple[Rect, ...]:
        return tuple(
            (display.rect.left, display.rect.top, display.rect.right, display.rect.bottom)
            for display in self._uia.GetDisplays()
        )

    def position(self) -> Point:
        return tuple(self._uia.GetCursorPos())

    def foreground(self) -> int:
        return int(self._user32.GetForegroundWindow() or 0)

    def validate_point(self, point: Point) -> None:
        if len(point) != 2 or any(
            isinstance(value, bool) or not isinstance(value, int) for value in point
        ):
            raise ValueError("A coordinate must contain exactly two integer physical pixels.")
        if not any(contains(rect, point) for rect in self.displays()):
            raise ValueError(
                "The coordinate is outside the connected monitors or in a monitor gap."
            )

    def ensure_target(self, point: Point | None = None, window_id: int | None = None) -> None:
        foreground = self.foreground()
        if window_id is not None and foreground != window_id:
            raise RuntimeError("The foreground window changed. Obtain a fresh screenshot.")
        for handle in self._control_windows():
            if not self._user32.IsWindowVisible(handle) or self._user32.IsIconic(handle):
                continue
            if self._user32.GetWindowLongW(handle, -20) & 0x20:
                continue  # The no-activate, click-through cursor is not an input target.
            if point is None and foreground == handle:
                raise RuntimeError("Desktop-MCP cannot type into its own control window.")
            if point is not None:
                rect = wintypes.RECT()
                if not self._user32.GetWindowRect(handle, ctypes.byref(rect)):
                    raise ctypes.WinError(ctypes.get_last_error())
                if contains((rect.left, rect.top, rect.right, rect.bottom), point):
                    raise RuntimeError("Minimize the control window before clicking underneath it.")

    def _stamp(self, event, *, keyboard: bool = False):
        payload = event.union.ki if keyboard else event.union.mi
        payload.dwExtraInfo = ctypes.cast(
            ctypes.c_void_p(INJECTED_INPUT_TAG), ctypes.POINTER(wintypes.ULONG)
        )
        return event

    def _send(self, events: Sequence) -> None:
        if not events:
            return
        array = (self._input_type * len(events))(*events)
        sent = self._user32.SendInput(len(events), array, ctypes.sizeof(self._input_type))
        if sent != len(events):
            error = ctypes.get_last_error()
            held = {}
            for event in events[:sent]:
                if event.type != 1:
                    continue
                key = event.union.ki
                identity = (key.wVk, key.wScan, key.dwFlags & 0x0004)
                if key.dwFlags & 0x0002:
                    held.pop(identity, None)
                else:
                    held[identity] = event
            for event in reversed(tuple(held.values())):
                release = self._input_type.from_buffer_copy(event)
                release.union.ki.dwFlags |= 0x0002
                self._pending_releases.append(release)
            self.release_pending()
            raise OSError(
                error,
                f"Windows accepted {sent} of {len(events)} input events. "
                "Input may be blocked by a locked or elevated desktop; do not replay blindly.",
            )

    def release_pending(self) -> None:
        """Complete key-up events if Windows accepted only part of a text packet."""
        if not self._pending_releases:
            return
        events = (self._input_type * len(self._pending_releases))(*self._pending_releases)
        sent = self._user32.SendInput(
            len(self._pending_releases), events, ctypes.sizeof(self._input_type)
        )
        del self._pending_releases[:sent]
        if self._pending_releases:
            raise OSError("Windows could not release all partially emitted text input.")

    def move(self, point: Point) -> None:
        x, y = normalize_absolute(point, self.bounds())
        self._send([self._stamp(self._uia.MouseInput(x, y, dwFlags=0xC001))])

    def button(self, button: Button, down: bool) -> None:
        down_flag, up_flag, data = _BUTTON_FLAGS[button]
        event = self._uia.MouseInput(0, 0, mouseData=data, dwFlags=down_flag if down else up_flag)
        self._send([self._stamp(event)])

    def key(self, code: int, down: bool) -> None:
        flags = (0x0001 if code in _EXTENDED_KEYS else 0) | (0 if down else 0x0002)
        self._send([self._stamp(self._uia.KeyboardInput(code, 0, flags), keyboard=True)])

    def text(self, text: str) -> None:
        events = []
        for character in text:
            if character in {"\n", "\t"}:
                code = 0x0D if character == "\n" else 0x09
                events.extend(
                    self._stamp(self._uia.KeyboardInput(code, 0, flags), keyboard=True)
                    for flags in (0, 0x0002)
                )
            else:
                encoded = character.encode("utf-16-le")
                for offset in range(0, len(encoded), 2):
                    code = int.from_bytes(encoded[offset : offset + 2], "little")
                    events.extend(
                        self._stamp(self._uia.KeyboardInput(0, code, flags), keyboard=True)
                        for flags in (0x0004, 0x0006)
                    )
        self._send(events)

    def wheel(self, delta_x: int, delta_y: int) -> None:
        events = []
        if delta_y:
            events.append(self._stamp(self._uia.MouseInput(0, 0, delta_y & 0xFFFFFFFF, 0x0800)))
        if delta_x:
            events.append(self._stamp(self._uia.MouseInput(0, 0, delta_x & 0xFFFFFFFF, 0x1000)))
        self._send(events)

    def focus(self, window_id: int) -> None:
        if window_id in self._control_windows():
            raise ValueError("The agent cannot target its own control interface.")
        if not self._user32.IsWindow(window_id):
            raise ValueError("The requested window no longer exists.")
        if self._user32.IsIconic(window_id):
            self._user32.ShowWindow(window_id, 9)
        if not self._user32.SetForegroundWindow(window_id):
            raise OSError("Windows refused the focus change. Use a fresh screenshot or Alt+Tab.")

    @staticmethod
    def launch(executable: str, args: list[str]) -> int:
        path = Path(executable)
        if (
            not path.is_absolute()
            or not path.is_file()
            or path.suffix.casefold() not in {".exe", ".com"}
        ):
            raise ValueError("App launch requires an existing absolute .exe or .com path.")
        process = subprocess.Popen(
            [str(path.resolve()), *args],
            shell=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
        )
        return process.pid
