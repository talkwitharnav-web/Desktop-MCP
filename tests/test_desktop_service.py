"""Shared-host MCP regressions using fake desktops and real local named pipes."""

import asyncio
from contextlib import asynccontextmanager
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
from types import SimpleNamespace
import uuid

from fastmcp import Client
from fastmcp.client.transports import StdioTransport
import pytest

from desktop_mcp.app import DesktopApplication
from desktop_mcp.pipe_transport import connect
from desktop_mcp.service import ServiceState, _handshake, run_host
from desktop_mcp import service
from tests.test_desktop_tools import FixtureApplication


class SharedApplication(FixtureApplication):
    def __init__(self):
        super().__init__()
        self.exit_requested = threading.Event()
        self.started = 0
        self.closed = 0
        self.shown = 0
        self.surface = SimpleNamespace(show=self.show, window_handles=lambda: ())

    def start(self):
        self.started += 1

    def close(self):
        self.closed += 1
        super().close()

    def show(self):
        self.shown += 1

    request_exit = DesktopApplication.request_exit


@asynccontextmanager
async def host(tmp_path):
    app = SharedApplication()
    name = f"Desktop-MCP-host-test-{uuid.uuid4().hex}"
    task = asyncio.create_task(run_host(app, name=name, state_root=tmp_path))
    try:
        ready = await connect(name, timeout=3)
        await _handshake(ready, "probe")
        ready.close()
        yield app, name, task
    finally:
        app.request_exit()
        await asyncio.wait_for(task, 5)


def bridge_script(name):
    return (
        "import asyncio; "
        "from desktop_mcp.pipe_transport import connect; "
        "from desktop_mcp.service import _handshake; "
        "from desktop_mcp.stdio_bridge import forward_stdio;\n"
        "async def main():\n"
        f"    pipe = await connect({name!r}, timeout=3)\n"
        "    await _handshake(pipe, 'mcp')\n"
        "    await forward_stdio(pipe)\n"
        "asyncio.run(main())"
    )


def transport(name):
    return StdioTransport(command=sys.executable, args=["-c", bridge_script(name)])


async def test_two_copilot_clients_share_one_desktop_and_can_disconnect_independently(tmp_path):
    async with host(tmp_path) as (app, name, _):
        async with Client(transport(name)) as first:
            async with Client(transport(name)) as second:
                assert len(await first.list_tools()) == 20
                assert len(await second.list_tools()) == 20
                one, two = await asyncio.gather(
                    first.call_tool("DesktopStatus"), second.call_tool("DesktopStatus")
                )
                assert one.data["state"] == two.data["state"] == "stopped"
                assert app.started == 1 and app.closed == 0
            assert (await first.call_tool("DesktopStatus")).data["state"] == "stopped"
        assert app.closed == 0, (
            "Closing a CLI must not strand or destroy the Start-menu application."
        )
    assert app.closed == 1
    assert ServiceState(name, tmp_path).read()["closed"] is True


async def test_local_open_reveals_the_same_instance_without_arming(tmp_path):
    async with host(tmp_path) as (app, name, _):
        for _ in range(2):
            channel = await connect(name)
            info = await _handshake(channel, "show")
            assert info["status"]["state"] == "stopped"
            await channel.send(b'{"activate":true}')
            assert json.loads(await channel.receive()) == {"shown": True}
            channel.close()
        assert app.shown == 2
        assert app.started == 1
        assert not app.controller.snapshot().armed


async def test_x_exits_host_and_stdio_bridge_even_with_client_stdin_open(tmp_path):
    async with host(tmp_path) as (app, name, task):
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-c",
            bridge_script(name),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            process.stdin.write(
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "initialize",
                        "params": {
                            "protocolVersion": "2025-11-25",
                            "capabilities": {},
                            "clientInfo": {"name": "test-client", "version": "1"},
                        },
                    }
                ).encode()
                + b"\n"
            )
            await process.stdin.drain()
            reply = json.loads(await asyncio.wait_for(process.stdout.readline(), 5))
            assert "result" in reply
            app.request_exit()
            await asyncio.wait_for(task, 5)
            # Deliberately don't close stdin: an attached client must not keep the proxy alive.
            assert await asyncio.wait_for(process.wait(), 3) == 0
        finally:
            if process.returncode is None:
                process.kill()
                await process.wait()


async def test_stop_remains_global_but_status_probes_do_not_disarm_other_clients(tmp_path):
    async with host(tmp_path) as (app, name, _):
        # Fake backend only; no real UI/authorization is being bypassed here.
        app.controller.arm_local()
        generation = app.controller.snapshot().generation
        async with Client(transport(name)) as first:
            assert (await first.call_tool("DesktopStatus")).data["state"] == "ready"
        await asyncio.sleep(0.05)
        assert app.controller.snapshot().generation == generation
        async with Client(transport(name)) as second:
            await second.call_tool("DesktopStop")
        assert not app.controller.snapshot().armed


def test_explicit_quit_state_is_recorded_without_private_content(tmp_path):
    state = ServiceState("fixture", tmp_path)
    assert state.read() == {}
    state.write(closed=True, version="0.1.0")
    assert state.read() == {"closed": True, "version": "0.1.0"}
    assert [item.name for item in tmp_path.iterdir()] == ["fixture.json"]


