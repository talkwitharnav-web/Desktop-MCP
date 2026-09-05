"""Local named-pipe tests: synthetic bytes, no desktop windows or input."""

import asyncio
import uuid

import pytest

from desktop_mcp.pipe_transport import InstanceLock, PipeListener, channel_name, connect


@pytest.fixture
def endpoint():
    return f"Desktop-MCP-test-{uuid.uuid4().hex}"


async def pair(listener):
    pending = asyncio.create_task(listener.accept())
    client = await connect(listener.address.rsplit("\\", 1)[-1], timeout=1)
    return await asyncio.wait_for(pending, 2), client


@pytest.mark.parametrize("size", [1, 100, 65536, 65537, 1500000])
async def test_messages_survive_overlapped_pipe_boundaries(endpoint, size):
    listener = PipeListener(endpoint)
    server, client = await pair(listener)
    try:
        expected = b"x" * (size - 1) + b"z"
        sent = asyncio.create_task(server.send(expected))
        actual = await asyncio.wait_for(client.receive(), 5)
        await sent
        assert actual == expected
        await client.send(b"reply")
        assert await asyncio.wait_for(server.receive(), 2) == b"reply"
    finally:
        server.close()
        client.close()
        listener.close()


async def test_receive_and_accept_are_cancellable_without_stranded_threads(endpoint):
    listener = PipeListener(endpoint)
    server, client = await pair(listener)
    try:
        reading = asyncio.create_task(server.receive())
        accepting = asyncio.create_task(listener.accept())
        await asyncio.sleep(0.02)
        for task in (reading, accepting):
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, 1)
    finally:
        server.close()
        client.close()
        listener.close()


async def test_peer_exit_releases_receive(endpoint):
    listener = PipeListener(endpoint)
    server, client = await pair(listener)
    try:
        reading = asyncio.create_task(server.receive())
        await asyncio.sleep(0.01)
        client.close()
        with pytest.raises(EOFError):
            await asyncio.wait_for(reading, 1)
    finally:
        server.close()
        client.close()
        listener.close()


async def test_client_that_leaves_before_accept_does_not_break_listener(endpoint):
    listener = PipeListener(endpoint)
    early = await connect(endpoint)
    early.close()
    accepting = asyncio.create_task(listener.accept())
    await asyncio.sleep(0.02)
    channel = await connect(endpoint, timeout=1)
    try:
        server = await asyncio.wait_for(accepting, 2)
        try:
            await channel.send(b"still working")
            assert await server.receive() == b"still working"
        finally:
            server.close()
    finally:
        channel.close()
        listener.close()


async def test_two_simultaneous_clients_use_distinct_channels(endpoint):
    listener = PipeListener(endpoint)
    server1, client1 = await pair(listener)
    server2, client2 = await pair(listener)
    try:
        await asyncio.gather(client1.send(b"one"), client2.send(b"two"))
        assert await server1.receive() == b"one"
        assert await server2.receive() == b"two"
    finally:
        for channel in (server1, client1, server2, client2):
            channel.close()
        listener.close()


def test_endpoint_is_stable_per_user_session():
    assert channel_name() == channel_name()
    assert channel_name().startswith("Desktop-MCP-v1-")


def test_mutex_releases_after_exception(endpoint):
    with pytest.raises(RuntimeError):
        with InstanceLock(endpoint, "test"):
            raise RuntimeError("test")
    with InstanceLock(endpoint, "test"):
        pass
