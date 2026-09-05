"""Opt-in native exercise confined to a newly created, harmless fixture window."""

from __future__ import annotations

import base64
import asyncio
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
        self.events = []
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
                if message in (
                    win32con.WM_MOUSEWHEEL,
                    win32con.WM_MOUSEHWHEEL,
                    win32con.WM_MBUTTONDOWN,
                    win32con.WM_MBUTTONUP,
                    win32con.WM_XBUTTONDOWN,
                    win32con.WM_XBUTTONUP,
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
            cls.hbrBackground = 0
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
        ), "An unowned window overlaps the fixture; refusing to capture its pixels"


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
    win32gui.SetForegroundWindow(panel)
    wait_until(lambda: win32gui.GetForegroundWindow() == panel)
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
    was_topmost = bool(
        win32gui.GetWindowLong(handle, win32con.GWL_EXSTYLE) & win32con.WS_EX_TOPMOST
    )
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
        assert_owned_region(handle, (left, top, right, bottom), allowed_above=overlay_handles)
        region = {"left": left, "top": top, "width": right - left, "height": bottom - top}
        with mss.mss() as capture:
            pixels = capture.grab(region)
            image = Image.frombytes("RGB", pixels.size, pixels.rgb)
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


async def exercise_native_teaching(client, application, fixture, main_window, artifact_root):
    import win32api
    import win32con
    import win32gui

    assert win32gui.GetForegroundWindow() == fixture.hwnd
    win32gui.ShowWindow(main_window, win32con.SW_SHOWNOACTIVATE)
    click_local_button(main_window, "Teach")
    wait_until(lambda: application.controller.snapshot().mode == "teach")
    assert not application.controller.snapshot().armed
    arm_fixture_locally(main_window)
    wait_until(lambda: application.controller.snapshot().armed)
    wait_until(lambda: win32gui.IsIconic(main_window))
    assert win32gui.GetForegroundWindow() == fixture.hwnd

    transcript = application.teaching_surface._panel
    editor = application.teaching_surface._editor
    assert win32gui.IsWindowVisible(transcript)
    assert not win32gui.GetWindowLong(transcript, win32con.GWL_EXSTYLE) & win32con.WS_EX_TOOLWINDOW
    assert win32gui.GetWindowLong(editor, win32con.GWL_STYLE) & win32con.ES_READONLY
    pointer = win32api.GetCursorPos()
    note = "Move your cursor into the marked area. Proximity does not mean you clicked."
    result = await client.call_tool(
        "Transcript", {"title": "Isolated teaching fixture", "text": note}
    )
    assert not result.is_error
    wait_until(lambda: note in win32gui.GetWindowText(editor))
    assert win32gui.GetForegroundWindow() == fixture.hwnd
    assert win32api.GetCursorPos() == pointer
    click_local_button(transcript, "Pin")
    assert win32gui.GetWindowLong(transcript, win32con.GWL_EXSTYLE) & win32con.WS_EX_TOPMOST
    pinned = await client.call_tool("Transcript", {"action": "back"}, raise_on_error=False)
    assert pinned.is_error
    click_local_button(transcript, "Unpin")
    await client.call_tool("Transcript", {"action": "front"})
    assert win32gui.GetForegroundWindow() == fixture.hwnd
    if artifact_root:
        save_own_window(transcript, artifact_root / "teaching-transcript.png")

    before_text = win32gui.GetWindowText(fixture.editor)
    denied = await client.call_tool(
        "Type", {"text": "must not type in teaching mode", "observe": False}, raise_on_error=False
    )
    assert denied.is_error
    assert win32gui.GetWindowText(fixture.editor) == before_text
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
    assert win32gui.GetWindowText(fixture.editor) == before_text

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
    assert win32gui.GetWindowText(fixture.editor) == before_text

    win32gui.ShowWindow(main_window, win32con.SW_SHOWNOACTIVATE)
    arm_fixture_locally(main_window)
    wait_until(lambda: application.controller.snapshot().armed)
    wait_until(lambda: win32gui.IsIconic(main_window))
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
        assert win32gui.GetWindowText(fixture.editor) == before_text
        print(f"Native teaching hotkey stop latency: {time.monotonic() - started:.3f}s")
    finally:
        if not waiting.done():
            waiting.cancel()
        await asyncio.gather(waiting, return_exceptions=True)
    print(
        "Native teaching: local mode/allow, transcript/pin/focus, ink/laser/capture exclusion, dwell and both stops passed"
    )


async def test_native_control_input_images_and_global_stop(monkeypatch):
    from fastmcp import Client
    from PIL import Image
    import win32api
    import win32con
    import win32gui

    from desktop_mcp.actions import Action
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
    workers = []
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
            win32gui.SetForegroundWindow(fixture.hwnd)
            wait_until(lambda: win32gui.GetForegroundWindow() == fixture.hwnd)
            point = win32gui.ClientToScreen(fixture.editor, (30, 30))
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
            assert win32gui.GetForegroundWindow() == fixture.hwnd
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
                lambda: {win32con.WM_MOUSEWHEEL, win32con.WM_MOUSEHWHEEL}.issubset(
                    {event[0] for event in fixture.events}
                )
            )
            assert any(event[0] == win32con.WM_XBUTTONDOWN for event in fixture.events)
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
            await exercise_native_teaching(client, application, fixture, main_window, artifact_root)
            open_own_ui_system_menu(main_window)
            with application.surface.capture_guard():
                assert not win32gui.IsWindowVisible(main_window)
            assert application.controller.snapshot().interface_ready
            assert application.surface._error is None
            open_own_ui_system_menu(main_window)
            # Lifespan shutdown must leave the owned system-menu modal loop without user input.
        assert application.window_handles() == ()
        assert application.controller.snapshot().state == "closed"
        print("Native owned-menu capture acknowledgement and modal shutdown passed")
    finally:
        application.controller.stop("Live fixture ended.")
        try:
            for worker in workers:
                worker.join(3)
                assert not worker.is_alive(), "An owned fixture input worker did not stop"
        finally:
            try:
                if fixture.thread.ident is not None:
                    fixture.close()
            finally:
                # Restore only the pointer position changed by this owned exercise.
                win32api.SetCursorPos(previous_pointer)
