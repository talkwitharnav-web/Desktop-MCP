"""Capture controller revocation at asynchronous MCP dispatch, before offloading."""

from __future__ import annotations

from collections.abc import Callable

from fastmcp.exceptions import ToolError
from fastmcp.server.middleware import Middleware, MiddlewareContext
from fastmcp.server.middleware.middleware import CallNext
from fastmcp.tools.tool import ToolResult
from mcp.types import CallToolRequestParams

from desktop_mcp.runtime import Controller, DesktopStopped


class ControlPolicy(Middleware):
    def __init__(self, get_controller: Callable[[], Controller]) -> None:
        self._get_controller = get_controller

    async def on_call_tool(
        self,
        context: MiddlewareContext[CallToolRequestParams],
        call_next: CallNext[CallToolRequestParams, ToolResult],
    ) -> ToolResult:
        if context.message.name in {"DesktopStatus", "DesktopStop"}:
            return await call_next(context)
        try:
            with self._get_controller().request():
                return await call_next(context)
        except DesktopStopped as error:
            raise ToolError(str(error)) from error
