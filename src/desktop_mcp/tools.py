"""An explicit MCP surface; no tool can arm or bypass the controller."""

from __future__ import annotations

import base64
from collections.abc import Callable
from dataclasses import asdict
import json
from typing import TYPE_CHECKING, Literal
from typing import Annotated

from fastmcp import Context
from fastmcp.tools.tool import ToolResult
from mcp.types import ImageContent, TextContent, ToolAnnotations
from pydantic import Field, StrictInt

from desktop_mcp.actions import Action, Button, parse_shortcut
from desktop_mcp.contracts import CaptureScope, Observation
from desktop_mcp.interaction import current_actor
from desktop_mcp.runtime import BatchInterrupted

if TYPE_CHECKING:
    from desktop_mcp.app import DesktopApplication


def response_context(app: DesktopApplication) -> dict[str, object]:
    actor = current_actor()
    return {
        "host": app.host_info,
        "request": None if actor is None else asdict(actor),
        "observation_due": app.interaction.status()["observation_due"],
        "pending_messages": app.teaching.conversation.status()["pending_messages"],
    }


def observation_result(
    observation: Observation | None,
    *,
    extra: dict[str, object] | None = None,
    detail: Literal["compact", "full"] = "compact",
) -> ToolResult:
    """Keep actual image blocks separate from their structured/text metadata."""
    metadata = dict(extra or {})
    if observation is not None:
        metadata["observation"] = observation.metadata
        metadata["frame_id"] = observation.frame_id
    if detail == "full":
        summary = json.dumps(metadata, ensure_ascii=False, allow_nan=False)
    else:
        lines = []
        if "actions" in metadata:
            lines.append(
                f"Completed: {len(metadata['actions'])} action step(s). "
                "Application outcome is not yet verified."
            )
        if "windows" in metadata:
            lines.extend(
                f"Window {window['window_id']}: {window['title']}" for window in metadata["windows"]
            )
        if "state" in metadata:
            lines.append(f"Desktop {metadata['state']}: {metadata.get('reason', '')}")
        if observation is not None:
            frame = observation.metadata
            lines.append(
                f"Frame {observation.frame_id}; scope={frame.get('scope', 'active')}; "
                f"window={frame.get('window_id')}; image={frame.get('image_dimensions')}."
            )
            lines.append(
                f"Image unchanged; reuse {frame.get('image_frame_id')}. Use the new frame_id for input."
                if observation.image is None
                else "Inspect the image before choosing the next action; use frame_id for its coordinates."
            )
        if metadata.get("observation_due"):
            lines.append(
                "Observation due: check the last action before a long wait or completion claim."
            )
        if metadata.get("pending_messages"):
            lines.append(f"Transcript has {metadata['pending_messages']} unanswered message(s).")
        summary = "\n".join(lines) or "Result details are in the structured response."
    content: list[TextContent | ImageContent] = [TextContent(type="text", text=summary)]
    if observation is not None and observation.image is not None:
        content.append(
            ImageContent(
                type="image",
                data=base64.b64encode(observation.image).decode("ascii"),
                mimeType=observation.mime_type,
            )
        )
    return ToolResult(content=content, structured_content=metadata)


