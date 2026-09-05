import base64
import asyncio
import io
import sys
from dataclasses import replace
from types import SimpleNamespace

from fastmcp import Client
from fastmcp.client.transports import StdioTransport
from PIL import Image
import pytest

from desktop_mcp.app import create_server
from desktop_mcp.contracts import CaptureContext, Observation
from desktop_mcp.runtime import Controller
from desktop_mcp.image_files import ImageFiles
from desktop_mcp.teaching import TeachingSession
from desktop_mcp.interaction import Interaction, host_identity
from tests.test_desktop_runtime import FakeInput


class FixtureVision:
    def __init__(self, controller):
        self.controller = controller
        self.calls = 0
        self.contexts = {}

    def observe(self, **kwargs):
        self.controller.checkpoint()
        self.calls += 1
        image = Image.new("RGB", (40, 30), "#727272")
        image.putpixel((7, 9), (10, 20, 30))
        output = io.BytesIO()
        image.save(output, format="PNG")
        frame_id = f"synthetic-{self.calls}"
        self.contexts[frame_id] = CaptureContext(
            1, (0, 0, 1000, 1000), (0, 0, 1000, 1000), scope=kwargs.get("scope", "active")
        )
        return Observation(
            frame_id,
            {"frame_id": frame_id, "image_size": [40, 30], "image_changed": True},
            output.getvalue(),
            "image/png",
        )

    def context_for(self, frame):
        return self.contexts[frame]

    def resolve(self, frame, point):
        return 10, 10

    def invalidate(self):
        self.contexts.clear()


class FixtureApplication:
    def __init__(self, armed=False):
        self.backend = FakeInput()
        self.controller = Controller(self.backend)
        self.controller.set_interface_ready(True)
        if armed:
            self.controller.arm_local()
        self.interaction = Interaction(self.controller)
        self.host_info = host_identity()
        self.vision = FixtureVision(self.controller)
        self.capture = SimpleNamespace(
            context=lambda: CaptureContext(1, (0, 0, 1000, 1000), (0, 0, 1000, 1000))
        )
        self.teaching_context = lambda expected: self.capture.context()
        self.teaching = TeachingSession(
            self.controller, position=self.backend.position, context=self.teaching_context
        )
        self.teaching_surface = SimpleNamespace(visible=True, enabled=True)

        def visible(value):
            self.teaching_surface.visible = self.teaching_surface.enabled = value

        self.teaching_surface.show = lambda stacking: visible(True)
        self.teaching_surface.set_visible = visible
        self.export_frames = False
        self.image_files = ImageFiles()

    def start(self):
        pass

    def close(self):
        self.controller.close()
        self.image_files.close()

    def export_observation(self, observation):
        return self.image_files.export(observation)

    def windows(self):
        return []

    def displays(self):
        return [{"bounds": [0, 0, 1000, 1000], "dpi": 96}]

    def accessibility_tree(self, **kwargs):
        return "Synthetic button at (10, 10)"


EXPECTED_TOOLS = {
    "DesktopStatus",
    "DesktopStop",
    "DesktopControl",
    "DesktopBatch",
    "Screenshot",
    "Click",
    "Move",
    "Scroll",
    "Keyboard",
    "Shortcut",
    "Type",
    "Wait",
    "App",
    "DisplayInventory",
    "Snapshot",
    "Transcript",
    "TranscriptRead",
    "Laser",
    "Draw",
    "Erase",
    "Cursor",
    "WaitForCursor",
}


async def test_supervised_surface_has_no_arm_or_raw_system_tools():
    application = FixtureApplication()
    async with Client(create_server(application)) as client:
        tools = await client.list_tools()
        assert {tool.name for tool in tools} == EXPECTED_TOOLS
        result = await client.call_tool("DesktopStatus")
        assert result.data["state"] == "stopped"