async def test_autostart_refuses_to_reverse_an_explicit_x_click(tmp_path, monkeypatch):
    name = f"Desktop-MCP-closed-test-{uuid.uuid4().hex}"
    state = ServiceState(name, tmp_path)
    state.write(closed=True)
    monkeypatch.setattr(service, "channel_name", lambda: name)
    monkeypatch.setattr(service, "ServiceState", lambda *args: state)
    monkeypatch.setattr(
        service.subprocess, "Popen", lambda *args, **kwargs: pytest.fail("Respawned")
    )
    with pytest.raises(RuntimeError, match="closed with X"):
        await service.ensure_host()


@pytest.mark.parametrize("command", ["arm", [], {"name": "arm"}, None])
async def test_unknown_client_command_does_not_take_down_the_host(tmp_path, command):
    async with host(tmp_path) as (_, name, _):
        bad = await connect(name)
        await bad.send(json.dumps({"protocol": 1, "command": command}).encode())
        with pytest.raises(EOFError):
            await bad.receive()
        bad.close()
        good = await connect(name)
        try:
            assert (await _handshake(good, "probe"))["status"]["state"] == "stopped"
        finally:
            good.close()


async def test_malformed_tool_name_cannot_terminate_other_clients(tmp_path):
    async with host(tmp_path) as (_, name, task):
        bad = await connect(name)
        try:
            await _handshake(bad, "mcp")
            await bad.send(
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "initialize",
                        "params": {
                            "protocolVersion": "2025-11-25",
                            "capabilities": {},
                            "clientInfo": {"name": "test", "version": "1"},
                        },
                    }
                ).encode()
            )
            assert "result" in json.loads(await bad.receive())
            await bad.send(b'{"jsonrpc":"2.0","method":"notifications/initialized"}')
            await bad.send(b'{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":[]}}')
            result = json.loads(await asyncio.wait_for(bad.receive(), 3))
            assert "error" in result
            good = await connect(name)
            try:
                assert (await _handshake(good, "probe"))["status"]["state"] == "stopped"
                assert not task.done()
            finally:
                good.close()
        finally:
            bad.close()


def test_host_spawn_requests_independent_lifetime(monkeypatch):
    calls = []
    monkeypatch.setattr(service.subprocess, "Popen", lambda *args, **kwargs: calls.append(kwargs))
    service._spawn_host(Path(sys.executable))
    flags = calls[0]["creationflags"]
    assert flags & subprocess.CREATE_BREAKAWAY_FROM_JOB
    assert flags & subprocess.CREATE_NO_WINDOW


def test_forbidden_job_breakaway_does_not_fall_back_to_a_client_owned_host(monkeypatch):
    calls = []

    def denied(*args, **kwargs):
        calls.append(True)
        error = OSError("The test job forbids breakaway")
        error.winerror = 5
        raise error

    monkeypatch.setattr(service.subprocess, "Popen", denied)
    with pytest.raises(RuntimeError, match="Start first"):
        service._spawn_host(Path(sys.executable))
    assert calls == [True]


async def test_backed_up_stdout_does_not_prevent_bridge_exit_when_its_host_exits(tmp_path):
    """The host is another process so its exit signal is independent of pipe reads."""
    name = f"Desktop-MCP-output-test-{uuid.uuid4().hex}"
    site_packages = Path(sys.prefix) / "Lib" / "site-packages"
    host_script = (
        f"import site; site.addsitedir({str(site_packages)!r}); "
        "import asyncio; from pathlib import Path; "
        "from desktop_mcp.service import run_host; "
        "from tests.test_desktop_service import SharedApplication; "
        f"asyncio.run(run_host(SharedApplication(), name={name!r}, state_root=Path({str(tmp_path)!r})))"
    )
    host_process = await asyncio.create_subprocess_exec(
        sys._base_executable,
        "-c",
        host_script,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    proxy = None
    try:
        ready = await connect(name, timeout=5)
        info = await _handshake(ready, "probe")
        assert info["pid"] == host_process.pid
        ready.close()
        # A raw OS pipe is deliberately never read, unlike asyncio's buffered stdout reader.
        read_fd, write_fd = os.pipe()
        try:
            proxy = await asyncio.create_subprocess_exec(
                sys._base_executable,
                "-c",
                f"import site; site.addsitedir({str(site_packages)!r}); " + bridge_script(name),
                stdin=asyncio.subprocess.PIPE,
                stdout=write_fd,
                stderr=asyncio.subprocess.PIPE,
            )
            initialize = {
                "jsonrpc": "2.0",
                "id": 0,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-11-25",
                    "capabilities": {},
                    "clientInfo": {"name": "blocked-stdout", "version": "1"},
                },
            }
            proxy.stdin.write(json.dumps(initialize).encode() + b"\n")
            proxy.stdin.write(b'{"jsonrpc":"2.0","method":"notifications/initialized"}\n')
            for index in range(1, 20):
                proxy.stdin.write(
                    json.dumps(
                        {
                            "jsonrpc": "2.0",
                            "id": index,
                            "method": "tools/list",
                            "params": {},
                        }
                    ).encode()
                    + b"\n"
                )
            await proxy.stdin.drain()
            await asyncio.sleep(1.0)
            assert proxy.returncode is None
            # Only this owned, fake-desktop host is ended. No UI or user process exists here.
            host_process.kill()
            await host_process.wait()
            assert await asyncio.wait_for(proxy.wait(), 3) == 0
        finally:
            os.close(write_fd)
            os.close(read_fd)
    finally:
        for process in (proxy, host_process):
            if process is not None and process.returncode is None:
                process.kill()
                await process.wait()
