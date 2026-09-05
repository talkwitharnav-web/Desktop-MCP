"""The actual MCP tools can guide and operate in one locally armed session."""

from fastmcp import Client
from types import SimpleNamespace

from desktop_mcp.app import create_server
from desktop_mcp.contracts import CaptureContext
from desktop_mcp.teaching import TeachingSession
from tests.test_desktop_tools import FixtureApplication


def unified_application():
    app = FixtureApplication(armed=True)
    context = CaptureContext(1, (0, 0, 1000, 1000), (0, 0, 1000, 1000))
    app.teaching_context = lambda expected: context
    app.teaching = TeachingSession(
        app.controller, position=app.backend.position, context=app.teaching_context
    )
    app.teaching_surface = SimpleNamespace(show=lambda stacking: None)
    return app


async def test_explain_highlight_click_and_continue_without_switching_or_rearming():
    app = unified_application()
    generation = app.controller.snapshot().generation
    async with Client(create_server(app)) as client:
        await client.call_tool("Transcript", {"text": "This is the next tab."})
        await client.call_tool("Laser", {"bounds": [10, 10, 40, 40]})
        assert app.teaching.snapshot().marks
        await client.call_tool("Click", {"loc": [10, 10], "observe": False})
        await client.call_tool(
            "Transcript", {"text": "Now I can explain the controls in that tab."}
        )
        await client.call_tool("Type", {"text": "Example value", "observe": False})
        status = (await client.call_tool("DesktopStatus")).data
        assert status["state"] == "ready"
        assert status["generation"] == generation
        assert "mode" not in status
        assert len(app.teaching.snapshot().entries) == 2
        assert ("button", "left", True) in app.backend.events
        assert ("text", "Example value") in app.backend.events


async def test_learner_cursor_wait_then_agent_click_preserves_one_local_authorization():
    app = unified_application()
    original_wait = app.controller.wait
    observed_waits = []

    def learner_moves(duration):
        observed_waits.append(app.controller.snapshot().awaiting_user)
        original_wait(duration)
        # Fake physical movement, not a backend input event.
        app.backend.point = (50, 50)
        app.controller.notify_human_input(kind="move", position=(50, 50))

    app.controller.wait = learner_moves
    generation = app.controller.snapshot().generation
    async with Client(create_server(app)) as client:
        await client.call_tool("Transcript", {"text": "Move your cursor onto the highlighted tab."})
        result = await client.call_tool(
            "WaitForCursor", {"loc": [50, 50], "radius": 0.0, "dwell": 0.0, "timeout": 1.0}
        )
        assert result.data["status"] == "reached"
        assert observed_waits and all(observed_waits)
        assert not app.controller.snapshot().awaiting_user
        app.controller.wait = original_wait
        await client.call_tool("Click", {"loc": [50, 50], "observe": False})
        assert app.controller.snapshot().generation == generation
        assert app.controller.snapshot().armed
        await client.call_tool("DesktopStop")
        denied = await client.call_tool("Click", {"observe": False}, raise_on_error=False)
        assert denied.is_error
