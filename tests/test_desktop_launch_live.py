"""Opt-in native launch/quit only: no arming, input injection or screenshots."""

import asyncio
import ctypes
from ctypes import wintypes
import os
import sys
import uuid

import pytest
import win32api
import win32con
import win32gui
import win32process
from fastmcp import Client

from desktop_mcp.pipe_transport import connect
from desktop_mcp.service import ServiceState, _handshake
from tests.test_desktop_service import transport

pytestmark = pytest.mark.skipif(
    os.getenv("DESKTOP_MCP_LAUNCH_LIVE") != "1",
    reason="Requires explicit no-input native launch test opt-in and a free stop hotkey.",
)


def control_text(handle):
    """Read the actual control buffer, not another process's cached window caption."""
    send = ctypes.WinDLL("user32", use_last_error=True).SendMessageTimeoutW
    send.argtypes = [
        wintypes.HWND,
        wintypes.UINT,
        wintypes.WPARAM,
        wintypes.LPARAM,
        wintypes.UINT,
        wintypes.UINT,
        ctypes.POINTER(ctypes.c_size_t),
    ]
    send.restype = wintypes.LPARAM
    length = ctypes.c_size_t()
    assert send(handle, win32con.WM_GETTEXTLENGTH, 0, 0, 2, 1000, ctypes.byref(length))
    buffer = ctypes.create_unicode_buffer(length.value + 1)
    result = ctypes.c_size_t()
    assert send(
        handle,
        win32con.WM_GETTEXT,
        len(buffer),
        ctypes.addressof(buffer),
        2,
        1000,
        ctypes.byref(result),
    )
    return buffer.value


@pytest.fixture
def physical_queries():
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.SetThreadDpiAwarenessContext.argtypes = [ctypes.c_void_p]
    user32.SetThreadDpiAwarenessContext.restype = ctypes.c_void_p
    previous = user32.SetThreadDpiAwarenessContext(ctypes.c_void_p(-4))
    assert previous
    try:
        yield
    finally:
        assert user32.SetThreadDpiAwarenessContext(previous)