async def test_zero_duration_coordinate_batch_fails_before_its_first_key():
    application = FixtureApplication(armed=True)
    async with Client(create_server(application)) as client:
        result = await client.call_tool(
            "DesktopBatch",
            {
                "actions": [
                    {"kind": "key", "keys": ["a"]},
                    {"kind": "scroll", "loc": [900, 10], "duration": 0.0, "delta_y": -120},
                ],
                "observe": False,
            },
            raise_on_error=False,
        )
        assert result.is_error
        assert application.backend.events == []


@pytest.mark.parametrize("phase", ["tree", "image", "export"])
@pytest.mark.parametrize("change", ["window", "input", "none"])
async def test_snapshot_rejects_context_or_input_changes_between_compound_phases(phase, change):
    from desktop_mcp.vision import VisionService
    from tests.test_desktop_vision import Clock, Provider

    application = FixtureApplication(armed=True)
    application.controller.set_human_takeover(False)
    clock = Clock()
    provider = Provider(clock)
    application.capture = provider
    application.vision = VisionService(
        provider,
        revision=lambda: application.controller.input_revision,
        checkpoint=application.controller.checkpoint,
        wait=clock.wait,
        clock=clock,
    )

    def change_context():
        if change == "window":
            provider.current = replace(provider.current, window_id=29)
        elif change == "input":
            application.controller.notify_human_input(kind="key")

    def tree(**kwargs):
        if phase == "tree":
            change_context()
        return "Window1 Save button"

    observe = application.vision.observe

    def image(**kwargs):
        result = observe(**kwargs)
        if phase == "image":
            change_context()
        return result

    application.accessibility_tree = tree
    application.vision.observe = image
    if phase == "export":
        application.export_frames = True

        def export(observation):
            change_context()
            return observation

        application.export_observation = export
    async with Client(create_server(application)) as client:
        result = await client.call_tool("Snapshot", {}, raise_on_error=False)
        if change == "none":
            assert not result.is_error
            assert result.structured_content["accessibility_tree"] == "Window1 Save button"
            assert result.structured_content["observation"]["window_id"] == 17
            assert any(block.type == "image" for block in result.content)
        else:
            assert result.is_error
            assert not any(block.type == "image" for block in result.content)
        assert application.controller.snapshot().armed


@pytest.mark.parametrize(
    "name,arguments",
    [
        ("Screenshot", {}),
        ("Click", {"loc": [10, 10]}),
        ("Move", {"loc": [20, 20]}),
        ("Scroll", {}),
        ("Keyboard", {"keys": ["win"]}),
        ("Shortcut", {"shortcut": "win"}),
        ("Type", {"text": "must not type"}),
        ("DesktopBatch", {"actions": [{"kind": "text", "text": "must not type"}]}),
        ("Wait", {"duration": 0}),
        ("App", {}),
        ("DisplayInventory", {}),
        ("Snapshot", {}),
        ("Laser", {"loc": [10, 10]}),
        ("Draw", {"kind": "path", "points": [[10, 10], [20, 20]]}),
        ("Erase", {}),
        ("Cursor", {}),
        ("WaitForCursor", {"loc": [10, 10]}),
    ],
)
async def test_every_desktop_surface_obeys_the_stop_gate(name, arguments):
    application = FixtureApplication()
    async with Client(create_server(application)) as client:
        result = await client.call_tool(name, arguments, raise_on_error=False)
        assert result.is_error
    assert application.backend.events == []
    assert application.vision.calls == 0


async def test_batch_returns_one_image_after_multiple_actions():
    application = FixtureApplication(armed=True)
    async with Client(create_server(application)) as client:
        result = await client.call_tool(
            "DesktopBatch",
            {
                "actions": [
                    {"kind": "key", "keys": ["ctrl", "a"]},
                    {"kind": "text", "text": "Fast Unicode \u03bb"},
                    {"kind": "key", "keys": ["enter"]},
                ],
            },
        )
        images = [content for content in result.content if content.type == "image"]
        assert len(images) == 1
        decoded = Image.open(io.BytesIO(base64.b64decode(images[0].data)))
        assert decoded.size == (40, 30)
        assert decoded.getpixel((7, 9)) == (10, 20, 30)
        assert len(result.structured_content["actions"]) == 3
        assert application.vision.calls == 1


