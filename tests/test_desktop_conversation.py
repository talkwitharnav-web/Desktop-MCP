import asyncio

from fastmcp import Client
import pytest

from desktop_mcp.app import create_server
from desktop_mcp.conversation import Conversation, MAX_PENDING
from tests.test_desktop_tools import FixtureApplication


def chat():
    return Conversation(is_closed=lambda: False)


async def test_user_message_is_delivered_once_and_acknowledged_by_its_reply():
    conversation = chat()
    message = conversation.send_user("I see the button, but what does it do?")
    assert conversation.status()["pending_messages"] == 1
    assert not conversation.status()["listener_connected"]
    delivered = await conversation.listen("session-1", timeout=0)
    assert delivered["message"] == {"id": message.sequence, "text": message.text, "role": "user"}
    # Reading again before replying is idempotent and cannot lose the question.
    assert (await conversation.listen("session-1", timeout=0))["message"] == delivered["message"]
    with pytest.raises(ValueError):
        conversation.reply("Wrong recipient", reply_to=message.sequence, owner="session-2")
    conversation.reply("It adds an object.", reply_to=message.sequence, owner="session-1")
    assert conversation.status()["pending_messages"] == 0
    assert [entry.role for entry in conversation.entries()] == ["user", "assistant"]
    with pytest.raises(ValueError):
        conversation.reply("Duplicate", reply_to=message.sequence, owner="session-1")


async def test_waiting_reader_receives_new_ui_text_promptly():
    conversation = chat()
    waiting = asyncio.create_task(conversation.listen("session", timeout=1))
    await asyncio.sleep(0)
    assert conversation.status()["listener_waiting"]
    conversation.send_user("Hey, explain that again.")
    result = await asyncio.wait_for(waiting, 0.5)
    assert result["message"]["text"] == "Hey, explain that again."
    assert not conversation.status()["listener_waiting"]
    assert conversation.status()["awaiting_reply"]


async def test_another_client_cannot_take_an_active_conversation():
    conversation = chat()
    await conversation.listen("first", timeout=0)
    with pytest.raises(RuntimeError, match="Another"):
        await conversation.listen("second", timeout=0)
    message = conversation.send_user("Pending while the first client leaves")
    conversation.release_listener("first")
    assert (await conversation.listen("second", timeout=0))["message"]["id"] == message.sequence


async def test_cancelled_read_clears_waiting_and_disconnect_preserves_pending_text():
    conversation = chat()
    reading = asyncio.create_task(conversation.listen("first", timeout=30))
    await asyncio.sleep(0)
    reading.cancel()
    with pytest.raises(asyncio.CancelledError):
        await reading
    assert not conversation.status()["listener_waiting"]
    conversation.send_user("Don't lose this.")
    conversation.release_listener("first")
    assert conversation.status()["pending_messages"] == 1
    assert not conversation.status()["listener_connected"]


def test_queue_overflow_is_explicit_and_does_not_replace_user_messages():
    conversation = chat()
    for index in range(MAX_PENDING):
        conversation.send_user(f"message {index}")
    with pytest.raises(ValueError, match="queue is full"):
        conversation.send_user("This draft must remain in the editor")
    assert conversation.status()["pending_messages"] == MAX_PENDING
    for invalid in ("", "\ud800", "x" * 16001):
        with pytest.raises(ValueError):
            chat().send_user(invalid)


async def test_closed_app_refuses_messages_and_wakes_a_waiter():
    closed = [False]
    conversation = Conversation(is_closed=lambda: closed[0])
    waiting = asyncio.create_task(conversation.listen("session", timeout=30))
    await asyncio.sleep(0)
    closed[0] = True
    with pytest.raises(RuntimeError, match="closed"):
        await asyncio.wait_for(waiting, 0.5)
    with pytest.raises(RuntimeError):
        conversation.send_user("Too late")


async def test_real_mcp_conversation_works_while_desktop_is_stopped_without_arming():
    app = FixtureApplication()
    async with Client(create_server(app)) as client:
        reading = asyncio.create_task(client.call_tool("TranscriptRead", {"timeout": 1.0}))
        await asyncio.sleep(0.05)
        question = app.teaching.conversation.send_user("What does that Blender button do?")
        result = await asyncio.wait_for(reading, 2)
        assert result.data["message"]["id"] == question.sequence
        response = await client.call_tool(
            "Transcript", {"text": "It adds a new object.", "reply_to": question.sequence}
        )
        assert not response.is_error
        status = (await client.call_tool("DesktopStatus")).data
        assert status["state"] == "stopped"
        assert status["transcript"]["pending_messages"] == 0
        assert status["transcript"]["visible"]
        assert app.backend.events == []
        blocked = await client.call_tool("Click", {"observe": False}, raise_on_error=False)
        assert blocked.is_error
        await client.call_tool("Transcript", {"action": "hide"})
        assert not (await client.call_tool("DesktopStatus")).data["transcript"]["visible"]
        await client.call_tool("Transcript", {"action": "show"})
        assert (await client.call_tool("DesktopStatus")).data["transcript"]["visible"]


async def test_expired_listener_lease_does_not_lose_the_unanswered_message():
    clock = [100.0]
    conversation = Conversation(is_closed=lambda: False, clock=lambda: clock[0])
    message = conversation.send_user("Still waiting")
    await conversation.listen("old", timeout=0)
    clock[0] += 121
    assert not conversation.status()["listener_connected"]
    current = await conversation.listen("new", timeout=0)
    assert current["message"]["id"] == message.sequence
    with pytest.raises(ValueError):
        conversation.reply("Old delayed response", reply_to=message.sequence, owner="old")


def test_close_wins_even_if_it_happens_after_initial_message_validation():
    conversation = chat()

    def closing_clock():
        conversation.close()
        return 1.0

    conversation._clock = closing_clock
    with pytest.raises(RuntimeError, match="closed"):
        conversation.send_user("Not after X")
    assert conversation.entries() == ()


async def test_listening_for_chat_does_not_block_desktop_tools():
    app = FixtureApplication(armed=True)
    async with Client(create_server(app)) as client:
        waiting = asyncio.create_task(client.call_tool("TranscriptRead", {"timeout": 2.0}))
        try:
            for _ in range(100):
                if app.teaching.conversation.status()["listener_waiting"]:
                    break
                await asyncio.sleep(0.01)
            assert app.teaching.conversation.status()["listener_waiting"]
            result = await asyncio.wait_for(
                client.call_tool("Type", {"text": "A demonstration", "observe": False}), 1
            )
            assert not result.is_error
            assert not waiting.done()
            app.teaching.conversation.send_user("Wait, what does that mean?")
            assert (await waiting).data["message"]["text"] == "Wait, what does that mean?"
        finally:
            if not waiting.done():
                waiting.cancel()
                await asyncio.gather(waiting, return_exceptions=True)
