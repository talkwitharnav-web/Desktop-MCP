import asyncio
import base64
from io import BytesIO
from types import SimpleNamespace

from fastmcp import Client
from PIL import Image
import pytest

from desktop_mcp.app import DesktopApplication, create_server
from desktop_mcp.capture import ForegroundUnavailable
from desktop_mcp.interaction import DesktopInUse, Interaction, RequestActor, request_actor
from desktop_mcp.runtime import Controller, DesktopStopped
from tests.test_desktop_runtime import FakeInput
from tests.test_desktop_tools import FixtureApplication
from tests.test_desktop_service import host, transport


def armed_interaction():
    controller = Controller(FakeInput())
    controller.set_interface_ready(True)
    controller.arm_local()
    return controller, Interaction(controller)


def test_task_owner_persists_across_calls_and_foreign_release_cannot_stop_it():
    controller, interaction = armed_interaction()
    generation = controller.snapshot().generation
    interaction.claim("coordinator", generation=generation, task="Manual slides")
    with pytest.raises(DesktopInUse):
        interaction.claim("research", generation=generation)
    assert not interaction.release("research", disconnected=True)
    assert controller.snapshot().armed
    with pytest.raises(DesktopInUse):
        interaction.release("research")
    assert interaction.release("coordinator")
    assert not controller.snapshot().armed
    controller.arm_local()
    interaction.claim("new-owner", generation=controller.snapshot().generation)


def test_stale_request_cannot_claim_after_local_stop_and_rearm():
    controller, interaction = armed_interaction()
    old = controller.snapshot().generation
    controller.stop()
    controller.arm_local()
    with pytest.raises(DesktopStopped):
        interaction.claim("old-request", generation=old)
    assert interaction.status()["owner"] is None


async def test_second_mcp_client_cannot_interleave_actions_or_stop_owner_by_disconnecting(tmp_path):
    async with host(tmp_path) as (app, name, _):
        app.controller.arm_local()  # Fake backend only.
        async with Client(transport(name)) as coordinator:
            await coordinator.call_tool(
                "DesktopControl", {"action": "claim", "task": "Manual slides"}
            )
            async with Client(transport(name)) as helper:
                refused = await helper.call_tool(
                    "Type",
                    {"text": "out-of-scope helper edit", "observe": False},
                    raise_on_error=False,
                )
                assert refused.is_error
                assert "Another MCP session" in refused.content[0].text
            assert app.controller.snapshot().armed
            assert app.backend.events == []
            await coordinator.call_tool("Type", {"text": "coordinator edit", "observe": False})
            assert app.backend.events == [("text", "coordinator edit")]
            await coordinator.call_tool("DesktopControl", {"action": "release"})
            assert not app.controller.snapshot().armed


async def test_unanswered_correction_blocks_new_input_but_not_observation_or_chat():
    app = FixtureApplication(armed=True)
    async with Client(create_server(app)) as client:
        app.teaching.conversation.send_user("Please keep this manual. Do not upload.")
        refused = await client.call_tool(
            "Type", {"text": "old plan", "observe": False}, raise_on_error=False
        )
        assert refused.is_error and "unanswered transcript" in refused.content[0].text
        assert app.controller.snapshot().armed
        assert app.backend.events == []
        assert not (await client.call_tool("Screenshot")).is_error
        question = await client.call_tool("TranscriptRead", {"timeout": 0.0})
        await client.call_tool(
            "Transcript",
            {
                "text": "Understood, continuing manually.",
                "reply_to": question.data["message"]["id"],
            },
        )
        await client.call_tool("Type", {"text": "manual edit", "observe": False})
        assert app.backend.events == [("text", "manual edit")]


async def test_unobserved_input_prevents_a_long_idle_transcript_wait():
    app = FixtureApplication(armed=True)
    async with Client(create_server(app)) as client:
        result = await client.call_tool("Type", {"text": "edit", "observe": False})
        assert result.structured_content["observation_due"]
        waiting = await asyncio.wait_for(client.call_tool("TranscriptRead", {"timeout": 25.0}), 1)
        assert waiting.data["observation_due"]
        assert waiting.data["wait_skipped"]
        await client.call_tool("Screenshot")
        assert not (await client.call_tool("DesktopStatus")).data["interaction"]["observation_due"]


async def test_input_completion_wakes_a_read_that_was_already_waiting_for_chat():
    app = FixtureApplication(armed=True)
    async with Client(create_server(app)) as client:
        reading = asyncio.create_task(client.call_tool("TranscriptRead", {"timeout": 25.0}))
        try:
            for _ in range(100):
                if app.teaching.conversation.status()["listener_waiting"]:
                    break
                await asyncio.sleep(0.01)
            await client.call_tool("Type", {"text": "finished edit", "observe": False})
            result = await asyncio.wait_for(reading, 1)
            assert result.data["interrupted"] == "observation_due"
            assert result.data["observation_due"]
        finally:
            if not reading.done():
                reading.cancel()
                await asyncio.gather(reading, return_exceptions=True)