async def test_stop_latches_across_subsequent_mcp_calls():
    application = FixtureApplication(armed=True)
    async with Client(create_server(application)) as client:
        result = await client.call_tool("DesktopStop")
        assert result.data["state"] == "stopped"
        for _ in range(3):
            rejected = await client.call_tool("Keyboard", {"keys": ["enter"]}, raise_on_error=False)
            assert rejected.is_error
    assert application.backend.events == []


async def test_malformed_input_never_reaches_backend():
    application = FixtureApplication(armed=True)
    async with Client(create_server(application)) as client:
        result = await client.call_tool(
            "DesktopBatch",
            {
                "actions": [
                    {"kind": "click", "loc": [10, 10]},
                    {"kind": "key", "keys": ["nonexistent"]},
                ],
            },
            raise_on_error=False,
        )
        assert result.is_error
    assert application.backend.events == []


async def test_observation_failure_does_not_disguise_completed_input():
    application = FixtureApplication(armed=True)

    def unavailable(**kwargs):
        raise RuntimeError("Capture unavailable")

    application.vision.observe = unavailable
    async with Client(create_server(application)) as client:
        result = await client.call_tool("Type", {"text": "already entered"}, raise_on_error=False)
        assert result.is_error
        assert "1 action(s) completed" in result.content[0].text
        assert "Do not replay" in result.content[0].text
    assert ("text", "already entered") in application.backend.events


async def test_client_can_request_a_local_file_without_losing_the_image_block():
    from pathlib import Path

    application = FixtureApplication(armed=True)
    async with Client(create_server(application)) as client:
        result = await client.call_tool("Screenshot", {"export_image": True})
        path = Path(result.structured_content["observation"]["image_path"])
        assert path.is_absolute()
        with Image.open(path) as image:
            assert image.getpixel((7, 9)) == (10, 20, 30)
        assert any(content.type == "image" for content in result.content)
    assert not path.exists()


async def test_rpc_waiting_before_the_worker_cannot_revive_after_resume():
    from fastmcp.server.middleware import Middleware

    entered = asyncio.Event()
    release = asyncio.Event()

    class DelayedTool(Middleware):
        async def on_call_tool(self, context, call_next):
            if context.message.name == "Type":
                entered.set()
                await release.wait()
            return await call_next(context)

    application = FixtureApplication(armed=True)
    server = create_server(application)
    server.add_middleware(DelayedTool())
    async with Client(server) as client:
        pending = asyncio.create_task(
            client.call_tool("Type", {"text": "old queued request"}, raise_on_error=False)
        )
        try:
            await asyncio.wait_for(entered.wait(), 2)
            await client.call_tool("DesktopStop")
            application.controller.arm_local()
        finally:
            release.set()
        result = await asyncio.wait_for(pending, 2)
        assert result.is_error
        assert "revoked" in result.content[0].text
    assert application.backend.events == []


async def test_real_stdio_transport_preserves_image_blocks():
    # The child uses only this synthetic application, never an OS input backend.
    script = (
        "from tests.test_desktop_tools import FixtureApplication; "
        "from desktop_mcp.app import create_server; "
        "create_server(FixtureApplication(armed=True)).run(transport='stdio', show_banner=False)"
    )
    async with Client(StdioTransport(command=sys.executable, args=["-c", script])) as client:
        result = await client.call_tool("Screenshot", {"settle": 0})
        images = [content for content in result.content if content.type == "image"]
        assert len(images) == 1
        assert images[0].mimeType == "image/png"
        decoded = Image.open(io.BytesIO(base64.b64decode(images[0].data)))
        assert decoded.getpixel((7, 9)) == (10, 20, 30)
        assert result.structured_content["frame_id"] == "synthetic-1"
