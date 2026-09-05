import asyncio
import json

from fastmcp import Client
import pytest

from desktop_mcp.app import create_server
from desktop_mcp.diagnostics import call_diagnostics, input_delivery, validated_frame
from desktop_mcp.native import TargetDenied
from desktop_mcp.vision import VisionService
from tests.test_desktop_tools import FixtureApplication
from tests.test_desktop_vision import Clock, Provider
from tests.test_desktop_window_targets import WindowPort, backend_for


def guarded_application():
    app = FixtureApplication(armed=True)
    port = WindowPort()
    port.add(1, owned=False)
    port.foreground = port.hit = 1
    port.add(20)
    port.add(21, root=20, bounds=(20, 20, 180, 180))
    native = backend_for(port)
    app.backend.ensure_target = native.ensure_target
    app.backend.protected_windows = native.protected_windows
    return app, native, port


async def test_diagnostic_evidence_is_isolated_between_concurrent_calls():
    async def collect(frame, completed):
        with call_diagnostics() as evidence:
            validated_frame(frame)
            input_delivery(completed, delivery="partial")
            await asyncio.sleep(0)
            return evidence

    first, second = await asyncio.gather(collect("accepted-one", 1), collect("accepted-two", 2))
    assert first.frame_ids == {"accepted-one"}
    assert second.frame_ids == {"accepted-two"}
    assert first.input["completed_actions"] == 1
    assert second.input["completed_actions"] == 2
    with call_diagnostics() as fresh:
        assert fresh.frame_ids == set()
        assert fresh.input is None


async def test_status_and_observation_explain_capture_excluded_rectangles():
    app = FixtureApplication(armed=True)
    protected = [
        {
            "role": "transcript-composer",
            "window_id": 21,
            "root_id": 20,
            "bounds": [100, 200, 500, 240],
            "visible": True,
            "minimized": False,
            "click_through": False,
        }
    ]
    app.backend.protected_windows = lambda: protected
    async with Client(create_server(app)) as client:
        status = (await client.call_tool("DesktopStatus")).data
        assert status["protected_windows"] == protected
        result = await client.call_tool("Screenshot")
        assert result.structured_content["observation"]["protected_windows"] == protected
        assert len([block for block in result.content if block.type == "image"]) == 1
        assert "transcript-composer" not in result.content[0].text


async def test_arbitrary_exception_details_are_not_returned_or_retained():
    app = FixtureApplication(armed=True)

    class UnrelatedFailure(RuntimeError):
        details = {"composer": "private fixture draft", "window_text": "private fixture title"}

    def fail(**kwargs):
        raise UnrelatedFailure("Opaque fixture failure")

    app.vision.observe = fail
    async with Client(create_server(app)) as client:
        result = await client.call_tool("Screenshot", raise_on_error=False)
        assert result.is_error
        assert "Opaque fixture failure" in result.content[0].text
        assert "private fixture" not in json.dumps(result.structured_content)
        assert "private fixture" not in result.content[0].text
        status = (await client.call_tool("DesktopStatus")).data
        assert status["interaction"]["last_denial"] is None


@pytest.mark.parametrize("tool", ["Click", "DesktopBatch"])
async def test_denial_preserves_mapped_point_validated_frame_and_mcp_caller(tool):
    app, native, port = guarded_application()
    clock = Clock()
    source = Provider(clock)
    port.add(17, owned=False)
    port.foreground = app.backend.window = 17
    app.vision = VisionService(
        source,
        revision=lambda: app.controller.input_revision,
        checkpoint=app.controller.checkpoint,
        wait=clock.wait,
        clock=clock,
    )
    async with Client(create_server(app)) as client:
        frame = await client.call_tool("Screenshot", {"max_dimension": 100, "settle": 0})
        frame_id = frame.structured_content["frame_id"]
        port.hit = 21
        arguments = {"loc": [50, 30], "frame_id": frame_id}
        if tool == "DesktopBatch":
            arguments = {"actions": [{"kind": "click", **arguments}]}
        result = await client.call_tool(tool, arguments, raise_on_error=False)
        assert result.is_error and result.structured_content["is_error"]
        denial = result.structured_content["denial"]
        assert denial["target_point"] == [110, 80]
        assert denial["matched"]["role"] == "transcript-composer"
        assert denial["matched"]["root_id"] == 20
        assert denial["matched"]["bounds"] == [20, 20, 180, 180]
        assert denial["matched"]["capture_excluded"] is True
        assert denial["frame_ids"] == [frame_id]
        assert denial["request"]["tool"] == tool
        assert denial["request"]["request_id"]
        assert denial["request"]["generation"] == app.controller.snapshot().generation
        assert denial["input"]["delivery"] == "not_started"
        assert denial["input"]["completed_actions"] == 0
        status = (await client.call_tool("DesktopStatus")).data
        assert status["interaction"]["last_denial"] == denial
        assert status["interaction"]["owner"]["session_id"] == denial["request"]["session_id"]
        assert status["state"] == "ready"
        assert not result.structured_content["observation_due"]
        assert "request" not in native.last_denial
        assert app.backend.events == []
        assert [block.type for block in result.content] == ["text"]