async def test_failed_followup_export_does_not_claim_an_observation_was_delivered():
    app = FixtureApplication(armed=True)
    app.export_frames = True

    def failed_export(observation):
        raise OSError("Owned test export failed")

    app.export_observation = failed_export
    async with Client(create_server(app)) as client:
        failure = await client.call_tool("Type", {"text": "already edited"}, raise_on_error=False)
        assert failure.is_error
        assert "1 action(s) completed" in failure.content[0].text
        assert (await client.call_tool("DesktopStatus")).data["interaction"]["observation_due"]


def test_auto_image_reuse_never_assumes_another_client_received_the_image():
    app = FixtureApplication(armed=True)
    with request_actor(RequestActor("one", "1", "Screenshot")), app.controller.operation("frame"):
        app.interaction.record_observation(app.vision.observe())
        assert app.interaction.observation_reference("active")
    with request_actor(RequestActor("two", "2", "Click")):
        assert app.interaction.observation_reference("active") is None
    app.controller.stop()
    app.controller.arm_local()
    with request_actor(RequestActor("one", "3", "Click")):
        assert app.interaction.observation_reference("active") is None
    app.close()


async def test_screenshot_schema_advertises_real_bounds_and_rejects_before_capture():
    app = FixtureApplication(armed=True)
    async with Client(create_server(app)) as client:
        screenshot = next(tool for tool in await client.list_tools() if tool.name == "Screenshot")
        schema = screenshot.inputSchema["properties"]
        assert schema["wait_for_change"]["minimum"] == 0
        assert schema["wait_for_change"]["maximum"] == 5
        assert schema["settle"]["maximum"] == 1
        assert schema["max_dimension"]["maximum"] == 4096
        rejected = await client.call_tool(
            "Screenshot", {"wait_for_change": 20}, raise_on_error=False
        )
        assert rejected.is_error and app.vision.calls == 0


async def test_observation_text_is_concise_with_full_metadata_available_on_request():
    app = FixtureApplication(armed=True)
    async with Client(create_server(app)) as client:
        compact = await client.call_tool("Screenshot")
        assert not compact.content[0].text.startswith("{")
        assert compact.structured_content["frame_id"] in compact.content[0].text
        assert any(content.type == "image" for content in compact.content)
        full = await client.call_tool("Screenshot", {"detail": "full"})
        assert full.content[0].text.startswith("{")
        assert full.structured_content["observation"]


async def test_post_input_observation_catches_a_briefly_delayed_application_update():
    from desktop_mcp.vision import VisionService
    from tests.test_desktop_vision import Clock, Provider

    app = FixtureApplication(armed=True)
    clock = Clock()
    provider = Provider(clock)
    app.backend.window = provider.current.window_id
    app.capture = provider
    app.vision = VisionService(
        provider,
        revision=lambda: app.controller.input_revision,
        checkpoint=app.controller.checkpoint,
        wait=clock.wait,
        clock=clock,
    )
    async with Client(create_server(app)) as client:
        await client.call_tool("Screenshot", {"settle": 0.0})
        start = clock.value
        provider.render = lambda bounds, count: Image.new(
            "RGB",
            (bounds[2] - bounds[0], bounds[3] - bounds[1]),
            (36, 40, 44) if clock.value < start + 0.2 else (10, 90, 160),
        )
        result = await client.call_tool("Type", {"text": "delayed application repaint"})
        observation = result.structured_content["observation"]
        assert observation["capture_count"] > 1
        assert start + 0.2 <= observation["captured_at"] <= start + 0.36
        image = next(content for content in result.content if content.type == "image")
        with Image.open(BytesIO(base64.b64decode(image.data))) as decoded:
            assert decoded.getpixel((1, 1)) == (10, 90, 160)
        assert result.structured_content["application_outcome"] == "unverified"


def test_post_focus_recovery_only_observes_and_never_steals_a_new_foreground(monkeypatch):
    import desktop_mcp.app as app_module

    app = DesktopApplication.__new__(DesktopApplication)
    clock = [1.0]
    foreground = [0, 0, 42, 42]
    calls = []
    app.controller = SimpleNamespace(
        checkpoint=lambda: None,
        wait=lambda seconds: clock.__setitem__(0, clock[0] + seconds),
    )
    app.backend = SimpleNamespace(foreground=lambda: foreground.pop(0) if foreground else 42)
    app.vision = SimpleNamespace(observe=lambda: calls.append("capture") or "image")
    monkeypatch.setattr(app_module.time, "monotonic", lambda: clock[0])
    assert app.observe_focused(42) == "image"
    assert calls == ["capture"]
    app.backend.foreground = lambda: 99
    with pytest.raises(RuntimeError, match="different window"):
        app.observe_focused(42)
    assert calls == ["capture"]


def test_post_focus_absence_is_bounded_and_does_not_repeat_the_focus(monkeypatch):
    import desktop_mcp.app as app_module

    app = DesktopApplication.__new__(DesktopApplication)
    clock = [1.0]
    app.controller = SimpleNamespace(
        checkpoint=lambda: None,
        wait=lambda seconds: clock.__setitem__(0, clock[0] + seconds),
    )
    app.backend = SimpleNamespace(foreground=lambda: 42)
    app.vision = SimpleNamespace(observe=lambda: (_ for _ in ()).throw(ForegroundUnavailable()))
    monkeypatch.setattr(app_module.time, "monotonic", lambda: clock[0])
    with pytest.raises(TimeoutError):
        app.observe_focused(42)
    assert clock[0] <= 1.51
