import base64
import io
import sys

from fastmcp import Client
from fastmcp.client.transports import StdioTransport
from PIL import Image
import pytest

from desktop_mcp.app import create_server
from desktop_mcp.contracts import CaptureContext, Observation
from desktop_mcp.runtime import Controller
from tests.test_desktop_runtime import FakeInput


class FixtureVision:
    def __init__(self, controller):
        self.controller = controller
        self.calls = 0

    def observe(self, **kwargs):
        self.controller.checkpoint()
        self.calls += 1
        image = Image.new("RGB", (40, 30), "#727272")
        image.putpixel((7, 9), (10, 20, 30))
        output = io.BytesIO()
        image.save(output, format="PNG")
        frame_id = f"synthetic-{self.calls}"
        return Observation(
            frame_id,
            {"frame_id": frame_id, "image_size": [40, 30], "image_changed": True},
            output.getvalue(),
            "image/png",
        )

    def context_for(self, frame):
        return CaptureContext(1, (0, 0, 1000, 1000), (0, 0, 1000, 1000))

    def resolve(self, frame, point):
        return 10, 10

    def invalidate(self):
        pass


class FixtureApplication:
    def __init__(self, armed=False):
        self.backend = FakeInput()
        self.controller = Controller(self.backend)
        self.controller.set_interface_ready(True)
        if armed:
            self.controller.arm_local()
        self.vision = FixtureVision(self.controller)

    def start(self):
        pass

    def close(self):
        self.controller.close()

    def windows(self):
        return []

    def displays(self):
        return [{"bounds": [0, 0, 1000, 1000], "dpi": 96}]

    def accessibility_tree(self, **kwargs):
        return "Synthetic button at (10, 10)"


EXPECTED_TOOLS = {
    "DesktopStatus",
    "DesktopStop",
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
}


async def test_supervised_surface_has_no_arm_or_raw_system_tools():
    application = FixtureApplication()
    async with Client(create_server(application)) as client:
        tools = await client.list_tools()
        assert {tool.name for tool in tools} == EXPECTED_TOOLS
        result = await client.call_tool("DesktopStatus")
        assert result.data["state"] == "stopped"


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
