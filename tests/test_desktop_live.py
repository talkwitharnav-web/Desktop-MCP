"""Opt-in native exercise confined to a newly created, harmless fixture window."""

from __future__ import annotations

import base64
import asyncio
import ctypes
import io
import json
import os
from pathlib import Path
import threading
import time
import uuid

import pytest
from tests.desktop_live_fixture import IsolatedFixtureWindow, owned_window_pid

pytestmark = pytest.mark.skipif(
    os.getenv("DESKTOP_MCP_LIVE") != "1",
    reason="Real desktop input requires the explicit, isolated live-fixture opt-in.",
)

_WM_MOUSEHWHEEL = 0x020E
_WM_XBUTTONDOWN, _WM_XBUTTONUP = 0x020B, 0x020C


class FixtureWindow:
    def __init__(self):
        self.ready = threading.Event()
        self.painted = threading.Event()
        self.closed = threading.Event()
        self.hwnd = None
        self.editor = None
        self.error = None
        self.events = []
        self.thread = threading.Thread(target=self._run, name="Desktop-MCP fixture")

    def _run(self):
        import win32api
        import win32con
        import win32gui

        user32 = ctypes.WinDLL("user32", use_last_error=True)
        user32.SetThreadDpiAwarenessContext.argtypes = [ctypes.c_void_p]
        user32.SetThreadDpiAwarenessContext.restype = ctypes.c_void_p
        previous_dpi = user32.SetThreadDpiAwarenessContext(ctypes.c_void_p(-4))
        assert previous_dpi
        instance = win32api.GetModuleHandle(None)
        monitor = win32api.MonitorFromPoint(
            win32api.GetCursorPos(), win32con.MONITOR_DEFAULTTONEAREST
        )
        left, top, right, bottom = win32api.GetMonitorInfo(monitor)["Work"]
        name = f"DesktopMCPFixture{uuid.uuid4().hex}"
        brush = win32gui.CreateSolidBrush(win32api.RGB(242, 242, 242))
        registered = False
        try:

            def handle_message(hwnd, message, wparam, lparam):
                if message in (
                    win32con.WM_MOUSEWHEEL,
                    _WM_MOUSEHWHEEL,
                    win32con.WM_MBUTTONDOWN,
                    win32con.WM_MBUTTONUP,
                    _WM_XBUTTONDOWN,
                    _WM_XBUTTONUP,
                ):
                    self.events.append((message, wparam, lparam))
                    return 0
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
                    self.painted.set()
                    return 0
                if message == win32con.WM_CLOSE:
                    self.closed.set()
                    return 0
                return win32gui.DefWindowProc(hwnd, message, wparam, lparam)

            def procedure(hwnd, message, wparam, lparam):
                if self.error is None:
                    try:
                        return handle_message(hwnd, message, wparam, lparam)
                    except Exception as error:
                        self.error = error
                        self.closed.set()
                return win32gui.DefWindowProc(hwnd, message, wparam, lparam)

            self._procedure = procedure
            cls = win32gui.WNDCLASS()
            cls.hInstance = instance
            cls.lpszClassName = name
            cls.lpfnWndProc = procedure
            cls.hbrBackground = 0
            win32gui.RegisterClass(cls)
            registered = True
            self.hwnd = win32gui.CreateWindowEx(
                win32con.WS_EX_TOPMOST | win32con.WS_EX_APPWINDOW,
                name,
                "Desktop-MCP isolated fixture",
                win32con.WS_POPUP | win32con.WS_VISIBLE,
                left,
                top,
                right - left,
                bottom - top,
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
                | win32con.WS_VSCROLL
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
            assert user32.SetThreadDpiAwarenessContext(previous_dpi)

    def start(self):
        self.thread.start()
        assert self.ready.wait(5), "Fixture window did not start"
        if self.error:
            raise self.error
        assert self.painted.wait(3), "Fixture did not paint an opaque owned background"
        if self.error:
            raise self.error

    def close(self):
        self.closed.set()
        self.thread.join(5)
        assert not self.thread.is_alive(), "Fixture window did not shut down"
        if self.error:
            raise self.error


def wait_until(predicate, timeout=3):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    assert predicate(), "The expected native state did not arrive before the deadline"


def focus_owned_window(handle):
    import pywintypes
    import win32gui

    owned_window_pid(handle)
    if win32gui.GetForegroundWindow() != handle:
        try:
            win32gui.SetForegroundWindow(handle)
        except pywintypes.error:
            if win32gui.GetForegroundWindow() not in (0, handle):
                raise
            wait_until(lambda: win32gui.GetForegroundWindow() == handle, timeout=0.5)
    wait_until(lambda: win32gui.GetForegroundWindow() == handle)


def own_capture_bounds(handle):
    import win32gui

    left, top = win32gui.ClientToScreen(handle, (0, 0))
    _, _, width, height = win32gui.GetClientRect(handle)
    assert width > 48 and height > 48
    return left + 12, top + 12, left + width - 12, top + height - 12


def assert_owned_region(handle, bounds, *, allowed_above=()):
    import win32gui

    above = []
    win32gui.EnumWindows(lambda window, _: above.append(window) or True, None)
    assert handle in above
    for window in above[: above.index(handle)]:
        if (
            window in allowed_above
            or not win32gui.IsWindowVisible(window)
            or win32gui.IsIconic(window)
        ):
            continue
        left, top, right, bottom = win32gui.GetWindowRect(window)
        assert not (
            max(bounds[0], left) < min(bounds[2], right)
            and max(bounds[1], top) < min(bounds[3], bottom)
        ), (
            "An unowned window overlaps the fixture; refusing to capture its pixels "
            f"(class={win32gui.GetClassName(window)}, window={window}, "
            f"target={handle}, target_style={win32gui.GetWindowLong(handle, -20):#x})"
        )


def click_local_button(parent, prefix):
    import win32con
    import win32gui

    children = []
    win32gui.EnumChildWindows(parent, lambda handle, _: children.append(handle) or True, None)
    matches = [
        handle
        for handle in children
        if win32gui.GetClassName(handle).casefold() == "button"
        and win32gui.GetWindowText(handle).replace("&", "").casefold().startswith(prefix.casefold())
    ]
    assert len(matches) == 1, f"Expected one local {prefix!r} button"
    win32gui.SendMessage(matches[0], win32con.BM_CLICK, 0, 0)


def arm_fixture_locally(panel):
    import win32con
    import win32gui

    button = win32gui.GetDlgItem(panel, 1001)
    assert win32gui.GetClassName(button).casefold() == "button"
    wait_until(lambda: win32gui.IsWindowEnabled(button))
    focus_owned_window(panel)
    win32gui.SendMessage(button, win32con.BM_CLICK, 0, 0)


def own_ui_modal_flags(panel):
    from ctypes import wintypes
    import win32process

    class GuiThreadInfo(ctypes.Structure):
        _fields_ = [
            ("size", wintypes.DWORD),
            ("flags", wintypes.DWORD),
            ("active", wintypes.HWND),
            ("focus", wintypes.HWND),
            ("capture", wintypes.HWND),
            ("menu_owner", wintypes.HWND),
            ("move_size", wintypes.HWND),
            ("caret", wintypes.HWND),
            ("caret_rect", wintypes.RECT),
        ]

    info = GuiThreadInfo(size=ctypes.sizeof(GuiThreadInfo))
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.GetGUIThreadInfo.argtypes = [wintypes.DWORD, ctypes.POINTER(GuiThreadInfo)]
    user32.GetGUIThreadInfo.restype = wintypes.BOOL
    thread, _ = win32process.GetWindowThreadProcessId(panel)
    assert user32.GetGUIThreadInfo(thread, ctypes.byref(info))
    return info.flags


def open_own_ui_system_menu(panel):
    import win32con
    import win32gui

    if own_ui_modal_flags(panel) & 0x04:
        return
    win32gui.ShowWindow(panel, win32con.SW_SHOWNOACTIVATE)
    focus_owned_window(panel)
    win32gui.PostMessage(panel, win32con.WM_SYSCOMMAND, win32con.SC_KEYMENU, ord(" "))
    wait_until(lambda: own_ui_modal_flags(panel) & 0x04)


def press_fixture_stop_hotkey(application, foreground):
    import win32api
    import win32gui

    from desktop_mcp.actions import key_code

    codes = [key_code(key) for key in ("ctrl", "shift", "h")]
    assert all(not win32api.GetAsyncKeyState(code) & 0x8000 for code in codes)
    pressed = []
    try:
        for code in codes:
            assert win32gui.GetForegroundWindow() == foreground, "Fixture lost foreground"
            pressed.append(code)
            application.backend.key(code, True)
    finally:
        for code in reversed(pressed):
            application.backend.key(code, False)


def save_own_window(
    handle,
    path: Path,
    *,
    backdrop,
    overlay_handles=(),
    restore_foreground=None,
    activate=True,
):
    """Capture an owned surface only above a full, opaque, owned backing window."""
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
    was_topmost = bool(
        win32gui.GetWindowLong(handle, win32con.GWL_EXSTYLE) & win32con.WS_EX_TOPMOST
    )
    original_affinity = {}
    try:
        owned_window_pid(backdrop)
        assert win32gui.IsWindowVisible(backdrop) and not win32gui.IsIconic(backdrop)
        backdrop_affinity = wintypes.DWORD()
        assert user32.GetWindowDisplayAffinity(backdrop, ctypes.byref(backdrop_affinity))
        assert backdrop_affinity.value == 0
        win32gui.ShowWindow(handle, win32con.SW_RESTORE if activate else win32con.SW_SHOWNOACTIVATE)
        wait_until(lambda: win32gui.IsWindowVisible(handle) and not win32gui.IsIconic(handle))
        for own_handle in handles:
            owned_window_pid(own_handle)
            affinity = wintypes.DWORD()
            assert user32.GetWindowDisplayAffinity(own_handle, ctypes.byref(affinity))
            original_affinity[own_handle] = affinity.value
            assert user32.SetWindowDisplayAffinity(own_handle, 0)
        if activate:
            focus_owned_window(handle)
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
        for overlay in overlay_handles:
            if win32gui.IsWindowVisible(overlay):
                win32gui.SetWindowPos(
                    overlay,
                    win32con.HWND_TOPMOST,
                    0,
                    0,
                    0,
                    0,
                    win32con.SWP_NOMOVE | win32con.SWP_NOSIZE | win32con.SWP_NOACTIVATE,
                )
        time.sleep(0.1)
        # Keep rounded frame corners and everything behind the window out of the image.
        left, top, right, bottom = own_capture_bounds(handle)
        safe = own_capture_bounds(backdrop)
        assert safe[0] <= left < right <= safe[2] and safe[1] <= top < bottom <= safe[3]
        assert_owned_region(handle, (left, top, right, bottom), allowed_above=overlay_handles)
        assert_owned_region(
            backdrop, (left, top, right, bottom), allowed_above=(handle, *overlay_handles)
        )
        dwm = ctypes.WinDLL("dwmapi", use_last_error=True)
        dwm.DwmFlush.restype = ctypes.c_long
        assert dwm.DwmFlush() >= 0
        region = {"left": left, "top": top, "width": right - left, "height": bottom - top}
        with mss.mss() as capture:
            pixels = capture.grab(region)
            image = Image.frombytes("RGB", pixels.size, pixels.rgb)
        if handle != backdrop:
            assert sum(image.convert("L").histogram()[:80]) > image.width * image.height / 4, (
                "The owned dark interface was excluded or not painted; refusing an invalid artifact "
                f"(window={handle}, rect={win32gui.GetWindowRect(handle)}, "
                f"hit={win32gui.WindowFromPoint(((left + right) // 2, (top + bottom) // 2))})"
            )
        image.save(path, format="PNG")
    finally:
        for own_handle, affinity in original_affinity.items():
            if win32gui.IsWindow(own_handle):
                user32.SetWindowDisplayAffinity(own_handle, affinity)
        if not was_topmost and win32gui.IsWindow(handle):
            win32gui.SetWindowPos(
                handle,
                win32con.HWND_NOTOPMOST,
                0,
                0,
                0,
                0,
                win32con.SWP_NOMOVE | win32con.SWP_NOSIZE | win32con.SWP_NOACTIVATE,
            )
        if (
            restore_foreground is not None
            and win32gui.IsWindow(restore_foreground)
            and win32gui.GetForegroundWindow() == handle
        ):
            owned_window_pid(restore_foreground)
            focus_owned_window(restore_foreground)


async def exercise_native_teaching(client, application, fixture, main_window, artifact_root):
    import win32api
    import win32con
    import win32gui

    from tests.test_desktop_launch_live import chat_message_controls

    assert win32gui.GetForegroundWindow() == fixture.hwnd
    win32gui.ShowWindow(main_window, win32con.SW_SHOWNOACTIVATE)
    assert not application.controller.snapshot().armed
    arm_fixture_locally(main_window)
    wait_until(lambda: application.controller.snapshot().armed)
    wait_until(lambda: win32gui.IsIconic(main_window))
    wait_until(lambda: win32gui.GetForegroundWindow() == fixture.hwnd)

    transcript = application.teaching_surface._panel
    editor = application.teaching_surface._history_window
    assert win32gui.IsWindowVisible(transcript)
    assert not win32gui.GetWindowLong(transcript, win32con.GWL_EXSTYLE) & win32con.WS_EX_TOOLWINDOW
    assert win32gui.GetClassName(editor).startswith("DesktopMCPChat-")
    pointer = win32api.GetCursorPos()
    note = "Move your cursor into the marked area. Proximity does not mean you clicked."
    result = await client.call_tool(
        "Transcript", {"title": "Isolated teaching fixture", "text": note}
    )
    assert not result.is_error
    wait_until(lambda: win32gui.IsWindowVisible(transcript))
    wait_until(
        lambda: any(
            text == note for _, text in chat_message_controls(editor, expected_pid=os.getpid())
        )
    )
    messages = chat_message_controls(editor, expected_pid=os.getpid())
    assert all(
        win32gui.GetWindowLong(handle, win32con.GWL_STYLE) & win32con.ES_READONLY
        for handle, _ in messages
    )
    assert win32gui.GetForegroundWindow() == fixture.hwnd
    assert win32api.GetCursorPos() == pointer
    click_local_button(transcript, "Pin")
    assert win32gui.GetWindowLong(transcript, win32con.GWL_EXSTYLE) & win32con.WS_EX_TOPMOST
    pinned = await client.call_tool("Transcript", {"action": "back"}, raise_on_error=False)
    assert pinned.is_error
    click_local_button(transcript, "Unpin")
    focus_owned_window(fixture.hwnd)
    await client.call_tool("Transcript", {"action": "front"})
    assert win32gui.GetForegroundWindow() == fixture.hwnd
    if artifact_root:
        save_own_window(
            transcript,
            artifact_root / "teaching-transcript.png",
            backdrop=fixture.hwnd,
            restore_foreground=fixture.hwnd,
        )

    combined_text = "Guidance and input in one session."
    combined = await client.call_tool(
        "Type",
        {
            "text": combined_text,
            "loc": list(win32gui.ClientToScreen(fixture.editor, (30, 40))),
            "observe": False,
        },
    )
    assert not combined.is_error
    wait_until(lambda: combined_text in fixture.text())
    before_text = fixture.text()
    pointer = win32api.GetCursorPos()
    assert application.controller.snapshot().armed

    top_left = win32gui.ClientToScreen(fixture.hwnd, (30, 322))
    bottom_right = win32gui.ClientToScreen(fixture.hwnd, (660, 405))
    bounds = (*top_left, *bottom_right)
    assert_owned_region(fixture.hwnd, bounds, allowed_above=application.window_handles())
    baseline = await client.call_tool(
        "Screenshot", {"region": list(bounds), "settle": 0, "encoding": "png"}
    )
    frame_id = baseline.structured_content["frame_id"]
    baseline_image = next(block.data for block in baseline.content if block.type == "image")
    ink = await client.call_tool(
        "Draw",
        {
            "kind": "ellipse",
            "points": [[90, 24], [230, 65]],
            "color": "#ffb454",
            "frame_id": frame_id,
        },
    )
    laser = await client.call_tool(
        "Laser", {"bounds": [260, 24, 395, 65], "duration": 3.0, "frame_id": frame_id}
    )
    assert not ink.is_error and not laser.is_error
    canvas = application.teaching_surface._canvas
    wait_until(lambda: win32gui.IsWindowVisible(canvas))
    assert win32api.GetCursorPos() == pointer, "Presentation moved the real pointer"
    assert win32gui.GetForegroundWindow() == fixture.hwnd
    if artifact_root:
        save_own_window(
            fixture.hwnd,
            artifact_root / "teaching-ink-and-laser.png",
            backdrop=fixture.hwnd,
            overlay_handles=tuple(
                handle
                for handle in application.window_handles()
                if win32gui.GetWindowLong(handle, win32con.GWL_EXSTYLE) & win32con.WS_EX_TRANSPARENT
            ),
        )
    clean = await client.call_tool(
        "Screenshot", {"region": list(bounds), "settle": 0, "encoding": "png"}
    )
    clean_image = next(block.data for block in clean.content if block.type == "image")
    assert clean_image == baseline_image, "Guidance pixels leaked into an application screenshot"
    wait_until(lambda: win32gui.IsWindowVisible(canvas))
    erased = await client.call_tool("Erase", {"identifier": ink.data["identifier"]})
    assert erased.data["removed"] == 1
    await client.call_tool("Erase")
    assert not application.teaching.snapshot().marks
    assert fixture.text() == before_text

    target = win32gui.ClientToScreen(fixture.hwnd, (180, 365))
    generation = application.controller.snapshot().generation
    revision = application.controller.input_revision
    waiting = asyncio.create_task(
        client.call_tool(
            "WaitForCursor", {"loc": list(target), "radius": 20.0, "dwell": 0.2, "timeout": 3.0}
        )
    )
    await asyncio.sleep(0.15)
    assert application.teaching.snapshot().waiting is not None
    # Trusted fixture emulation of learner motion, outside the model's input path.
    win32api.SetCursorPos(target)
    reached = await waiting
    assert reached.data["status"] == "reached"
    assert application.controller.snapshot().generation == generation
    assert application.controller.input_revision == revision
    assert application.controller.snapshot().armed
    cursor = await client.call_tool("Cursor")
    assert cursor.data["position"] == list(target)
    await client.call_tool(
        "Draw", {"kind": "path", "points": [list(target), [target[0] + 60, target[1]]]}
    )
    assert application.teaching.snapshot().marks
    click_local_button(transcript, "Stop")
    wait_until(lambda: not application.controller.snapshot().armed)
    wait_until(lambda: not application.teaching.snapshot().marks)
    wait_until(lambda: not win32gui.IsWindowVisible(canvas))
    assert fixture.text() == before_text

    win32gui.ShowWindow(main_window, win32con.SW_SHOWNOACTIVATE)
    arm_fixture_locally(main_window)
    wait_until(lambda: application.controller.snapshot().armed)
    wait_until(lambda: win32gui.IsIconic(main_window))
    wait_until(lambda: win32gui.GetForegroundWindow() == fixture.hwnd)
    waiting = asyncio.create_task(
        client.call_tool(
            "WaitForCursor",
            {"loc": [target[0] + 180, target[1]], "radius": 20.0, "dwell": 0.2, "timeout": 3.0},
            raise_on_error=False,
        )
    )
    try:
        await asyncio.sleep(0.15)
        assert application.teaching.snapshot().waiting is not None
        started = time.monotonic()
        press_fixture_stop_hotkey(application, fixture.hwnd)
        wait_until(lambda: not application.controller.snapshot().armed, timeout=1.0)
        assert "Ctrl+Shift+H" in application.controller.snapshot().reason
        assert (await waiting).is_error
        assert application.teaching.snapshot().waiting is None
        assert fixture.text() == before_text
        print(f"Native teaching hotkey stop latency: {time.monotonic() - started:.3f}s")
    finally:
        if not waiting.done():
            waiting.cancel()
        await asyncio.gather(waiting, return_exceptions=True)
    print(
        "Native teaching: local mode/allow, transcript/pin/focus, ink/laser/capture exclusion, dwell and both stops passed"
    )


def enable_owned_appearance_capture(monkeypatch):
    from ctypes import wintypes

    load_library = ctypes.WinDLL

    class VisibleCaptureLibrary:
        def __init__(self, library):
            self._library = library
            native_affinity = library.SetWindowDisplayAffinity
            native_affinity.argtypes = [wintypes.HWND, wintypes.DWORD]
            native_affinity.restype = wintypes.BOOL
            self.SetWindowDisplayAffinity = lambda window, affinity: native_affinity(window, 0)

        def __getattr__(self, name):
            return getattr(self._library, name)

    def load_visible_library(name, *args, **kwargs):
        library = load_library(name, *args, **kwargs)
        if str(name).casefold() in {"user32", "user32.dll"}:
            return VisibleCaptureLibrary(library)
        return library

    monkeypatch.setattr(ctypes, "WinDLL", load_visible_library)


async def test_native_control_input_images_and_global_stop(monkeypatch):
    from fastmcp import Client
    from PIL import Image
    import pywintypes
    import win32api
    import win32con
    import win32gui
    import win32process

    from desktop_mcp.actions import Action
    from desktop_mcp.app import DesktopApplication, create_server
    from desktop_mcp.runtime import BatchInterrupted
    from desktop_mcp.window_targets import GUIThreadInfo

    capture_affinity = os.getenv("DESKTOP_MCP_LIVE_APPEARANCE") != "1"
    if not capture_affinity:
        enable_owned_appearance_capture(monkeypatch)

    monkeypatch.setenv(
        "WINDOWS_MCP_SCREENSHOT_BACKEND", os.getenv("DESKTOP_MCP_LIVE_BACKEND", "mss")
    )
    monkeypatch.setenv("DESKTOP_MCP_IMAGE_FILES", "false")
    application = DesktopApplication()
    fixture = IsolatedFixtureWindow()
    artifacts = os.getenv("DESKTOP_MCP_LIVE_ARTIFACTS")
    variant = "default-affinity" if capture_affinity else "visible-appearance"
    artifact_root = Path(artifacts) / variant if artifacts else None
    appearance_root = artifact_root if not capture_affinity else None
    if artifact_root:
        artifact_root.mkdir(parents=True, exist_ok=False)
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.SetThreadDpiAwarenessContext.argtypes = [ctypes.c_void_p]
    user32.SetThreadDpiAwarenessContext.restype = ctypes.c_void_p
    previous_dpi = user32.SetThreadDpiAwarenessContext(ctypes.c_void_p(-4))
    assert previous_dpi
    previous_pointer = win32api.GetCursorPos()
    pointer_changed = False
    fixture_state = {"pid": os.getpid(), "previous_pointer": list(previous_pointer)}
    if artifact_root:
        (artifact_root / "fixture-state.json").write_text(
            json.dumps(fixture_state), encoding="utf-8"
        )
    workers = []
    try:
        fixture.start()
        fixture_state.update(
            fixture_pid=fixture.pid,
            fixture_launcher_pid=fixture.process.pid,
            fixture_window=fixture.hwnd,
            fixture_editor=fixture.editor,
        )
        if artifact_root:
            (artifact_root / "fixture-state.json").write_text(
                json.dumps(fixture_state), encoding="utf-8"
            )
        try:
            focus_owned_window(fixture.hwnd)
        except pywintypes.error as error:
            if error.args[:2] == (0, "SetForegroundWindow"):
                pytest.skip(
                    "Windows refused foreground for the owned fixture before any Arm or input; "
                    "physical input coverage is unavailable, not a pass."
                )
            raise
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
            fixture_state["control_window"] = main_window
            if artifact_root:
                (artifact_root / "fixture-state.json").write_text(
                    json.dumps(fixture_state), encoding="utf-8"
                )
            assert win32gui.IsWindowVisible(main_window)
            assert not (
                win32gui.GetWindowLong(main_window, win32con.GWL_EXSTYLE)
                & win32con.WS_EX_TOOLWINDOW
            ), "The control panel must be an Alt-Tab application window"
            if appearance_root:
                save_own_window(
                    main_window,
                    appearance_root / "control-window.png",
                    backdrop=fixture.hwnd,
                    restore_foreground=fixture.hwnd,
                )
            fixture_state["fixture_window"] = fixture.hwnd
            if artifact_root:
                (artifact_root / "fixture-state.json").write_text(
                    json.dumps(fixture_state), encoding="utf-8"
                )
            focus_owned_window(fixture.hwnd)
            point = win32gui.ClientToScreen(fixture.editor, (30, 30))
            pointer_changed = True
            win32api.SetCursorPos(point)
            time.sleep(0.05)
            native_target = application.backend.ensure_target
            safe_bounds = own_capture_bounds(fixture.hwnd)

            def fixture_only_target(point=None, window_id=None):
                assert application.backend.foreground() == fixture.hwnd, "Fixture lost foreground"
                if point is not None:
                    assert safe_bounds[0] <= point[0] < safe_bounds[2]
                    assert safe_bounds[1] <= point[1] < safe_bounds[3]
                native_target(point, window_id=fixture.hwnd)

            monkeypatch.setattr(application.backend, "ensure_target", fixture_only_target)
            # Exercise the actual local button, never an MCP arming method.
            arm_fixture_locally(main_window)
            wait_until(lambda: application.controller.snapshot().armed)
            wait_until(lambda: win32gui.IsIconic(main_window))
            wait_until(lambda: win32gui.GetForegroundWindow() == fixture.hwnd)
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
            wait_until(lambda: fixture.text().replace("\r\n", "\n") == content)
            await client.call_tool("Click", {"loc": list(point), "clicks": 2, "observe": False})
            thread = win32process.GetWindowThreadProcessId(fixture.hwnd)[0]
            focused = GUIThreadInfo(cbSize=ctypes.sizeof(GUIThreadInfo))
            assert application.backend._user32.GetGUIThreadInfo(thread, ctypes.byref(focused))
            assert focused.hwndFocus == fixture.editor
            content = "Edited fixture: \u03bb \U0001f369\nEdit focus checked before replacement."
            await client.call_tool("Type", {"text": content, "clear": True, "observe": False})
            wait_until(lambda: fixture.text().replace("\r\n", "\n") == content)
            bounds = own_capture_bounds(fixture.hwnd)
            assert_owned_region(fixture.hwnd, bounds, allowed_above=application.window_handles())
            frame = await client.call_tool(
                "Screenshot", {"settle": 0, "encoding": "png", "region": list(bounds)}
            )
            images = [block for block in frame.content if block.type == "image"]
            assert len(images) == 1
            decoded = Image.open(io.BytesIO(base64.b64decode(images[0].data)))
            assert decoded.width > 200 and decoded.height > 100
            assert frame.structured_content["observation"]["capture_bounds"] == list(bounds)
            print(f"Native screenshot backend used: {application.capture.last_backend}")
            if artifact_root:
                decoded.save(artifact_root / "mcp-fixture.png", format="PNG")
            start = win32gui.ClientToScreen(fixture.hwnd, (160, 340))
            target = win32gui.ClientToScreen(fixture.hwnd, (590, 340))
            await client.call_tool("Move", {"loc": list(start), "observe": False})
            moving = asyncio.create_task(
                client.call_tool("Move", {"loc": list(target), "duration": 1.2, "observe": False})
            )
            samples = []
            for _ in range(12):
                await asyncio.sleep(0.06)
                assert win32gui.GetForegroundWindow() == fixture.hwnd
                samples.append(win32api.GetCursorPos())
            await moving
            assert len(set(samples)) >= 6, "Native motion jumped instead of visibly moving"
            assert all(start[0] <= point[0] <= target[0] for point in samples)
            assert win32api.GetCursorPos() == target
            if appearance_root:
                save_own_window(
                    fixture.hwnd,
                    appearance_root / "visible-cursor.png",
                    backdrop=fixture.hwnd,
                    overlay_handles=tuple(
                        handle
                        for handle in application.surface.window_handles()
                        if win32gui.GetWindowLong(handle, win32con.GWL_EXSTYLE)
                        & win32con.WS_EX_TRANSPARENT
                    ),
                )
            for button in ("middle", "x1", "x2"):
                await client.call_tool(
                    "Click", {"loc": list(target), "button": button, "observe": False}
                )
            await client.call_tool(
                "Scroll",
                {"loc": list(target), "delta_x": 120, "delta_y": 0, "observe": False},
            )
            await client.call_tool(
                "Scroll",
                {"loc": list(target), "delta_y": -120, "observe": False},
            )
            wait_until(
                lambda: {win32con.WM_MOUSEWHEEL, _WM_MOUSEHWHEEL}.issubset(
                    {event[0] for event in fixture.events}
                )
            )
            assert any(event[0] == _WM_XBUTTONDOWN for event in fixture.events)
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
            workers.append(worker)
            worker.start()
            assert drag_started.wait(2)
            wait_until(lambda: win32api.GetAsyncKeyState(win32con.VK_MBUTTON) & 0x8000)
            started = time.monotonic()
            # Send just the harmless registered stop chord outside the controller so
            # this exercises the real Windows hotkey, not its software reservation.
            press_fixture_stop_hotkey(application, fixture.hwnd)
            wait_until(lambda: not application.controller.snapshot().armed, timeout=1.0)
            stop_latency = time.monotonic() - started
            assert "Ctrl+Shift+H" in application.controller.snapshot().reason
            worker.join(2)
            assert not worker.is_alive()
            assert stopped
            assert not win32api.GetAsyncKeyState(win32con.VK_MBUTTON) & 0x8000
            denied = await client.call_tool("Type", {"text": "must not type"}, raise_on_error=False)
            assert denied.is_error
            denied_capture = await client.call_tool("Screenshot", {}, raise_on_error=False)
            assert denied_capture.is_error
            print(f"Native hotkey stop latency: {stop_latency:.3f}s")
            await exercise_native_teaching(
                client, application, fixture, main_window, appearance_root
            )
            open_own_ui_system_menu(main_window)
            with application.surface.capture_guard():
                assert not win32gui.IsWindowVisible(main_window)
            assert application.controller.snapshot().interface_ready
            assert application.surface._error is None
            application.controller.close()
            open_own_ui_system_menu(application.teaching_surface._panel)
            application.teaching_surface.close()
            assert not application.teaching_surface._thread.is_alive()
            assert not application.teaching_surface.window_handles()
            open_own_ui_system_menu(main_window)
            # Lifespan shutdown must leave the owned system-menu modal loop without user input.
        assert application.window_handles() == ()
        assert application.controller.snapshot().state == "closed"
        print("Native owned-menu capture acknowledgement and both modal shutdowns passed")
    finally:
        application.controller.stop("Live fixture ended.")
        try:
            for worker in workers:
                worker.join(3)
                assert not worker.is_alive(), "An owned fixture input worker did not stop"
        finally:
            try:
                if fixture.process is not None:
                    fixture.close()
            finally:
                # Restore only the pointer position changed by this owned exercise.
                try:
                    if pointer_changed:
                        win32api.SetCursorPos(previous_pointer)
                finally:
                    assert user32.SetThreadDpiAwarenessContext(previous_dpi)


def test_native_auto_capture_owned_fixture(monkeypatch):
    """Exercise real backend selection without needing foreground permission or input."""
    import win32con
    import win32gui
    from windows_mcp.desktop import screenshot
    from windows_mcp.uia import Rect

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.SetThreadDpiAwarenessContext.argtypes = [ctypes.c_void_p]
    user32.SetThreadDpiAwarenessContext.restype = ctypes.c_void_p
    previous_dpi = user32.SetThreadDpiAwarenessContext(ctypes.c_void_p(-4))
    assert previous_dpi
    fixture = FixtureWindow()
    try:
        fixture.start()
        win32gui.SetWindowPos(
            fixture.hwnd,
            win32con.HWND_TOPMOST,
            0,
            0,
            0,
            0,
            win32con.SWP_NOMOVE | win32con.SWP_NOSIZE | win32con.SWP_NOACTIVATE,
        )
        origin = win32gui.ClientToScreen(fixture.hwnd, (0, 0))
        bounds = (origin[0] + 20, origin[1] + 320, origin[0] + 700, origin[1] + 420)
        safe = own_capture_bounds(fixture.hwnd)
        assert safe[0] <= bounds[0] < bounds[2] <= safe[2]
        assert safe[1] <= bounds[1] < bounds[3] <= safe[3]
        dwm = ctypes.WinDLL("dwmapi", use_last_error=True)
        dwm.DwmFlush.restype = ctypes.c_long
        assert dwm.DwmFlush() >= 0
        assert_owned_region(fixture.hwnd, bounds)
        assert fixture.painted.is_set() and fixture.error is None
        used = []
        for _ in range(3):
            assert_owned_region(fixture.hwnd, bounds)
            image, name = screenshot.capture(Rect(*bounds), backend="auto")
            try:
                assert image.size == (680, 100)
                assert image.getpixel((650, 80)) == (242, 242, 242)
                used.append(name)
            finally:
                image.close()
        print("Native owned-fixture auto capture backends:", used)
    finally:
        try:
            if fixture.thread.ident is not None:
                fixture.close()
        finally:
            assert user32.SetThreadDpiAwarenessContext(previous_dpi)


def test_native_control_accessibility_and_compact_layout(monkeypatch):
    """Read native owned-control metadata without arming, typing or capturing."""
    import win32con
    import win32gui
    from desktop_mcp import ui
    from desktop_mcp.app import DesktopApplication
    from desktop_mcp.runtime import DesktopStopped

    native_adapter = ui._Win32Adapter

    class CompactAdapter(native_adapter):
        def _work_area(self, rectangle=None):
            return getattr(self, "fixture_work", None) or super()._work_area(rectangle)

        def initialize(self, surface):
            super().initialize(surface)
            left, top, right, bottom = super()._work_area()
            self.fixture_work = (left, top, min(right, left + 640), min(bottom, top + 480))
            self._reflow_panel(self.fixture_work)

    monkeypatch.setattr(ui, "_Win32Adapter", CompactAdapter)
    application = DesktopApplication()
    fixture = FixtureWindow()
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.SetThreadDpiAwarenessContext.argtypes = [ctypes.c_void_p]
    user32.SetThreadDpiAwarenessContext.restype = ctypes.c_void_p
    previous_dpi = user32.SetThreadDpiAwarenessContext(ctypes.c_void_p(-4))
    assert previous_dpi
    try:
        fixture.start()
        application.start()
        panel = application.surface.window_handles()[0]
        takeover = win32gui.GetDlgItem(panel, 1003)
        detail = win32gui.GetDlgItem(panel, 1101)
        activity = win32gui.GetDlgItem(panel, 1102)
        wait_until(lambda: "On" in win32gui.GetWindowText(takeover))
        assert "Arm" in win32gui.GetWindowText(detail)
        assert "Current action:" in win32gui.GetWindowText(activity)
        application.controller.set_human_takeover(False)
        wait_until(lambda: "Off" in win32gui.GetWindowText(takeover))

        def reject_arm():
            raise DesktopStopped("Owned fixture arm rejection")

        monkeypatch.setattr(application.controller, "arm_local", reject_arm)
        win32gui.SendMessage(win32gui.GetDlgItem(panel, 1001), win32con.BM_CLICK, 0, 0)
        wait_until(lambda: "Owned fixture arm rejection" in win32gui.GetWindowText(detail))
        assert not application.controller.snapshot().armed
        assert application.controller.snapshot().interface_ready
        work = application.surface._adapter.fixture_work
        left, top, right, bottom = win32gui.GetWindowRect(panel)
        assert work[0] <= left < right <= work[2] and work[1] <= top < bottom <= work[3]
        for identifier in (1001, 1002, 1003, 1004, 1101, 1102):
            child = win32gui.GetDlgItem(panel, identifier)
            x1, y1, x2, y2 = win32gui.GetWindowRect(child)
            assert left <= x1 < x2 <= right and top <= y1 < y2 <= bottom
        assert detail in application.window_handles() and activity in application.window_handles()
        print("Native accessible state/text and compact owned-panel bounds passed; no input armed")
    finally:
        try:
            application.close()
        finally:
            try:
                if fixture.thread.ident is not None:
                    fixture.close()
            finally:
                assert user32.SetThreadDpiAwarenessContext(previous_dpi)


async def test_native_compact_transcript_appearance_without_foreground(monkeypatch):
    """Capture only owned surfaces; do not arm, move the pointer or request foreground."""
    from fastmcp import Client
    import win32api
    import win32con
    import win32gui

    from desktop_mcp.app import DesktopApplication, create_server
    from tests.test_desktop_launch_live import chat_message_controls, control_text

    if os.getenv("DESKTOP_MCP_LIVE_APPEARANCE") != "1":
        pytest.skip("Native appearance needs its separate, explicit diagnostic capture run.")
    artifacts = os.getenv("DESKTOP_MCP_LIVE_ARTIFACTS")
    if not artifacts:
        pytest.skip("Native appearance requires an explicitly owned artifact directory.")
    artifact_root = Path(artifacts) / "compact-transcript"
    artifact_root.mkdir(parents=True, exist_ok=False)
    enable_owned_appearance_capture(monkeypatch)
    application = DesktopApplication()
    fixture = FixtureWindow()
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.SetThreadDpiAwarenessContext.argtypes = [ctypes.c_void_p]
    user32.SetThreadDpiAwarenessContext.restype = ctypes.c_void_p
    previous_dpi = user32.SetThreadDpiAwarenessContext(ctypes.c_void_p(-4))
    assert previous_dpi
    try:
        fixture.start()
        application.start()
        transcript = application.teaching_surface._panel
        main_panel = application.surface.window_handles()[0]
        win32gui.ShowWindow(main_panel, win32con.SW_SHOWMINNOACTIVE)
        foreground, pointer = win32gui.GetForegroundWindow(), win32api.GetCursorPos()
        click_local_button(transcript, "Pin")
        win32gui.SetWindowPos(
            fixture.hwnd,
            transcript,
            0,
            0,
            0,
            0,
            win32con.SWP_NOMOVE | win32con.SWP_NOSIZE | win32con.SWP_NOACTIVATE,
        )
        async with Client(create_server(application, manage_application=False)) as client:
            await client.call_tool(
                "Transcript",
                {
                    "title": "Owned native UI fixture",
                    "text": "Short guidance stays beside your message. Expand opens more history.",
                },
            )
            await client.call_tool(
                "Transcript",
                {
                    "title": "Ready for your question",
                    "text": "This fixture is paused. Send still works; desktop input is not armed.",
                },
            )
            composer = application.teaching_surface._composer
            question = "Can you explain the next step before clicking it?"
            win32gui.SendMessage(composer, win32con.WM_SETTEXT, 0, question)
            win32gui.SendMessage(win32gui.GetDlgItem(transcript, 206), win32con.BM_CLICK, 0, 0)
            incoming = await client.call_tool("TranscriptRead", {"timeout": 2.0})
            assert incoming.data["message"]["text"] == question
            await client.call_tool(
                "Transcript",
                {
                    "title": "One step at a time",
                    "text": "Yes. I will explain the control, give you a turn, then continue.",
                    "reply_to": incoming.data["message"]["id"],
                },
            )
            draft = "A short question can still be sent here."
            win32gui.SendMessage(composer, win32con.WM_SETTEXT, 0, draft)

            def reply_rendered():
                assert application.controller.snapshot().interface_ready, (
                    application.teaching_surface._error
                )
                return any(
                    text == "Yes. I will explain the control, give you a turn, then continue."
                    for _, text in chat_message_controls(
                        application.teaching_surface._history_window, expected_pid=os.getpid()
                    )
                )

            wait_until(reply_rendered, timeout=2.0)
            metadata = {}
            for name, compact in (("compact", True), ("expanded", False)):
                if not compact:
                    click_local_button(transcript, "Expand")
                wait_until(
                    lambda: application.teaching_surface.layout_status()["compact"] is compact
                )
                status = (await client.call_tool("DesktopStatus")).data
                assert status["state"] == "stopped" and status["completed_actions"] == 0
                assert control_text(composer) == draft
                texts = [
                    text
                    for _, text in chat_message_controls(
                        application.teaching_surface._history_window, expected_pid=os.getpid()
                    )
                ]
                assert question in texts
                assert "Yes. I will explain the control, give you a turn, then continue." in texts
                metadata[name] = status["transcript"]["layout"]
                save_own_window(
                    transcript,
                    artifact_root / f"{name}.png",
                    backdrop=fixture.hwnd,
                    activate=False,
                )
                assert win32gui.GetForegroundWindow() == foreground
                assert win32api.GetCursorPos() == pointer
            (artifact_root / "layout.json").write_text(json.dumps(metadata), encoding="utf-8")
            print(
                "Native compact/expanded appearance captured on an opaque owned backdrop; no input armed"
            )
    finally:
        try:
            application.close()
        finally:
            try:
                if fixture.thread.ident is not None:
                    fixture.close()
            finally:
                assert user32.SetThreadDpiAwarenessContext(previous_dpi)
