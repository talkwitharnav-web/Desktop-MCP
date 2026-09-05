"""One desktop host per Windows user/session, shared by lightweight MCP clients."""

from __future__ import annotations

import asyncio
from contextlib import ExitStack
from dataclasses import asdict
import json
import logging
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from typing import TYPE_CHECKING

import anyio
from platformdirs import user_state_dir

from desktop_mcp import __version__
from desktop_mcp.pipe_transport import (
    PROTOCOL,
    InstanceLock,
    PipeChannel,
    PipeListener,
    channel_name,
    connect,
)

if TYPE_CHECKING:
    from desktop_mcp.app import DesktopApplication

logger = logging.getLogger(__name__)


class ServiceState:
    """Persist only lifecycle metadata, never screenshots, tool input or credentials."""

    def __init__(self, name: str, root: Path | None = None) -> None:
        self.root = root or Path(user_state_dir("Desktop-MCP", appauthor=False))
        self.path = self.root / f"{name}.json"

    def read(self) -> dict[str, object]:
        try:
            content = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return {}
        result = json.loads(content)
        if not isinstance(result, dict):
            raise ValueError("Desktop-MCP's local service state is not a JSON object.")
        return result

    def write(self, **values: object) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        temporary: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=self.root,
                prefix="service-",
                suffix=".tmp",
                delete=False,
            ) as stream:
                temporary = Path(stream.name)
                json.dump(values, stream, ensure_ascii=True, allow_nan=False)
            os.replace(temporary, self.path)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)


async def _rpc_stream(channel: PipeChannel, application: DesktopApplication) -> None:
    """Adapt a pipe to the SDK's JSON-RPC streams; each client has its own MCP session."""
    from fastmcp.server.context import reset_transport, set_transport
    from mcp.shared.message import SessionMessage
    from mcp.types import JSONRPCMessage
    from pydantic import ValidationError
    from desktop_mcp.app import create_server

    chat_sessions: set[str] = set()
    desktop_sessions: set[str] = set()
    server = create_server(
        application,
        manage_application=False,
        on_chat_session=chat_sessions.add,
        on_desktop_session=desktop_sessions.add,
    )
    inbound, read_stream = anyio.create_memory_object_stream[SessionMessage | Exception](1)
    write_stream, outbound = anyio.create_memory_object_stream[SessionMessage](1)
    token = set_transport("stdio")
    try:
        # This follows FastMCP's stdio adapter. The application itself is owned by
        # the host, not this session: reconnecting never registers another hotkey.
        async with server._lifespan_manager():
            async with anyio.create_task_group() as tasks:

                async def receive() -> None:
                    try:
                        async with inbound:
                            while True:
                                packet = await channel.receive()
                                try:
                                    message = JSONRPCMessage.model_validate_json(packet)
                                except ValidationError:
                                    raise ValueError("Invalid MCP JSON-RPC message.") from None
                                await inbound.send(SessionMessage(message))
                    finally:
                        tasks.cancel_scope.cancel()

                async def send() -> None:
                    async with outbound:
                        async for message in outbound:
                            await channel.send(
                                message.message.model_dump_json(
                                    by_alias=True, exclude_none=True
                                ).encode("utf-8")
                            )

                tasks.start_soon(receive)
                tasks.start_soon(send)
                await server._mcp_server.run(
                    read_stream, write_stream, server._mcp_server.create_initialization_options()
                )
                tasks.cancel_scope.cancel()
    except* EOFError, OSError, ValueError, anyio.BrokenResourceError, anyio.ClosedResourceError:
        logger.info("An MCP connection ended.")
    finally:
        for owner in desktop_sessions:
            application.interaction.release(owner, disconnected=True)
        for owner in chat_sessions:
            application.teaching.conversation.release_listener(owner)
        reset_transport(token)
        await read_stream.aclose()
        await write_stream.aclose()


async def run_host(
    application: DesktopApplication | None = None,
    *,
    name: str | None = None,
    state_root: Path | None = None,
) -> None:
    """Run the actual desktop application until either local window is closed."""
    from desktop_mcp.app import DesktopApplication

    name = name or channel_name()
    state = ServiceState(name, state_root)
    with InstanceLock(name, "host"), ExitStack() as cleanup:
        try:
            active = application if application is not None else DesktopApplication()
            cleanup.callback(active.close)
            active.start()
            listener = PipeListener(name)
            cleanup.callback(listener.close)
            instance_id = str(active.host_info["instance_id"])
            state.write(closed=False, pid=os.getpid(), instance=instance_id, version=__version__)
            connections: set[PipeChannel] = set()

            async def client(channel: PipeChannel) -> None:
                try:
                    async with asyncio.timeout(10):
                        request = json.loads(await channel.receive())
                        if not isinstance(request, dict) or request.get("protocol") != PROTOCOL:
                            raise ValueError("Unsupported Desktop-MCP connection protocol.")
                        command = request.get("command")
                        if not isinstance(command, str) or command not in {"mcp", "show", "probe"}:
                            raise ValueError("Unsupported Desktop-MCP connection command.")
                        if active.exit_requested.is_set():
                            raise EOFError("Desktop-MCP is quitting.")
                        await channel.send(
                            json.dumps(
                                {
                                    "protocol": PROTOCOL,
                                    "pid": os.getpid(),
                                    "version": __version__,
                                    "instance": instance_id,
                                    "status": asdict(active.controller.snapshot()),
                                }
                            ).encode("utf-8")
                        )
                    if command == "show":
                        async with asyncio.timeout(5):
                            activation = json.loads(await channel.receive())
                        if activation != {"activate": True}:
                            raise ValueError("Invalid local launch request.")
                        # This reveals the LOCAL panel only. It never arms/resumes input.
                        await anyio.to_thread.run_sync(active.surface.show)
                        import pywintypes
                        import win32gui

                        windows = active.surface.window_handles()
                        if windows:
                            try:
                                win32gui.SetForegroundWindow(windows[0])
                            except pywintypes.error:
                                logger.info("Windows left the foreground with another application.")
                        await channel.send(b'{"shown":true}')
                    elif command == "mcp":
                        await _rpc_stream(channel, active)
                except EOFError, OSError, ValueError, TimeoutError:
                    logger.info("A local client could not complete its connection.")
                finally:
                    channel.close()
                    connections.discard(channel)

            async with anyio.create_task_group() as tasks:

                async def accept() -> None:
                    while True:
                        channel = await listener.accept()
                        if len(connections) >= 24:
                            channel.close()
                            continue
                        connections.add(channel)
                        tasks.start_soon(client, channel)

                tasks.start_soon(accept)
                while not active.exit_requested.is_set():
                    if not active.controller.snapshot().interface_ready:
                        raise RuntimeError(
                            "The local desktop interface failed. Open Desktop-MCP again."
                        )
                    await asyncio.sleep(0.05)
                # Record explicit Quit before dropping clients; reconnect attempts
                # must not undo the user's X click by reopening the app automatically.
                state.write(closed=True, version=__version__)
                tasks.cancel_scope.cancel()
        except Exception as error:
            state.write(
                closed=False,
                version=__version__,
                error=f"Desktop-MCP could not run: {type(error).__name__}: {error}",
            )
            raise


