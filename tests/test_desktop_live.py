"""Opt-in native exercise confined to a newly created, harmless fixture window."""

from __future__ import annotations

import base64
import ctypes
import io
import os
from pathlib import Path
import threading
import time
import uuid

import pytest

pytestmark = pytest.mark.skipif(
    os.getenv("DESKTOP_MCP_LIVE") != "1",
    reason="Real desktop input requires the explicit, isolated live-fixture opt-in.",
)


class FixtureWindow:
    def __init__(self):
        self.ready = threading.Event()
        self.closed = threading.Event()
        self.hwnd = None
        self.editor = None
        self.error = None
        self.thread = threading.Thread(target=self._run, name="Desktop-MCP fixture")

    def _run(self):
        import win32api
        import win32con
        import win32gui

        instance = win32api.GetModuleHandle(None)
        name = f"DesktopMCPFixture{uuid.uuid4().hex}"
        brush = win32gui.CreateSolidBrush(win32api.RGB(242, 242, 242))
        registered = False
        try:

            def procedure(hwnd, message, wparam, lparam):
                if message == win32con.WM_PAINT:
                    dc, paint = win32gui.BeginPaint(hwnd)
                    try:
                        win32gui.FillRect(dc, win32gui.GetClientRect(hwnd), brush)
                        win32gui.SetBkMode(dc, win32con.TRANSPARENT)
                        win32gui.SetTextColor(dc, win32api.RGB(32, 32, 32))
                        win32gui.DrawText(
                            dc,
                            "Desktop-MCP isolated input fixture",
                            -1,
                            (24, 20, 670, 60),
                            win32con.DT_LEFT,
                        )
                        win32gui.DrawText(
                            dc,
                            "Only this temporary window receives test input.",
                            -1,
                            (24, 330, 670, 375),
                            win32con.DT_LEFT,
                        )
                    finally:
                        win32gui.EndPaint(hwnd, paint)
                    return 0
                if message == win32con.WM_CLOSE:
                    self.closed.set()
                    return 0
                return win32gui.DefWindowProc(hwnd, message, wparam, lparam)

            self._procedure = procedure
            cls = win32gui.WNDCLASS()
            cls.hInstance = instance
            cls.lpszClassName = name
            cls.lpfnWndProc = procedure
            cls.hbrBackground = brush
            win32gui.RegisterClass(cls)
            registered = True
            self.hwnd = win32gui.CreateWindowEx(
                win32con.WS_EX_TOPMOST,
                name,
                "Desktop-MCP isolated fixture",
                win32con.WS_OVERLAPPEDWINDOW | win32con.WS_VISIBLE,
                70,
                70,
                720,
                470,
                0,
                0,
                instance,
                None,
            )
            self.editor = win32gui.CreateWindowEx(
                win32con.WS_EX_CLIENTEDGE,
                "EDIT",
                "",
                win32con.WS_CHILD
                | win32con.WS_VISIBLE
                | win32con.WS_TABSTOP
                | win32con.ES_MULTILINE
                | win32con.ES_AUTOVSCROLL,
                24,
                78,
                650,
                228,
                self.hwnd,
                1,
                instance,
                None,
            )
            self.ready.set()
            while not self.closed.wait(0.004):
                win32gui.PumpWaitingMessages()
        except Exception as error:
            self.error = error
            self.ready.set()
        finally:
            if self.hwnd and win32gui.IsWindow(self.hwnd):
                win32gui.DestroyWindow(self.hwnd)
            if registered:
                win32gui.UnregisterClass(name, instance)
            win32gui.DeleteObject(brush)

    def start(self):
        self.thread.start()
        assert self.ready.wait(5), "Fixture window did not start"
        if self.error:
            raise self.error

    def close(self):
        self.closed.set()
        self.thread.join(5)
        assert not self.thread.is_alive(), "Fixture window did not shut down"


def wait_until(predicate, timeout=3):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    assert predicate(), "The expected native state did not arrive before the deadline"


def save_own_window(handle, path: Path, *, overlay_handles=()):
    """Capture only an opaque inset of an explicitly owned window."""
    import mss
    from PIL import Image
    import win32con
    import win32gui
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.SetWindowDisplayAffinity.argtypes = [wintypes.HWND, wintypes.DWORD]
    user32.SetWindowDisplayAffinity.restype = wintypes.BOOL
    user32.GetWindowDisplayAffinity.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user32.GetWindowDisplayAffinity.restype = wintypes.BOOL
    handles = (handle, *overlay_handles)
    original_affinity = {}
    try:
        for own_handle in handles:
            affinity = wintypes.DWORD()
            assert user32.GetWindowDisplayAffinity(own_handle, ctypes.byref(affinity))
            original_affinity[own_handle] = affinity.value
            assert user32.SetWindowDisplayAffinity(own_handle, 0)
        win32gui.SetWindowPos(
            handle,
            win32con.HWND_TOPMOST,
            0,
            0,
            0,
            0,
            win32con.SWP_NOMOVE | win32con.SWP_NOSIZE | win32con.SWP_NOACTIVATE,
        )
        win32gui.RedrawWindow(
            handle,
            None,
            None,
            win32con.RDW_INVALIDATE | win32con.RDW_UPDATENOW | win32con.RDW_ALLCHILDREN,
        )
        time.sleep(0.1)
        left, top = win32gui.ClientToScreen(handle, (0, 0))
        _, _, width, height = win32gui.GetClientRect(handle)
        # Keep rounded frame corners and everything behind the window out of the image.
        region = {"left": left + 12, "top": top + 12, "width": width - 24, "height": height - 24}
        with mss.mss() as capture:
            pixels = capture.grab(region)
            image = Image.frombytes("RGB", pixels.size, pixels.rgb)
        image.save(path, format="PNG")
    finally:
        for own_handle, affinity in original_affinity.items():
            if win32gui.IsWindow(own_handle):
                user32.SetWindowDisplayAffinity(own_handle, affinity)


