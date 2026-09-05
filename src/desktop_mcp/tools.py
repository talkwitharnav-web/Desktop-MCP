"""An explicit MCP surface; no tool can arm or bypass the controller."""

from __future__ import annotations

import base64
from collections.abc import Callable
from dataclasses import asdict
import json
from typing import TYPE_CHECKING, Literal

from fastmcp.tools.tool import ToolResult
from mcp.types import ImageContent, TextContent, ToolAnnotations
from pydantic import StrictInt

from desktop_mcp.actions import Action, Button, parse_shortcut
from desktop_mcp.contracts import CaptureScope, Observation

if TYPE_CHECKING:
    from desktop_mcp.app import DesktopApplication


def observation_result(
    observation: Observation | None,
    *,
    extra: dict[str, object] | None = None,
) -> ToolResult:
    """Keep actual image blocks separate from their structured/text metadata."""
    metadata = dict(extra or {})
    if observation is not None:
        metadata["observation"] = observation.metadata
        metadata["frame_id"] = observation.frame_id
    content: list[TextContent | ImageContent] = [
        TextContent(type="text", text=json.dumps(metadata, ensure_ascii=False, allow_nan=False))
    ]
    if observation is not None and observation.image is not None:
        content.append(
            ImageContent(
                type="image",
                data=base64.b64encode(observation.image).decode("ascii"),
                mimeType=observation.mime_type,
            )
        )
    return ToolResult(content=content, structured_content=metadata)


def register_tools(mcp, get_app: Callable[[], DesktopApplication]) -> None:
    read = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True)
    mutate = ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=False)

    def run(
        actions: list[Action],
        *,
        observe: bool,
        since: str | None = None,
        scope: CaptureScope = "active",
        window_id: int | None = None,
    ) -> ToolResult:
        app = get_app()
        with app.controller.operation("Desktop input"):
            frames = {action.frame_id for action in actions if action.frame_id is not None}
            for frame_id in frames:
                context = app.vision.context_for(frame_id)
                if window_id is not None and context.window_id != window_id:
                    raise ValueError("The frame belongs to a different foreground window.")
                window_id = context.window_id
            completed = app.controller.execute(
                actions, resolve=app.vision.resolve, window_id=window_id
            )
            try:
                observation = app.vision.observe(scope=scope, since=since) if observe else None
                if observation is not None and observation.image is not None and app.export_frames:
                    observation = app.export_observation(observation)
            except (OSError, RuntimeError, ValueError) as error:
                raise RuntimeError(
                    f"{len(completed)} action(s) completed, but the follow-up observation failed: "
                    f"{error}. Do not replay the input; request a fresh Screenshot if allowed."
                ) from error
            return observation_result(
                observation,
                extra={"actions": completed, "input_revision": app.controller.input_revision},
            )

    @mcp.tool(
        name="DesktopStatus",
        description="Read control status without capturing the screen. Only the local control window can allow/resume access. Ctrl+Shift+H stops Desktop-MCP, not unrelated shell tools or other MCP servers.",
        annotations=read,
    )
    def status() -> dict[str, object]:
        app = get_app()
        return {
            **asdict(app.controller.snapshot()),
            "transcript": {
                "visible": app.teaching_surface.visible,
                "enabled": app.teaching_surface.enabled,
                **app.teaching.conversation.status(),
            },
        }

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
        description="Observe the active window (default) or desktop, with an actual image block and a frame_id. For loc coordinates taken from this image, pass its frame_id to input tools: the server applies crop origins/scales. since reuses identical image content with fresh metadata. wait_for_change performs bounded adaptive polling, not video streaming; settle waits briefly for visual stability. If your client drops image blocks, export_image=true also returns a temporary image_path for a native image-reading tool. Stop blocks capture too.",
        annotations=read,
    )
    def screenshot(
        scope: CaptureScope = "active",
        region: tuple[StrictInt, StrictInt, StrictInt, StrictInt] | None = None,
        max_dimension: StrictInt = 1440,
        encoding: Literal["auto", "png", "jpeg"] = "auto",
        quality: StrictInt = 85,
        since: str | None = None,
        wait_for_change: float = 0.0,
        settle: float = 0.06,
        export_image: bool = False,
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
            return observation_result(observation)

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
    ) -> ToolResult:
        return run(actions, observe=observe, since=since, scope=scope, window_id=window_id)

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
                return observation_result(None, extra={"windows": app.windows()})
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
            try:
                observation = app.vision.observe() if observe else None
                if observation is not None and observation.image is not None and app.export_frames:
                    observation = app.export_observation(observation)
            except (OSError, RuntimeError, ValueError) as error:
                raise RuntimeError(
                    f"The application {mode} completed, but observation failed: {error}. "
                    "Do not launch the application again just to obtain an image."
                ) from error
            return observation_result(observation, extra=result)

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
            return observation_result(observation, extra={"accessibility_tree": tree})
