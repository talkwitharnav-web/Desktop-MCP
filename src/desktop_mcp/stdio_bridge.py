"""Small MCP stdio bridge; the Windows interface lives in the shared host."""

from __future__ import annotations

import asyncio
import os
import queue
import threading

from desktop_mcp.pipe_transport import MAX_PACKET, PipeChannel
from desktop_mcp.service import ensure_host


async def forward_stdio(channel: PipeChannel, input_fd: int = 0, output_fd: int = 1) -> None:
    """A closed host exits the bridge even while Copilot still holds stdin open."""
    incoming: queue.Queue[bytes] = queue.Queue(maxsize=8)
    outgoing: queue.Queue[bytes] = queue.Queue(maxsize=2)
    stopping = threading.Event()
    input_done = threading.Event()
    output_failed = threading.Event()
    errors: list[Exception] = []

    def enqueue(target: queue.Queue[bytes], packet: bytes) -> bool:
        while not stopping.is_set():
            try:
                target.put(packet, timeout=0.05)
                return True
            except queue.Full:
                continue
        return False

    def read_input() -> None:
        buffer = bytearray()
        try:
            while not stopping.is_set():
                data = os.read(input_fd, 65536)
                if not data:
                    if buffer.strip():
                        enqueue(incoming, bytes(buffer))
                    return
                buffer.extend(data)
                while (end := buffer.find(b"\n")) != -1:
                    if end > MAX_PACKET:
                        raise ValueError("An MCP request exceeds the 8 MiB message limit.")
                    packet = bytes(buffer[:end])
                    del buffer[: end + 1]
                    if packet.strip() and not enqueue(incoming, packet):
                        return
                if len(buffer) > MAX_PACKET:
                    raise ValueError("An MCP request exceeds the 8 MiB message limit.")
        except (OSError, ValueError) as error:
            errors.append(error)
        finally:
            input_done.set()

    def write_output() -> None:
        try:
            while not stopping.is_set():
                try:
                    packet = outgoing.get(timeout=0.05)
                except queue.Empty:
                    continue
                data = memoryview(packet + b"\n")
                while data and not stopping.is_set():
                    count = os.write(output_fd, data)
                    if count == 0:
                        raise BrokenPipeError("The MCP client stopped reading.")
                    data = data[count:]
        except OSError as error:
            errors.append(error)
            output_failed.set()

    # Raw descriptor I/O avoids Python's buffered-stdin finalization lock.
    # These threads are process-local transport helpers, never desktop workers.
    threading.Thread(target=read_input, name="MCP stdin", daemon=True).start()
    threading.Thread(target=write_output, name="MCP stdout", daemon=True).start()

    async def send() -> None:
        while not output_failed.is_set():
            try:
                packet = incoming.get_nowait()
            except queue.Empty:
                if input_done.is_set():
                    return
                await asyncio.sleep(0.005)
            else:
                await channel.send(packet)

    async def receive() -> None:
        while not output_failed.is_set():
            packet = await channel.receive()
            while not output_failed.is_set():
                try:
                    outgoing.put_nowait(packet)
                    break
                except queue.Full:
                    await asyncio.sleep(0.005)

    tasks = [asyncio.create_task(send()), asyncio.create_task(receive())]
    try:
        finished, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in finished:
            try:
                task.result()
            except EOFError:
                pass  # X closed the host; do not respawn or replay any request.
        if errors and not isinstance(errors[0], BrokenPipeError):
            raise errors[0]
    finally:
        stopping.set()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        channel.close()


async def run_bridge() -> None:
    channel, _ = await ensure_host()
    await forward_stdio(channel)
