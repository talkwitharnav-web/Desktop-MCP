from types import SimpleNamespace

import pytest

from desktop_mcp.contracts import CaptureContext
from desktop_mcp.runtime import DesktopStopped
from desktop_mcp.teaching_tools import register_teaching_tools
from desktop_mcp.teaching import TeachingSession
from tests.test_desktop_tools import FixtureApplication
from tests.test_desktop_vision import Rig


class ToolRegistry:
    def __init__(self):
        self.tools = {}

    def tool(self, *, name, **kwargs):
        def register(function):
            self.tools[name] = function
            return function

        return register


@pytest.fixture
def teaching_tools():
    app = FixtureApplication(armed=True)
    calls = []
    context = CaptureContext(1, (0, 0, 1000, 1000), (0, 0, 1000, 1000))

    def publish(text, *, title):
        calls.append(("publish", text, title))
        return SimpleNamespace(sequence=1, text=text)

    def draw(kind, points, **kwargs):
        calls.append(("draw", kind, points, kwargs))
        return "ink-1"

    def resolve_many(frame, points):
        calls.append(("resolve_many", frame, points))
        return [(100 + 2 * x, 50 + 2 * y) for x, y in points]

    app.vision.resolve_many = resolve_many
    app.vision.context_for = lambda frame: context
    app.teaching_context = lambda expected: context
    app.teaching = SimpleNamespace(
        publish=publish,
        draw=draw,
        erase=lambda identifier: 1,
        cursor_position=lambda: (123, 456),
        wait_for_cursor=lambda target, **kwargs: {"status": "reached", "cursor": list(target)},
    )
    app.teaching_surface = SimpleNamespace(show=lambda action: calls.append(("show", action)))
    registry = ToolRegistry()
    register_teaching_tools(registry, lambda: app)
    yield app, registry.tools, calls
    app.close()


def test_laser_circles_image_bounds_without_moving_the_pointer(teaching_tools):
    app, tools, calls = teaching_tools
    result = tools["Laser"](bounds=(0, 0, 20, 10), frame_id="frame")
    assert result["moves_real_cursor"] is False
    mappings = [call for call in calls if call[0] == "resolve_many"]
    assert len(mappings) == 1
    mark = next(call for call in calls if call[0] == "draw")
    assert mark[1] == "laser"
    assert mark[2][0] == mark[2][-1] == (140, 60)
    assert len(mark[2]) == 49
    assert app.backend.events == []


def test_drawing_erasing_and_cursor_observation_emit_no_input(teaching_tools):
    app, tools, calls = teaching_tools
    tools["Draw"](kind="path", points=[(10, 10), (20, 20)])
    assert tools["Erase"]("ink-1") == {"removed": 1}
    assert tools["Cursor"]()["position"] == [123, 456]
    assert app.backend.events == []


def test_transcript_publishes_without_echoing_instruction_text(teaching_tools):
    _, tools, calls = teaching_tools
    result = tools["Transcript"](text="Click Add, then Mesh.", title="Next step")
    assert result == {"sequence": 1, "characters": 21, "display": "shown"}
    assert ("publish", "Click Add, then Mesh.", "Next step") in calls
    assert ("show", "unchanged") in calls


def test_transcript_stacking_does_not_republish_content(teaching_tools):
    _, tools, calls = teaching_tools
    tools["Transcript"](action="back")
    assert calls == [("show", "back")]
    with pytest.raises(ValueError):
        tools["Transcript"](text="invalid combination", action="front")
    assert len(calls) == 1


@pytest.mark.parametrize(
    "name,arguments",
    [
        ("Transcript", {"text": "must not publish"}),
        ("Laser", {"loc": (50, 50)}),
        ("Draw", {"kind": "path", "points": [(1, 1), (2, 2)]}),
        ("Erase", {}),
        ("Cursor", {}),
        ("WaitForCursor", {"loc": (10, 10)}),
    ],
)
def test_presentation_and_cursor_waits_obey_stop(teaching_tools, name, arguments):
    app, tools, calls = teaching_tools
    app.controller.stop()
    with pytest.raises(DesktopStopped):
        tools[name](**arguments)
    assert calls == []


@pytest.mark.parametrize(
    "name,arguments,method",
    [
        ("Laser", {"loc": (10, 10), "frame_id": "frame"}, "draw"),
        ("Draw", {"kind": "path", "points": [(10, 10), (20, 20)], "frame_id": "frame"}, "draw"),
        ("WaitForCursor", {"loc": (10, 10), "frame_id": "frame", "dwell": 0.0}, "wait_for_cursor"),
    ],
)
@pytest.mark.parametrize("kind", ["move", "button", "key"])
def test_input_ticket_survives_the_gap_between_mapping_and_session_authorization(
    teaching_tools, name, arguments, method, kind
):
    app, tools, _ = teaching_tools
    app.controller.set_mode_local("teach")
    app.controller.arm_local()
    app.backend.point = (120, 70)
    app.teaching = TeachingSession(
        app.controller, position=app.backend.position, context=app.teaching_context
    )
    original = getattr(app.teaching, method)

    def after_mapping(*args, **kwargs):
        app.controller.notify_human_input(kind=kind)
        return original(*args, **kwargs)

    setattr(app.teaching, method, after_mapping)
    if kind == "move":
        result = tools[name](**arguments)
        assert result
    else:
        with pytest.raises(RuntimeError, match="Input changed"):
            tools[name](**arguments)
        assert not app.teaching.snapshot().marks
        assert app.teaching.snapshot().waiting is None
    assert app.controller.snapshot().armed


def test_input_during_bulk_mapping_never_publishes_guidance(teaching_tools):
    app, tools, calls = teaching_tools
    app.controller.set_mode_local("teach")
    app.controller.arm_local()
    resolve = app.vision.resolve_many

    def changed(frame, points):
        result = resolve(frame, points)
        app.controller.notify_human_input(kind="button")
        return result

    app.vision.resolve_many = changed
    with pytest.raises(RuntimeError, match="Input changed"):
        tools["Laser"](loc=(10, 10), frame_id="frame")
    assert not any(call[0] == "draw" for call in calls)


def test_bulk_mapping_checks_the_context_once_for_an_entire_path():
    rig = Rig()
    frame = rig.vision.observe(settle=0)
    rig.provider.context_calls.clear()
    points = [(index, 10) for index in range(100)]
    mapped = rig.vision.resolve_many(frame.frame_id, points)
    assert mapped == [(10 + index, 30) for index in range(100)]
    assert rig.provider.context_calls == ["active"]


def test_bulk_mapping_rejects_bad_points_and_revocation():
    rig = Rig()
    frame = rig.vision.observe(settle=0)
    with pytest.raises(ValueError):
        rig.vision.resolve_many(frame.frame_id, [(True, 0)])
    with pytest.raises(ValueError):
        rig.vision.resolve_many(frame.frame_id, [])
    rig.revision += 1
    with pytest.raises(RuntimeError, match="Input changed"):
        rig.vision.resolve_many(frame.frame_id, [(1, 1), (2, 2)])