async def test_native_control_input_images_and_global_stop(monkeypatch):
    from fastmcp import Client
    from PIL import Image
    import win32api
    import win32con
    import win32gui

    from desktop_mcp.actions import Action, key_code
    from desktop_mcp.app import DesktopApplication, create_server
    from desktop_mcp.runtime import BatchInterrupted

    monkeypatch.setenv("WINDOWS_MCP_SCREENSHOT_BACKEND", "mss")
    monkeypatch.setenv("DESKTOP_MCP_IMAGE_FILES", "false")
    application = DesktopApplication()
    fixture = FixtureWindow()
    artifacts = os.getenv("DESKTOP_MCP_LIVE_ARTIFACTS")
    artifact_root = Path(artifacts) if artifacts else None
    if artifact_root:
        artifact_root.mkdir(parents=True, exist_ok=False)
    previous_pointer = win32api.GetCursorPos()
    try:
        async with Client(create_server(application)) as client:
            status = await client.call_tool("DesktopStatus")
            assert status.data["state"] == "stopped"
            handles = application.surface.window_handles()
            main_window = next(
                handle
                for handle in handles
                if not win32gui.GetWindowLong(handle, win32con.GWL_EXSTYLE)
                & win32con.WS_EX_TRANSPARENT
            )
            assert win32gui.IsWindowVisible(main_window)
            assert not (
                win32gui.GetWindowLong(main_window, win32con.GWL_EXSTYLE)
                & win32con.WS_EX_TOOLWINDOW
            ), "The control panel must be an Alt-Tab application window"
            if artifact_root:
                save_own_window(main_window, artifact_root / "control-window.png")
            fixture.start()
            # This trusted fixture, not an MCP tool, locally allows its own exercise.
            application.controller.arm_local()
            for handle in application.surface.window_handles():
                if (
                    not win32gui.GetWindowLong(handle, win32con.GWL_EXSTYLE)
                    & win32con.WS_EX_TRANSPARENT
                ):
                    win32gui.ShowWindow(handle, win32con.SW_MINIMIZE)
            win32gui.SetForegroundWindow(fixture.hwnd)
            wait_until(lambda: win32gui.GetForegroundWindow() == fixture.hwnd)
            point = win32gui.ClientToScreen(fixture.editor, (30, 30))
            content = "Desktop-MCP fixture: \u03bb \U0001f369\nSecond line."
            result = await client.call_tool(
                "Type",
                {
                    "loc": list(point),
                    "text": content,
                    "observe": False,
                },
            )
            assert not result.is_error
            wait_until(
                lambda: win32gui.GetWindowText(fixture.editor).replace("\r\n", "\n") == content
            )
            frame = await client.call_tool("Screenshot", {"settle": 0, "encoding": "png"})
            images = [block for block in frame.content if block.type == "image"]
            assert len(images) == 1
            decoded = Image.open(io.BytesIO(base64.b64decode(images[0].data)))
            assert decoded.width > 200 and decoded.height > 100
            if artifact_root:
                save_own_window(
                    fixture.hwnd,
                    artifact_root / "visible-cursor.png",
                    overlay_handles=tuple(
                        handle
                        for handle in application.surface.window_handles()
                        if win32gui.GetWindowLong(handle, win32con.GWL_EXSTYLE)
                        & win32con.WS_EX_TRANSPARENT
                    ),
                )
            stopped = []
            drag_started = threading.Event()
            target = win32gui.ClientToScreen(fixture.hwnd, (580, 350))

            def long_drag():
                try:
                    with application.controller.operation("Live fixture drag"):
                        drag_started.set()
                        application.controller.execute(
                            [Action(kind="drag", loc=target, button="middle", duration=2.0)],
                            window_id=fixture.hwnd,
                        )
                except BatchInterrupted:
                    stopped.append(True)

            worker = threading.Thread(target=long_drag, name="Desktop-MCP live drag")
            worker.start()
            assert drag_started.wait(2)
            wait_until(lambda: win32api.GetAsyncKeyState(win32con.VK_MBUTTON) & 0x8000)
            started = time.monotonic()
            # Send just the harmless registered stop chord outside the controller so
            # this exercises the real Windows hotkey, not its software reservation.
            codes = [key_code(key) for key in ("ctrl", "shift", "h")]
            try:
                for code in codes:
                    application.backend.key(code, True)
            finally:
                for code in reversed(codes):
                    application.backend.key(code, False)
            wait_until(lambda: not application.controller.snapshot().armed, timeout=1.0)
            stop_latency = time.monotonic() - started
            worker.join(2)
            assert not worker.is_alive()
            assert stopped
            assert not win32api.GetAsyncKeyState(win32con.VK_MBUTTON) & 0x8000
            denied = await client.call_tool("Type", {"text": "must not type"}, raise_on_error=False)
            assert denied.is_error
            denied_capture = await client.call_tool("Screenshot", {}, raise_on_error=False)
            assert denied_capture.is_error
            print(f"Native hotkey stop latency: {stop_latency:.3f}s")
    finally:
        application.controller.stop("Live fixture ended.")
        if fixture.thread.ident is not None:
            fixture.close()
        # Restore only the pointer position changed by this specifically owned exercise.
        win32api.SetCursorPos(previous_pointer)
