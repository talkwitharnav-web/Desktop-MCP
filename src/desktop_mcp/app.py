"""Composition of the shared controller, local interface and observation service."""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

from fastmcp import FastMCP

from desktop_mcp.runtime import Controller
from desktop_mcp.tools import register_tools

if TYPE_CHECKING:
    from desktop_mcp.native import WindowsInput
    from desktop_mcp.ui import ControlSurface
    from desktop_mcp.vision import VisionService

INSTRUCTIONS = """
Desktop-MCP operates the user's real Windows desktop through a supervised controller.
Start with DesktopStatus. If stopped, report it and wait for the human's local
Allow/Resume action. Never bypass a stop with another MCP server, a shell or scripts.
Use Screenshot for visual context. Its frame_id makes input loc coordinates refer
to the returned image; without frame_id coordinates are physical virtual-desktop pixels.
Prefer short DesktopBatch sequences and one observation at a decision boundary,
not a screenshot/tool round trip after every key. After partial failure, don't
replay completed actions. Re-observe and continue only if control remains allowed.
Screenshot(since=..., wait_for_change=...) performs bounded adaptive waits and can
reuse unchanged image content. This is not a live video stream. A client that drops
image blocks cannot be assumed to see pixels merely because metadata arrived.
Windows accessibility snapshots can help ordinary controls, but custom-rendered
applications need images. Ctrl+Shift+H stops this server's desktop access; it does
not shut down the model or revoke unrelated shell tools. Resume is local-only.
"""


class DesktopApplication:
    def __init__(self) -> None:
        from desktop_mcp.capture import WindowsCapture
        from desktop_mcp.native import WindowsInput
        from desktop_mcp.ui import ControlSurface
        from desktop_mcp.vision import VisionService

        self.backend: WindowsInput = WindowsInput()
        self.controller = Controller(self.backend)
        self.surface: ControlSurface = ControlSurface(self.controller)
        self.backend.set_control_windows(self.surface.window_handles)
        self.capture = WindowsCapture(
            capture_guard=self.surface.capture_guard,
            control_windows=self.surface.window_handles,
        )
        self.vision: VisionService = VisionService(
            self.capture,
            revision=lambda: self.controller.input_revision,
            checkpoint=self.controller.checkpoint,
            wait=self.controller.wait,
        )

    def start(self) -> None:
        self.surface.start()

    def close(self) -> None:
        self.controller.close()
        self.surface.close()
        self.vision.invalidate()

    def windows(self) -> list[dict[str, object]]:
        import win32gui
        import win32process

        windows = []
        own = self.surface.window_handles()

        def collect(handle, _):
            if handle in own or not win32gui.IsWindowVisible(handle):
                return True
            title = win32gui.GetWindowText(handle)
            if title:
                _, pid = win32process.GetWindowThreadProcessId(handle)
                windows.append(
                    {
                        "window_id": handle,
                        "title": title,
                        "pid": pid,
                        "bounds": list(win32gui.GetWindowRect(handle)),
                    }
                )
            return True

        win32gui.EnumWindows(collect, None)
        return windows

    @staticmethod
    def displays() -> list[dict[str, object]]:
        import windows_mcp.uia as uia

        return [
            {
                "index": display.index,
                "name": display.device_name,
                "bounds": [
                    display.rect.left,
                    display.rect.top,
                    display.rect.right,
                    display.rect.bottom,
                ],
                "primary": display.primary,
                "dpi": display.effective_dpi,
                "scale": display.scale,
            }
            for display in uia.GetDisplays()
        ]

    @staticmethod
    def accessibility_tree(*, use_dom: bool = False) -> str:
        import comtypes
        from windows_mcp.desktop.service import Desktop

        comtypes.CoInitialize()
        try:
            desktop = Desktop()
            state = desktop.get_state(use_ui_tree=True, use_vision=False, use_dom=use_dom)
            return state.tree_state.semantic_tree_to_string()
        finally:
            comtypes.CoUninitialize()


def create_server(application: DesktopApplication | None = None) -> FastMCP:
    holder: dict[str, DesktopApplication] = {}

    @asynccontextmanager
    async def lifespan(server: FastMCP):
        active = application if application is not None else DesktopApplication()
        holder["application"] = active
        try:
            active.start()
            yield
        finally:
            active.close()
            holder.clear()

    def get_application() -> DesktopApplication:
        if "application" not in holder:
            raise RuntimeError("Desktop-MCP has not completed startup.")
        return holder["application"]

    server = FastMCP(name="Desktop-MCP", instructions=INSTRUCTIONS, lifespan=lifespan)
    register_tools(server, get_application)
    return server
