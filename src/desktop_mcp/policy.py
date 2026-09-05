"""Capture controller revocation at asynchronous MCP dispatch, before offloading."""

from __future__ import annotations

from collections.abc import Callable

from fastmcp.exceptions import ToolError
from fastmcp.server.middleware import Middleware, MiddlewareContext
from fastmcp.server.middleware.middleware import CallNext
from fastmcp.tools.tool import ToolResult
from mcp.types import CallToolRequestParams

from desktop_mcp.runtime import DesktopStopped
from desktop_mcp.interaction import DesktopInUse, RequestActor, request_actor

DESKTOP_TOOLS = frozenset(
    {
        "Screenshot",
        "Snapshot",
        "DesktopBatch",
        "Click",
        "Move",
        "Scroll",
        "Keyboard",
        "Shortcut",
        "Type",
        "Wait",
        "App",
        "DisplayInventory",
        "Laser",
        "Draw",
        "Erase",
        "Cursor",
        "WaitForCursor",
    }
)
INPUT_TOOLS = frozenset(
    {"DesktopBatch", "Click", "Move", "Scroll", "Keyboard", "Shortcut", "Type", "App"}
)


class ControlPolicy(Middleware):
    def __init__(
        self, get_application: Callable, on_desktop_session: Callable[[str], None] | None = None
    ) -> None:
        self._get_application = get_application
        self._on_desktop_session = on_desktop_session

    async def on_call_tool(
        self,
        context: MiddlewareContext[CallToolRequestParams],
        call_next: CallNext[CallToolRequestParams, ToolResult],
    ) -> ToolResult:
        mcp_context = context.fastmcp_context
        if mcp_context is None:
            raise ToolError("An initialized MCP client context is required.")
        actor = RequestActor(mcp_context.session_id, mcp_context.request_id, context.message.name)
        try:
            with request_actor(actor):
                if context.message.name not in DESKTOP_TOOLS:
                    return await call_next(context)
                app = self._get_application()
                with app.controller.request() as generation:
                    app.interaction.claim(actor.session_id, generation=generation)
                    if self._on_desktop_session is not None:
                        self._on_desktop_session(actor.session_id)
                    arguments = context.message.arguments or {}
                    changes_desktop = actor.tool in INPUT_TOOLS and not (
                        actor.tool == "App" and arguments.get("mode", "list") == "list"
                    )
                    pending = app.teaching.conversation.status()["pending_messages"]
                    if changes_desktop and pending:
                        raise ToolError(
                            f"{pending} unanswered transcript message(s) are waiting. "
                            "Use TranscriptRead and reply with reply_to before changing the desktop. "
                            "Desktop access is still armed."
                        )
                    return await call_next(context)
        except DesktopInUse as error:
            raise ToolError(str(error)) from error
        except DesktopStopped as error:
            raise ToolError(str(error)) from error