def register_tools(
    mcp,
    get_app: Callable[[], DesktopApplication],
    *,
    on_desktop_session: Callable[[str], None] | None = None,
) -> None:
    read = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True)
    mutate = ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=False)

    def run(
        actions: list[Action],
        *,
        observe: bool,
        since: str | None = None,
        scope: CaptureScope = "active",
        window_id: int | None = None,
        detail: Literal["compact", "full"] = "compact",
    ) -> ToolResult:
        app = get_app()
        with app.controller.operation("Desktop input"):
            frames = {action.frame_id for action in actions if action.frame_id is not None}
            for frame_id in frames:
                context = app.vision.context_for(frame_id)
                if window_id is not None and context.window_id != window_id:
                    raise ValueError("The frame belongs to a different foreground window.")
                window_id = context.window_id
            actor = current_actor()
            try:
                completed = app.controller.execute(
                    actions, resolve=app.vision.resolve, window_id=window_id
                )
            except BatchInterrupted as error:
                app.interaction.record_input(
                    tool=actor.tool if actor else "Desktop input",
                    completed=error.completed,
                    partial=True,
                )
                raise
            if any(action.kind != "wait" for action in actions):
                app.interaction.record_input(
                    tool=actor.tool if actor else "Desktop input", completed=len(completed)
                )
            try:
                reference = since
                if reference is None and len(frames) == 1:
                    candidate = next(iter(frames))
                    # A change wait is meaningful only in the same captured context.
                    if context.scope == scope:
                        reference = candidate
                if reference is None:
                    reference = app.interaction.observation_reference(scope)
                observation = (
                    app.vision.observe(
                        scope=scope,
                        since=reference,
                        wait_for_change=0.35
                        if reference
                        and any(action.kind not in {"move", "wait"} for action in actions)
                        else 0.0,
                    )
                    if observe
                    else None
                )
                if observation is not None and observation.image is not None and app.export_frames:
                    observation = app.export_observation(observation)
                if observation is not None:
                    app.interaction.record_observation(observation)
            except (OSError, RuntimeError, ValueError) as error:
                raise RuntimeError(
                    f"{len(completed)} action(s) completed, but the follow-up observation failed: "
                    f"{error}. Do not replay the input; request a fresh Screenshot if allowed."
                ) from error
            return observation_result(
                observation,
                extra={
                    "actions": completed,
                    "input_revision": app.controller.input_revision,
                    "application_outcome": "unverified",
                    **response_context(app),
                },
                detail=detail,
            )

    @mcp.tool(
        name="DesktopStatus",
        description="Read control status without capturing the screen. Only the local control window can allow/resume access. Ctrl+Shift+H stops Desktop-MCP, not unrelated shell tools or other MCP servers.",
        annotations=read,
    )
    def status() -> dict[str, object]:
        app = get_app()
        actor = current_actor()
        return {
            **asdict(app.controller.snapshot()),
            "host": app.host_info,
            "interaction": app.interaction.status(actor.session_id if actor else None),
            "transcript": {
                "visible": app.teaching_surface.visible,
                "enabled": app.teaching_surface.enabled,
                **app.teaching.conversation.status(),
            },
        }

    @mcp.tool(
        name="DesktopControl",
        description="Claim or release ownership of a multi-step desktop task. One interactive MCP session owns input/observations at a time; other helpers must return research to it, not interleave GUI actions. Claim requires existing local Arm and never grants it. First desktop use also claims automatically. Release stops desktop access and invalidates queued input. Status is read-only. Use a brief task label, not private document contents.",
        annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True),
    )
    def desktop_control(
        ctx: Context,
        action: Literal["claim", "release", "status"] = "status",
        task: str | None = None,
    ) -> dict[str, object]:
        app = get_app()
        owner = ctx.session_id
        if action == "claim":
            with app.controller.request() as generation:
                result = app.interaction.claim(owner, generation=generation, task=task)
            if on_desktop_session is not None:
                on_desktop_session(owner)
            return {"claimed": True, "owner": result}
        if task is not None:
            raise ValueError("task is only valid when claiming a desktop task.")
        if action == "release":
            return {"released": app.interaction.release(owner)}
        return app.interaction.status(owner)

    @mcp.tool(
        name="DesktopStop",
        description="Immediately revoke Desktop-MCP input and captures and release its held keys/buttons. Only the human can resume locally. Never try to work around a stop through another tool.",
        annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True),
    )
    async def stop() -> dict[str, object]:
        app = get_app()
        app.controller.stop("Stopped by DesktopStop.")
        return asdict(app.controller.snapshot())

    @mcp.tool(
        name="Screenshot",
        description="Observe the active window or desktop with real image pixels and a frame_id for safe coordinate mapping. wait_for_change is 0..5 seconds, settle is 0..1; this is bounded polling, not video or proof of application readiness. Use a relevant region and verify the actual UI postcondition. since can reuse an unchanged image; omit it if you need new image bytes. detail='full' repeats all structured diagnostics as text for clients that need that compatibility path. export_image returns a temporary path only when requested.",
        annotations=read,
    )
    def screenshot(
        scope: CaptureScope = "active",
        region: tuple[StrictInt, StrictInt, StrictInt, StrictInt] | None = None,
        max_dimension: Annotated[int, Field(strict=True, ge=1, le=4096)] = 1440,
        encoding: Literal["auto", "png", "jpeg"] = "auto",
        quality: Annotated[int, Field(strict=True, ge=1, le=100)] = 85,
        since: str | None = None,
        wait_for_change: Annotated[
            float, Field(strict=True, ge=0, le=5, allow_inf_nan=False)
        ] = 0.0,
        settle: Annotated[float, Field(strict=True, ge=0, le=1, allow_inf_nan=False)] = 0.06,
        export_image: bool = False,
        detail: Literal["compact", "full"] = "compact",
    ) -> ToolResult:
        app = get_app()
        with app.controller.operation("Observing"):
            if export_image and since is not None:
                raise ValueError("For a full image file, omit since when using export_image.")
            observation = app.vision.observe(
                scope=scope,
                region=region,
                max_dimension=max_dimension,
                encoding=encoding,
                quality=quality,
                since=since,
                wait_for_change=wait_for_change,
                settle=settle,
            )
            if observation.image is not None and (export_image or app.export_frames):
                observation = app.export_observation(observation)
            app.interaction.record_observation(observation)
            return observation_result(
                observation,
                detail=detail,
                extra=response_context(app),
            )

    @mcp.tool(
        name="DesktopBatch",
        description="Run 1-64 validated actions serially, then return one fresh observation by default. Prefer short known sequences over one tool round trip per key. Supports move/click/drag/scroll/key/text/wait and key_down/key_up/button_down/button_up within this batch only. All held input is released at batch end or cancellation. Mouse motion uses acceleration/deceleration. Never replay completed steps after partial failure. frame_id identifies image-space coordinates; without it loc is a physical virtual-desktop coordinate.",
        annotations=mutate,
    )
    def batch(
        actions: list[Action],
        observe: bool = True,
        since: str | None = None,
        scope: CaptureScope = "active",
        window_id: StrictInt | None = None,
        detail: Literal["compact", "full"] = "compact",
    ) -> ToolResult:
        return run(
            actions, observe=observe, since=since, scope=scope, window_id=window_id, detail=detail
        )

    @mcp.tool(
        name="Click",
        description="Glide to loc, then left/right/middle/x1/x2 click (1-3 clicks), optionally holding named modifier keys. Supply frame_id for image coordinates; otherwise loc is physical desktop pixels. Omitting loc clicks the current pointer position. A fresh image is returned by default.",
        annotations=mutate,
    )
    def click(
        loc: tuple[StrictInt, StrictInt] | None = None,
        button: Button = "left",
        clicks: StrictInt = 1,
        modifiers: list[str] | None = None,
        frame_id: str | None = None,
        duration: float | None = None,
        observe: bool = True,
    ) -> ToolResult:
        return run(
            [
                Action(
                    kind="click",
                    loc=loc,
                    button=button,
                    clicks=clicks,
                    keys=modifiers or [],
                    frame_id=frame_id,
                    duration=duration,
                )
            ],
            observe=observe,
        )

    @mcp.tool(
        name="Move",
        description="Move with smooth acceleration/deceleration, or drag with any mouse button and modifiers (e.g. middle drag / Shift+middle drag in Blender). from_loc gives an explicit drag start. duration is optional seconds with an 80 ms motion minimum; default is distance-adaptive, not teleporting. frame_id maps image coordinates for both endpoints.",
        annotations=mutate,
    )
    def move(
        loc: tuple[StrictInt, StrictInt],
        drag: bool = False,
        from_loc: tuple[StrictInt, StrictInt] | None = None,
        button: Button = "left",
        modifiers: list[str] | None = None,
        duration: float | None = None,
        frame_id: str | None = None,
        observe: bool = True,
    ) -> ToolResult:
        return run(
            [
                Action(
                    kind="drag" if drag else "move",
                    loc=loc,
                    start=from_loc,
                    button=button,
                    keys=modifiers or [],
                    duration=duration,
                    frame_id=frame_id,
                )
            ],
            observe=observe,
        )

    @mcp.tool(
        name="Scroll",
        description="Native vertical and horizontal wheel input. A wheel notch is 120 units: positive delta_y scrolls up; positive delta_x scrolls right. Optional modifiers support Ctrl/Shift wheel gestures. loc/frame_id optionally position the pointer first.",
        annotations=mutate,
    )
    def scroll(
        delta_y: StrictInt = -120,
        delta_x: StrictInt = 0,
        loc: tuple[StrictInt, StrictInt] | None = None,
        modifiers: list[str] | None = None,
        frame_id: str | None = None,
        observe: bool = True,
    ) -> ToolResult:
        return run(
            [
                Action(
                    kind="scroll",
                    loc=loc,
                    delta_x=delta_x,
                    delta_y=delta_y,
                    keys=modifiers or [],
                    frame_id=frame_id,
                )
            ],
            observe=observe,
        )

    @mcp.tool(
        name="Keyboard",
        description="Press and release a named key or chord, optionally repeated. Supports Win, left/right modifiers, navigation, F1-F24, numpad and media keys. For literal characters/Unicode use Type. For held keys across several actions use DesktopBatch, not separate calls.",
        annotations=mutate,
    )
    def keyboard(keys: list[str], repeat: StrictInt = 1, observe: bool = True) -> ToolResult:
        return run([Action(kind="key", keys=keys, repeat=repeat)], observe=observe)

    @mcp.tool(
        name="Shortcut",
        description="Press a plus-separated chord, such as win, alt+tab, ctrl+shift+s or ctrl+c. Ctrl+Shift+H triggers the emergency stop; no keyboard chord resumes Desktop-MCP. Use Type for text.",
        annotations=mutate,
    )
    def shortcut(shortcut: str, observe: bool = True) -> ToolResult:
        return run([Action(kind="key", keys=parse_shortcut(shortcut))], observe=observe)

    @mcp.tool(
        name="Type",
        description="Enter literal Unicode text without an artificial typing-speed cap. By default type into the currently focused control; optional loc/frame_id glides and focuses first. clear selects existing text, press_enter submits. Newlines and tabs are keyboard events. Long text remains stoppable between native input chunks.",
        annotations=mutate,
    )
    def type_text(
        text: str,
        loc: tuple[StrictInt, StrictInt] | None = None,
        clear: bool = False,
        press_enter: bool = False,
        frame_id: str | None = None,
        observe: bool = True,
    ) -> ToolResult:
        actions = []
        if loc is not None:
            actions.append(Action(kind="click", loc=loc, frame_id=frame_id))
        elif frame_id is not None:
            raise ValueError("frame_id requires loc when typing.")
        actions.append(Action(kind="text", text=text, clear=clear, submit=press_enter))
        return run(actions, observe=observe)

    @mcp.tool(
        name="Wait",
        description="An interruptible wait, in seconds, optionally followed by a screenshot. Prefer Screenshot(wait_for_change=...) over blind delays when waiting for rendering.",
        annotations=read,
    )
    def wait(duration: float, observe: bool = True) -> ToolResult:
        return run([Action(kind="wait", duration=duration)], observe=observe)

    @mcp.tool(
        name="App",
        description="List visible windows, focus an existing window_id, or launch an explicit absolute executable path with separate arguments (no shell). For a visible Start-menu launch, use Keyboard/Type instead. This tool is subject to the same local stop gate.",
        annotations=mutate,
    )
    def app_tool(
        mode: Literal["list", "focus", "launch"] = "list",
        window_id: StrictInt | None = None,
        executable: str | None = None,
        args: list[str] | None = None,
        observe: bool = True,
    ) -> ToolResult:
        app = get_app()
        with app.controller.operation("Application"):
            if mode == "list":
                if window_id is not None or executable is not None or args is not None:
                    raise ValueError("Window listing does not accept focus or launch arguments.")
                return observation_result(
                    None, extra={"windows": app.windows(), **response_context(app)}
                )
            if mode == "focus":
                if window_id is None or executable is not None or args is not None:
                    raise ValueError("Focus requires only window_id.")
                app.controller.emit(lambda: app.backend.focus(window_id))
                result = {"window_id": window_id}
            else:
                if executable is None or window_id is not None:
                    raise ValueError("Launch requires executable and optional args, not window_id.")
                pid = app.controller.emit(lambda: app.backend.launch(executable, args or []))
                result = {"pid": pid, "launched": True}
            app.interaction.record_input(tool=f"App.{mode}", completed=1)
            try:
                observation = (
                    app.observe_focused(window_id)
                    if mode == "focus" and observe
                    else app.vision.observe()
                    if observe
                    else None
                )
                if observation is not None and observation.image is not None and app.export_frames:
                    observation = app.export_observation(observation)
                if observation is not None:
                    app.interaction.record_observation(observation)
            except (OSError, RuntimeError, ValueError) as error:
                raise RuntimeError(
                    f"The application {mode} completed, but observation failed: {error}. "
                    "Do not launch the application again just to obtain an image."
                ) from error
            return observation_result(
                observation,
                extra={**result, "application_outcome": "unverified", **response_context(app)},
            )

    @mcp.tool(
        name="DisplayInventory",
        description="Read physical monitor bounds, DPI and scale information without taking a screenshot. Virtual-desktop coordinates may have negative origins.",
        annotations=read,
    )
    def displays() -> dict[str, object]:
        app = get_app()
        with app.controller.operation("Display inventory"):
            return {"displays": app.displays()}

    @mcp.tool(
        name="Snapshot",
        description="Optional heavier Windows accessibility-tree inspection, with a fresh image. Prefer Screenshot for fast visual work and custom-rendered apps such as Blender. UIA text/labels are not a substitute for image pixels.",
        annotations=read,
    )
    def snapshot(use_dom: bool = False) -> ToolResult:
        from desktop_mcp.capture import context_identity

        app = get_app()
        with app.controller.operation("Accessibility inspection"):
            revision = app.controller.input_revision
            context = app.capture.context()

            def check_ticket(current=None) -> None:
                app.controller.checkpoint()
                if app.controller.input_revision != revision:
                    raise RuntimeError("Input changed during Snapshot. Obtain a fresh observation.")
                current = app.capture.context() if current is None else current
                if context_identity(current) != context_identity(context):
                    raise RuntimeError("The Snapshot window changed. Obtain a fresh observation.")
                app.controller.checkpoint()
                if app.controller.input_revision != revision:
                    raise RuntimeError("Input changed during Snapshot. Obtain a fresh observation.")

            tree = app.accessibility_tree(
                use_dom=use_dom,
                expected_context=context,
                expected_input_revision=revision,
            )
            check_ticket()
            observation = app.vision.observe()
            if observation.image is not None and app.export_frames:
                observation = app.export_observation(observation)
            check_ticket(app.vision.context_for(observation.frame_id))
            app.interaction.record_observation(observation)
            return observation_result(
                observation, extra={"accessibility_tree": tree, **response_context(app)}
            )