@pytest.mark.parametrize("window_kind", ["control", "instructions"])
async def test_either_window_x_ends_its_owned_host_process(tmp_path, window_kind, physical_queries):
    name = f"Desktop-MCP-live-exit-{uuid.uuid4().hex}"
    script = (
        "import asyncio, os; from pathlib import Path; "
        "os.environ['ANONYMIZED_TELEMETRY']='false'; "
        "from desktop_mcp.service import run_host; "
        f"asyncio.run(run_host(name={name!r}, state_root=Path({str(tmp_path)!r})))"
    )
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        script,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    windows = []
    info = None
    try:
        connecting = asyncio.create_task(connect(name, timeout=20))
        exited = asyncio.create_task(process.wait())
        try:
            await asyncio.wait((connecting, exited), return_when=asyncio.FIRST_COMPLETED)
            if exited.done():
                _, stderr = await process.communicate()
                detail = stderr.decode("utf-8", errors="replace")
                (tmp_path / "startup-stderr.txt").write_text(detail, encoding="utf-8")
                pytest.fail(f"The owned native host exited during startup:\n{detail}")
            channel = await connecting
        finally:
            for task in (connecting, exited):
                if not task.done():
                    task.cancel()
            await asyncio.gather(connecting, exited, return_exceptions=True)
        try:
            info = await _handshake(channel, "probe")
        finally:
            channel.close()
        assert info["status"]["state"] == "stopped"
        assert info["status"]["completed_actions"] == 0
        assert "mode" not in info["status"]
        def collect(handle, _):
            if win32process.GetWindowThreadProcessId(handle)[1] == info["pid"]:
                title = win32gui.GetWindowText(handle)
                if title.startswith("Desktop-MCP") and not (
                    win32gui.GetWindowLong(handle, win32con.GWL_EXSTYLE) & win32con.WS_EX_TOOLWINDOW
                ):
                    windows.append((handle, title))
            return True

        win32gui.EnumWindows(collect, None)
        visible = [handle for handle, _ in windows if win32gui.IsWindowVisible(handle)]
        assert len(visible) == 2, "The app and its two-way transcript should open together."
        main_panel = next(handle for handle, title in windows if "Transcript" not in title)
        children = []
        win32gui.EnumChildWindows(
            main_panel,
            lambda handle, _: children.append(win32gui.GetDlgCtrlID(handle)) or True,
            None,
        )
        assert 1004 in children and 1005 not in children
        assert "Transcript" in win32gui.GetWindowText(win32gui.GetDlgItem(main_panel, 1004))
        if window_kind == "control":
            transcript = next(handle for handle, title in windows if "Transcript" in title)
            composer = win32gui.GetDlgItem(transcript, 303)
            sender = win32gui.GetDlgItem(transcript, 206)
            history = win32gui.GetDlgItem(transcript, 301)
            async with Client(transport(name)) as client:
                status = (await client.call_tool("DesktopStatus")).data
                layout = status["transcript"]["layout"]
                compact_bounds = win32gui.GetWindowRect(transcript)
                work = win32api.GetMonitorInfo(
                    win32api.MonitorFromWindow(transcript, win32con.MONITOR_DEFAULTTONEAREST)
                )["Work"]
                scale = layout["dpi"] / 96
                assert layout["compact"] and layout["dock"] == "bottom"
                assert list(compact_bounds) == layout["bounds"]
                assert work[0] <= compact_bounds[0] < compact_bounds[2] <= work[2]
                assert work[1] <= compact_bounds[1] < compact_bounds[3] <= work[3]
                if work[2] - work[0] >= 1136 * scale and work[3] - work[1] >= 180 * scale:
                    assert compact_bounds[2] - compact_bounds[0] == round(1120 * scale)
                    assert compact_bounds[3] - compact_bounds[1] == round(164 * scale)
                    assert layout["font_height"] == round(14 * scale)
                if layout["split"]:
                    assert compact_bounds[2] - compact_bounds[0] > 4 * (
                        compact_bounds[3] - compact_bounds[1]
                    )
                rows = {row["window_id"]: row for row in status["protected_windows"]}
                for handle, role in (
                    (transcript, "transcript"),
                    (history, "transcript-history"),
                    (composer, "transcript-composer"),
                    (sender, "transcript-send"),
                ):
                    assert rows[handle]["role"] == role
                    assert rows[handle]["root_id"] == transcript
                origin = win32gui.ClientToScreen(transcript, (0, 0))
                _, _, width, height = win32gui.GetClientRect(transcript)
                assert rows[win32gui.GetDlgItem(transcript, 306)]["role"] == "transcript-history-scrollbar"
                assert rows[win32gui.GetDlgItem(transcript, 307)]["role"] == "transcript-composer-scrollbar"
                for identifier in (*range(201, 210), *range(301, 308)):
                    child = win32gui.GetDlgItem(transcript, identifier)
                    assert child and child in rows
                    if not win32gui.IsWindowVisible(child):
                        continue
                    left, top, right, bottom = win32gui.GetWindowRect(child)
                    assert origin[0] <= left < right <= origin[0] + width
                    assert origin[1] <= top < bottom <= origin[1] + height
                draft = "Native fixture draft survives expansion"
                win32gui.SendMessage(composer, win32con.WM_SETTEXT, 0, draft)
                win32gui.SendMessage(composer, win32con.EM_SETSEL, 7, 14)
                foreground = win32gui.GetForegroundWindow()
                for compact in (False, True):
                    win32gui.SendMessage(
                        transcript, win32con.WM_COMMAND, 207,
                        win32gui.GetDlgItem(transcript, 207),
                    )
                    status = (await client.call_tool("DesktopStatus")).data
                    assert status["interface_ready"], status["last_error"]
                    layout = status["transcript"]["layout"]
                    assert layout["compact"] is compact
                    assert control_text(composer) == draft
                    assert win32gui.SendMessage(composer, win32con.EM_GETSEL, 0, 0) == (
                        7 | (14 << 16)
                    )
                    actual_foreground = win32gui.GetForegroundWindow()
                    assert actual_foreground == foreground, {
                        "before": foreground,
                        "after": actual_foreground,
                        "after_is_owned": (
                            win32gui.IsWindow(actual_foreground)
                            and win32process.GetWindowThreadProcessId(actual_foreground)[1] == info["pid"]
                        ),
                        "transcript": transcript,
                        "main": main_panel,
                    }
                    if not compact:
                        assert not layout["split"]
                        assert layout["bounds"][3] - layout["bounds"][1] > (
                            compact_bounds[3] - compact_bounds[1]
                        )
                assert win32gui.GetWindowRect(transcript) == compact_bounds
                pin = win32gui.GetDlgItem(transcript, 201)
                win32gui.SendMessage(pin, win32con.BM_CLICK, 0, 0)
                win32gui.SendMessage(
                    win32gui.GetDlgItem(transcript, 208), win32con.BM_CLICK, 0, 0
                )
                layout = (await client.call_tool("DesktopStatus")).data["transcript"]["layout"]
                monitor = win32api.GetMonitorInfo(
                    win32api.MonitorFromWindow(transcript, win32con.MONITOR_DEFAULTTONEAREST)
                )
                assert layout["dock"] == "taskbar-edge"
                assert layout["bounds"][3] == monitor["Monitor"][3]
                left, top, right, bottom = win32gui.GetWindowRect(
                    win32gui.GetDlgItem(transcript, 205)
                )
                hit = win32gui.WindowFromPoint(((left + right) // 2, (top + bottom) // 2))
                assert win32gui.GetAncestor(hit, 2) == transcript, (
                    "The pinned taskbar-edge Stop control is occluded"
                )
                win32gui.SendMessage(
                    win32gui.GetDlgItem(transcript, 203), win32con.BM_CLICK, 0, 0
                )
                win32gui.SendMessage(pin, win32con.BM_CLICK, 0, 0)
                assert control_text(composer) == draft
                native_question = "Native transcript question " + "\u03bb" * 1000
                win32gui.SendMessage(composer, win32con.WM_SETTEXT, 0, native_question)
                assert control_text(composer) == native_question
                win32gui.SendMessage(sender, win32con.BM_CLICK, 0, 0)
                question = await client.call_tool("TranscriptRead", {"timeout": 2.0})
                assert question.data["message"] is not None, (
                    control_text(composer),
                    control_text(win32gui.GetDlgItem(transcript, 302)),
                    win32gui.IsWindowEnabled(sender),
                )
                assert question.data["message"]["text"] == native_question
                assert control_text(composer) == ""
                await client.call_tool(
                    "Transcript",
                    {
                        "text": "Native transcript reply",
                        "reply_to": question.data["message"]["id"],
                    },
                )
                for _ in range(100):
                    if "Native transcript reply" in control_text(history):
                        break
                    await asyncio.sleep(0.02)
                assert "Native transcript reply" in control_text(history)
                await client.call_tool("Transcript", {"action": "hide"})
                assert not win32gui.IsWindowVisible(transcript)
                await client.call_tool("Transcript", {"action": "show"})
                assert win32gui.IsWindowVisible(transcript)
                toggle = win32gui.GetDlgItem(main_panel, 1004)
                for expected in (False, True):
                    win32gui.SendMessage(toggle, win32con.BM_CLICK, 0, 0)
                    for _ in range(100):
                        if bool(win32gui.IsWindowVisible(transcript)) is expected:
                            break
                        await asyncio.sleep(0.02)
                    assert bool(win32gui.IsWindowVisible(transcript)) is expected
                status = (await client.call_tool("DesktopStatus")).data
                assert status["state"] == "stopped" and status["completed_actions"] == 0
                assert status["transcript"]["pending_messages"] == 0
        handle = next(
            handle
            for handle, title in windows
            if ("Transcript" in title) == (window_kind == "instructions")
        )
        # Only an explicitly owned application's X message, never a user's other window.
        win32gui.PostMessage(handle, win32con.WM_CLOSE, 0, 0)
        stdout, stderr = await asyncio.wait_for(process.communicate(), 10)
        assert process.returncode == 0, stderr.decode("utf-8", errors="replace")
        assert ServiceState(name, tmp_path).read()["closed"] is True
        assert all(not win32gui.IsWindow(handle) for handle, _ in windows)
    finally:
        if process.returncode is None:
            for window, _ in windows:
                if (
                    info is not None
                    and win32gui.IsWindow(window)
                    and win32process.GetWindowThreadProcessId(window)[1] == info["pid"]
                ):
                    win32gui.PostMessage(window, win32con.WM_CLOSE, 0, 0)
            try:
                await asyncio.wait_for(process.communicate(), 5)
            except asyncio.TimeoutError:
                process.kill()
                await asyncio.wait_for(process.communicate(), 5)
        else:
            await process.communicate()