async def test_batch_wrapper_retains_partial_delivery_warning_and_exact_count():
    app, _, port = guarded_application()
    app.backend.on_event = lambda event: port.gui.update({200: {"hwndFocus": 21}})
    async with Client(create_server(app)) as client:
        result = await client.call_tool(
            "DesktopBatch",
            {
                "actions": [
                    {"kind": "text", "text": "Known fixture text"},
                    {"kind": "key", "keys": ["escape"]},
                ]
            },
            raise_on_error=False,
        )
        assert result.is_error
        denial = result.structured_content["denial"]
        assert denial["routing"] == "keyboard_focus"
        assert denial["input"]["delivery"] == "partial"
        assert denial["input"]["completed_actions"] == 1
        assert denial["input"]["current_action_may_be_partial"]
        assert "Do not blindly replay" in result.content[0].text
        assert "1 completed action(s)" in result.content[0].text
        assert result.structured_content["observation_due"]
        assert app.backend.events == [("text", "Known fixture text")]


@pytest.mark.parametrize("tool", ["Type", "App"])
async def test_completed_input_capture_denial_never_implies_input_was_undone(tool):
    app, native, port = guarded_application()

    def after_input(event):
        port.foreground = 20

    def denied_observation(*args, **kwargs):
        native.ensure_observable_foreground()
        pytest.fail("The owned foreground should have been rejected")

    app.backend.on_event = after_input
    app.backend.focus = lambda handle: app.backend._record("focus", handle)
    app.observe_focused = denied_observation
    app.vision.observe = denied_observation
    arguments = (
        {"text": "Known fixture text"} if tool == "Type" else {"mode": "focus", "window_id": 1}
    )
    async with Client(create_server(app)) as client:
        result = await client.call_tool(tool, arguments, raise_on_error=False)
        assert result.is_error
        denial = result.structured_content["denial"]
        assert denial["operation"] == "observe_foreground"
        assert denial["input"]["delivery"] == "complete"
        assert denial["input"]["completed_actions"] == 1
        assert not denial["input"]["current_action_may_be_partial"]
        assert denial["input"]["application_outcome"] == "unverified"
        assert result.structured_content["observation_due"]
        assert "follow-up observation failed" in result.content[0].text
        assert "Do not replay the input" in result.content[0].text
        assert len(app.backend.events) == 1


async def test_unvalidated_frame_arguments_and_extra_error_fields_are_not_exposed():
    app, native, port = guarded_application()
    port.hit = 21

    def denied_context(frame):
        try:
            native.ensure_target((50, 50))
        except TargetDenied as error:
            error.details["draft"] = "private fixture draft"
            error.details["matched"]["window_text"] = "private fixture title"
            raise

    app.vision.context_for = denied_context
    async with Client(create_server(app)) as client:
        result = await client.call_tool(
            "Click",
            {"loc": [50, 50], "frame_id": "private fixture unvalidated reference"},
            raise_on_error=False,
        )
        assert result.is_error
        assert result.structured_content["denial"]["frame_ids"] == []
        assert "private fixture" not in json.dumps(result.structured_content)
        assert "private fixture" not in result.content[0].text
        status = (await client.call_tool("DesktopStatus")).data
        assert "private fixture" not in json.dumps(status["interaction"]["last_denial"])
