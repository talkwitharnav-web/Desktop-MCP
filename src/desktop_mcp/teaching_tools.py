"""Presentation and learner-cursor tools; none inject input into an application."""

from __future__ import annotations

from collections.abc import Callable
import math
from typing import TYPE_CHECKING, Literal

from fastmcp import Context
from mcp.types import ToolAnnotations
from pydantic import StrictFloat, StrictInt

from desktop_mcp.contracts import CaptureContext, Point

if TYPE_CHECKING:
    from desktop_mcp.app import DesktopApplication

StrictPoint = tuple[StrictInt, StrictInt]


def register_teaching_tools(
    mcp,
    get_app: Callable[[], DesktopApplication],
    *,
    on_chat_session: Callable[[str], None] | None = None,
) -> None:
    presentation = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False)
    read = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True)

    def map_points(
        app: DesktopApplication, points: list[Point], frame_id: str | None
    ) -> tuple[list[Point], CaptureContext | None, int]:
        revision = app.controller.input_revision
        if frame_id is not None:
            context = app.vision.context_for(frame_id)
            points = app.vision.resolve_many(frame_id, points)
        else:
            context = app.teaching_context(None)
        app.controller.checkpoint()
        if app.controller.input_revision != revision:
            raise RuntimeError("Input changed while mapping guidance. Obtain a fresh observation.")
        return points, context, revision

    @mcp.tool(
        name="Transcript",
        description="Reply or publish to the on-screen conversation. For a user message from TranscriptRead, include its id as reply_to so it is acknowledged. action=show/hide toggles visibility directly; front/back changes stacking without stealing focus, with local pin preference respected. Chat works while desktop control is paused, but never grants input permission. A publish shows the transcript; it is not automatic mirroring of CLI output.",
        annotations=presentation,
    )
    def transcript(
        text: str | None = None,
        title: str = "Instructions",
        action: Literal["publish", "front", "back", "show", "hide"] = "publish",
        reply_to: StrictInt | None = None,
        ctx: Context | None = None,
    ) -> dict[str, object]:
        app = get_app()
        app.teaching.conversation.ensure_open()
        if action == "publish":
            if text is None:
                raise ValueError("Publishing an instruction requires text.")
            owner = ctx.session_id if ctx is not None else None
            if owner is not None and on_chat_session is not None:
                on_chat_session(owner)
            entry = app.teaching.conversation.reply(
                text, title=title, reply_to=reply_to, owner=owner
            )
            try:
                app.teaching_surface.show("unchanged")
            except RuntimeError as error:
                raise RuntimeError(
                    "The reply was stored, but its window could not be shown. "
                    "Do not duplicate the message; use Transcript(action='show')."
                ) from error
            return {"sequence": entry.sequence, "characters": len(entry.text), "display": "shown"}
        if text is not None or reply_to is not None:
            raise ValueError("Visibility/stacking actions do not accept text or reply_to.")
        if action == "hide":
            app.teaching_surface.set_visible(False)
        else:
            app.teaching_surface.show("front" if action == "show" else action)
        return {"display": action, "visible": app.teaching_surface.visible}

    @mcp.tool(
        name="TranscriptRead",
        description="Listen for the user's next on-screen message (bounded wait, default 25s). Reply in Transcript with reply_to=message.id, then listen again while the conversation is active. Messages remain queued until answered. Only one MCP session listens at a time; release=true hands the conversation back. No screenshots or desktop input, and no requirement to Arm for chat. This does not wake an idle model automatically.",
        annotations=read,
    )
    async def transcript_read(
        ctx: Context,
        timeout: StrictFloat = 25.0,
        listener_name: str = "Copilot",
        release: bool = False,
    ) -> dict[str, object]:
        app = get_app()
        owner = ctx.session_id
        if on_chat_session is not None:
            on_chat_session(owner)
        if release:
            return {"released": app.teaching.conversation.release_listener(owner)}
        result = await app.teaching.conversation.listen(
            owner,
            label=listener_name,
            timeout=timeout,
            interrupt=lambda: "observation_due" if app.interaction.observation_due(owner) else None,
        )
        due = app.interaction.observation_due(owner)
        return {
            **result,
            "transcript_visible": app.teaching_surface.visible,
            "transcript_enabled": app.teaching_surface.enabled,
            "observation_due": due,
            "wait_skipped": (
                "Observe the last desktop action before waiting."
                if result.get("interrupted") == "observation_due"
                else None
            ),
        }

    @mcp.tool(
        name="Laser",
        description="Show a temporary visual laser without moving the real cursor (duration 0.01..10 seconds). Give exactly one loc, path, or bounds. Bounds continuously circles an ellipse around a button/object; a path whose last point equals its first also loops, at a speed independent of duration. An open path sweeps once and rests at its endpoint; loc stays in place. Coordinates are image pixels with frame_id, otherwise physical desktop pixels. The laser never clicks or edits Blender. It clears on stop/context/input changes.",
        annotations=presentation,
    )
    def laser(
        loc: StrictPoint | None = None,
        path: list[StrictPoint] | None = None,
        bounds: tuple[StrictInt, StrictInt, StrictInt, StrictInt] | None = None,
        frame_id: str | None = None,
        duration: StrictFloat = 2.0,
        color: str = "#ffb454",
    ) -> dict[str, object]:
        if sum(value is not None for value in (loc, path, bounds)) != 1:
            raise ValueError("Give exactly one of loc, path or bounds.")
        app = get_app()
        with app.controller.operation("Laser guidance"):
            ellipse_bounds = None
            if bounds is not None:
                corners, context, revision = map_points(
                    app, [(bounds[0], bounds[1]), (bounds[2], bounds[3])], frame_id
                )
                left, right = sorted((corners[0][0], corners[1][0]))
                top, bottom = sorted((corners[0][1], corners[1][1]))
                if left == right or top == bottom:
                    raise ValueError("Laser bounds must have positive width and height.")
                ellipse_bounds = (left, top, right, bottom)
                cx, cy = (left + right) / 2, (top + bottom) / 2
                rx, ry = (right - left) / 2, (bottom - top) / 2
                points = [
                    (
                        round(cx + rx * math.cos(2 * math.pi * index / 48)),
                        round(cy + ry * math.sin(2 * math.pi * index / 48)),
                    )
                    for index in range(48)
                ]
                points.append(points[0])
            else:
                points, context, revision = map_points(
                    app, [loc] if loc is not None else path, frame_id
                )
            identifier = app.teaching.draw(
                "laser",
                points,
                color=color,
                lifetime=duration,
                expected_context=context,
                expected_input_revision=revision,
                laser_bounds=ellipse_bounds,
            )
            return {"identifier": identifier, "duration": duration, "moves_real_cursor": False}

    @mcp.tool(
        name="Draw",
        description="Draw erasable screen ink on a separate visual layer: freehand path, rectangle or ellipse. Two corners define rectangle/ellipse; path uses ordered vertices. Supply frame_id for image coordinates. This never edits the underlying application. Ink is bounded and clears when its context is stale or control stops.",
        annotations=presentation,
    )
    def draw(
        kind: Literal["path", "rectangle", "ellipse"],
        points: list[StrictPoint],
        frame_id: str | None = None,
        color: str = "#ffb454",
        width: StrictFloat = 3.0,
        lifetime: StrictFloat | None = None,
    ) -> dict[str, object]:
        app = get_app()
        with app.controller.operation("Drawing guidance"):
            physical, context, revision = map_points(app, points, frame_id)
            identifier = app.teaching.draw(
                kind,
                physical,
                color=color,
                width=width,
                lifetime=lifetime,
                expected_context=context,
                expected_input_revision=revision,
            )
            return {"identifier": identifier, "edits_underlying_app": False}

    @mcp.tool(
        name="Erase",
        description="Erase one annotation by identifier, or all Desktop-MCP ink/laser marks when omitted. This affects only our visual overlay, never Blender objects, app content or files.",
        annotations=presentation,
    )
    def erase(identifier: str | None = None) -> dict[str, object]:
        app = get_app()
        with app.controller.operation("Erasing guidance"):
            return {"removed": app.teaching.erase(identifier)}

    @mcp.tool(
        name="Cursor",
        description="Read the user's actual pointer position without a screenshot. Returned coordinates are physical virtual-desktop pixels, not the laser's visual position.",
        annotations=read,
    )
    def cursor() -> dict[str, object]:
        app = get_app()
        with app.controller.operation("Reading the learner cursor"):
            return {
                "position": list(app.teaching.cursor_position()),
                "units": "physical virtual-desktop pixels",
                "awaiting_user": app.controller.snapshot().awaiting_user,
            }

    @mcp.tool(
        name="WaitForCursor",
        description="Give the learner a turn: wait until their cursor stays near a target for a continuous dwell. No mode switch needed; control tools can resume after the wait if access is still armed. Returns reached/timeout/context_changed/input_changed, not proof a button was clicked. radius is physical pixels; frame_id maps screenshot coordinates. Ctrl+Shift+H always cancels. Publish the instruction with Transcript first.",
        annotations=read,
    )
    def wait_for_cursor(
        loc: StrictPoint,
        frame_id: str | None = None,
        radius: StrictFloat = 28.0,
        dwell: StrictFloat = 0.25,
        timeout: StrictFloat = 15.0,
    ) -> dict[str, object]:
        app = get_app()
        with app.controller.operation("Waiting for the learner"):
            points, context, revision = map_points(app, [loc], frame_id)
            return app.teaching.wait_for_cursor(
                points[0],
                radius=radius,
                dwell=dwell,
                timeout=timeout,
                expected_context=context,
                expected_input_revision=revision,
            )
