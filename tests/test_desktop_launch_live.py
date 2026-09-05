"""Opt-in native launch/quit only: no arming, input injection or screenshots."""

import asyncio
import os
import sys
import uuid

import pytest
import win32con
import win32gui
import win32process

from desktop_mcp.pipe_transport import connect
from desktop_mcp.service import ServiceState, _handshake

pytestmark = pytest.mark.skipif(
    os.getenv("DESKTOP_MCP_LAUNCH_LIVE") != "1",
    reason="Requires explicit no-input native launch test opt-in and a free stop hotkey.",
)


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
        assert len(visible) == 1, "Startup must not open separate empty teaching/control panels."
        main_panel = visible[0]
        children = []
        win32gui.EnumChildWindows(
            main_panel,
            lambda handle, _: children.append(win32gui.GetDlgCtrlID(handle)) or True,
            None,
        )
        assert 1004 not in children and 1005 not in children
        handle = next(
            handle
            for handle, title in windows
            if ("Instructions" in title) == (window_kind == "instructions")
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