async def _handshake(channel: PipeChannel, command: str) -> dict[str, object]:
    async with asyncio.timeout(30):
        await channel.send(json.dumps({"protocol": PROTOCOL, "command": command}).encode("ascii"))
        result = json.loads(await channel.receive())
    if not isinstance(result, dict) or result.get("protocol") != PROTOCOL:
        raise RuntimeError("The running Desktop-MCP uses a different connection protocol.")
    if command == "mcp" and result.get("version") != __version__:
        raise RuntimeError(
            "A different Desktop-MCP version is still open. Close its window and reopen it."
        )
    return result


def _spawn_host(executable: Path) -> subprocess.Popen:
    """Never let one MCP client's kill-on-close job own everybody's GUI host."""
    try:
        return subprocess.Popen(
            [str(executable), "-m", "desktop_mcp", "host"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW | subprocess.CREATE_BREAKAWAY_FROM_JOB,
        )
    except OSError as error:
        if error.winerror == 5:
            raise RuntimeError(
                "This MCP client prevents an independent desktop application from starting. "
                "Open Desktop-MCP from Windows Start first, then reconnect in /mcp."
            ) from error
        raise


async def ensure_host(*, show: bool = False) -> tuple[PipeChannel, dict[str, object]]:
    """Connect first; serialize only startup so racing Copilot sessions share the host."""
    name = channel_name()
    state = ServiceState(name)
    try:
        channel = await connect(name)
    except FileNotFoundError:
        with InstanceLock(name, "startup", timeout=35):
            try:
                channel = await connect(name)
            except FileNotFoundError:
                previous = {} if show else state.read()
                if previous.get("closed"):
                    raise RuntimeError(
                        "Desktop-MCP was closed with X. Open Desktop-MCP from Start, "
                        "then reconnect it in Copilot's /mcp panel."
                    ) from None
                executable = Path(sys.executable).with_name("pythonw.exe")
                if not executable.is_file():
                    raise FileNotFoundError("The Desktop-MCP environment is missing pythonw.exe.")
                process = _spawn_host(executable)
                deadline = time.monotonic() + 30
                while True:
                    try:
                        channel = await connect(name)
                        break
                    except FileNotFoundError:
                        if process.poll() is not None:
                            failure = state.read().get("error")
                            raise RuntimeError(
                                str(
                                    failure
                                    or "Desktop-MCP exited during startup. Another old instance may "
                                    "own Ctrl+Shift+H; close it and open Desktop-MCP again."
                                )
                            ) from None
                        if time.monotonic() >= deadline:
                            raise TimeoutError(
                                "Desktop-MCP is still starting. Try opening it from Start; "
                                "use desktop-mcp doctor for its current status."
                            ) from None
                        await asyncio.sleep(0.1)
    try:
        info = await _handshake(channel, "show" if show else "mcp")
        if show:
            # Start activation rights belong to the launcher process; forward them
            # to the host, then ask Windows to focus only our existing control panel.
            import ctypes
            from ctypes import wintypes

            allow = ctypes.WinDLL("user32", use_last_error=True).AllowSetForegroundWindow
            allow.argtypes, allow.restype = [wintypes.DWORD], wintypes.BOOL
            if not allow(int(info["pid"])):
                logger.info("Windows did not transfer foreground permission to Desktop-MCP.")
            await channel.send(b'{"activate":true}')
            async with asyncio.timeout(5):
                reply = json.loads(await channel.receive())
            if reply != {"shown": True}:
                raise RuntimeError("Desktop-MCP could not reveal its control window.")
        return channel, info
    except EOFError as error:
        channel.close()
        raise RuntimeError(
            "Desktop-MCP closed during connection. Open it from Start and reconnect in /mcp."
        ) from error
    except BaseException:
        channel.close()
        raise


async def doctor() -> dict[str, object]:
    """Read lifecycle metadata without starting or arming the application."""
    name = channel_name()
    try:
        channel = await connect(name)
    except FileNotFoundError:
        return {"running": False, "state": ServiceState(name).read(), "python": sys.executable}
    try:
        return {"running": True, **await _handshake(channel, "probe"), "python": sys.executable}
    finally:
        channel.close()
