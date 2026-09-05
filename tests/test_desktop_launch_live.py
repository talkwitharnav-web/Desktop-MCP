"""Opt-in native launch/quit only: no arming, input injection or screenshots."""

import asyncio
import ctypes
from ctypes import wintypes
import os
import sys
import uuid

import pytest
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


@pytest.mark.parametrize("window_kind", ["control", "instructions"])
async def test_either_window_x_ends_its_owned_host_process(tmp_path, window_kind):
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
    try:
        channel = await connect(name, timeout=20)
        try:
            info = await _handshake(channel, "probe")
        finally:
            channel.close()
        assert info["status"]["state"] == "stopped"
        assert info["status"]["completed_actions"] == 0
        assert "mode" not in info["status"]
        windows = []

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
                win32gui.SendMessage(composer, win32con.WM_SETTEXT, 0, "Native transcript question")
                assert control_text(composer) == "Native transcript question"
                win32gui.SendMessage(sender, win32con.BM_CLICK, 0, 0)
                question = await client.call_tool("TranscriptRead", {"timeout": 2.0})
                assert question.data["message"] is not None, (
                    control_text(composer),
                    control_text(win32gui.GetDlgItem(transcript, 302)),
                    win32gui.IsWindowEnabled(sender),
                )
                assert question.data["message"]["text"] == "Native transcript question"
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
            process.kill()
            await process.wait()
