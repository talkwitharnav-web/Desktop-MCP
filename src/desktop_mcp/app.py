"""Composition of the shared controller, local interface and observation service."""

from __future__ import annotations

from contextlib import ExitStack, asynccontextmanager, contextmanager
import json
import os
import subprocess
import sys
import threading
import time
from typing import TYPE_CHECKING, Iterator
from collections.abc import Callable
from importlib.resources import files

from fastmcp import FastMCP

from desktop_mcp.runtime import Controller
from desktop_mcp.contracts import CaptureContext, Observation
from desktop_mcp.image_files import ImageFiles
from desktop_mcp.policy import ControlPolicy
from desktop_mcp.teaching_tools import register_teaching_tools
from desktop_mcp.tools import register_tools

if TYPE_CHECKING:
    from desktop_mcp.native import WindowsInput
    from desktop_mcp.teaching import TeachingSession
    from desktop_mcp.teaching_ui import TeachingSurface
    from desktop_mcp.ui import ControlSurface
    from desktop_mcp.vision import VisionService

AGENT_GUIDE_URI = "desktop-mcp://guide"


def read_agent_guide() -> str:
    """Read the shipped operating guide, independent of the client's working directory."""
    return files("desktop_mcp").joinpath("AGENT_GUIDE.md").read_text(encoding="utf-8")


class DesktopApplication:
    def __init__(self) -> None:
        from desktop_mcp.capture import WindowsCapture
        from desktop_mcp.native import WindowsInput
        from desktop_mcp.teaching import TeachingSession
        from desktop_mcp.teaching_ui import TeachingSurface
        from desktop_mcp.ui import ControlSurface
        from desktop_mcp.vision import VisionService

        self.exit_requested = threading.Event()
        self.backend: WindowsInput = WindowsInput()
        self.controller = Controller(self.backend)
        self.surface: ControlSurface = ControlSurface(
            self.controller,
            control_windows=self.window_handles,
            on_exit=self.request_exit,
            transcript_visible=lambda: self.teaching_surface.enabled,
            on_transcript_toggle=lambda: self.teaching_surface.toggle_local(),
        )
        self.capture = WindowsCapture(
            capture_guard=self.capture_guard,
            control_windows=self.window_handles,
            checkpoint=self.controller.checkpoint,
        )
        self.vision: VisionService = VisionService(
            self.capture,
            revision=lambda: self.controller.input_revision,
            checkpoint=self.controller.checkpoint,
            wait=self.controller.wait,
        )
        self.teaching: TeachingSession = TeachingSession(
            self.controller, position=self.backend.position, context=self.teaching_context
        )
        self.teaching_surface: TeachingSurface = TeachingSurface(
            self.controller, self.teaching, on_exit=self.request_exit
        )
        self.backend.set_control_windows(self.window_handles)
        setting = os.getenv("DESKTOP_MCP_IMAGE_FILES", "false").casefold()
        if setting not in {"true", "1", "yes", "on", "false", "0", "no", "off", ""}:
            raise ValueError("DESKTOP_MCP_IMAGE_FILES must be a boolean setting.")
        self.export_frames = setting in {"true", "1", "yes", "on"}
        self.image_files = ImageFiles()

    def request_exit(self) -> None:
        """Revoke immediately; the host closes transports and both UI threads."""
        try:
            self.controller.stop("Desktop-MCP is quitting.")
        finally:
            try:
                self.teaching.conversation.close()
            finally:
                self.exit_requested.set()

    def start(self) -> None:
        try:
            # Arming becomes available only after both native interfaces exist.
            self.teaching_surface.start()
            self.surface.start()
        except Exception:
            self.controller.set_interface_ready(False, "The local interfaces could not start.")
            raise

    def close(self) -> None:
        with ExitStack() as cleanup:
            cleanup.callback(self.image_files.close)
            cleanup.callback(self.vision.invalidate)
            cleanup.callback(self.surface.close)
            cleanup.callback(self.teaching_surface.close)
            cleanup.callback(self.teaching.conversation.close)
            cleanup.callback(self.controller.close)

    def window_handles(self) -> tuple[int, ...]:
        handles = self.surface.window_handles()
        teaching = getattr(self, "teaching_surface", None)
        return handles + (() if teaching is None else teaching.window_handles())

    @contextmanager
    def capture_guard(self) -> Iterator[None]:
        with ExitStack() as guards:
            guards.enter_context(self.surface.capture_guard())
            guards.enter_context(self.teaching_surface.capture_guard())
            self.controller.checkpoint()
            yield

    def teaching_context(self, expected: CaptureContext | None) -> CaptureContext | None:
        """Read geometry, not pixels; safe outside a controller operation on the UI thread."""
        try:
            context = self.capture.context(scope="active" if expected is None else expected.scope)
        except OSError, RuntimeError:
            return None
        if not context.window_id or context.window_id in self.window_handles():
            return None
        return context

    def export_observation(self, observation: Observation) -> Observation:
        return self.image_files.export(observation)

    def windows(self) -> list[dict[str, object]]:
        import win32gui
        import win32process

        windows = []
        own = self.window_handles()

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

    def accessibility_tree(
        self,
        *,
        use_dom: bool = False,
        expected_context: CaptureContext | None = None,
        expected_input_revision: int | None = None,
    ) -> str:
        from desktop_mcp.capture import context_identity

        self.controller.checkpoint()
        revision = (
            self.controller.input_revision
            if expected_input_revision is None
            else expected_input_revision
        )

        def check_revision() -> None:
            self.controller.checkpoint()
            if self.controller.input_revision != revision:
                raise RuntimeError("Input changed during accessibility inspection. Observe again.")

        context = self.capture.context()
        check_revision()
        if expected_context is not None and context_identity(context) != context_identity(
            expected_context
        ):
            raise RuntimeError("The accessibility target changed before inspection. Observe again.")
        args = [
            sys.executable,
            "-m",
            "desktop_mcp.accessibility",
            "--window",
            str(context.window_id),
        ]
        if use_dom:
            args.append("--dom")
        check_revision()
        process = subprocess.Popen(
            args,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        deadline = time.monotonic() + 5.0
        try:
            while True:
                check_revision()
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError(
                        "Accessibility inspection timed out. Use Screenshot instead."
                    )
                try:
                    stdout, stderr = process.communicate(timeout=min(0.05, remaining))
                    break
                except subprocess.TimeoutExpired:
                    continue
            check_revision()
            if process.returncode:
                raise RuntimeError(
                    f"Accessibility worker exited with {process.returncode}: {stderr[-2000:]}"
                )
            if context_identity(context) != context_identity(self.capture.context()):
                raise RuntimeError("The foreground window changed during accessibility inspection.")
            check_revision()
            result = json.loads(stdout)
            if not isinstance(result, dict) or not isinstance(result.get("tree"), str):
                raise RuntimeError("Accessibility worker returned an invalid response.")
            check_revision()
            return result["tree"]
        finally:
            if process.poll() is None:
                # Only the worker created above is terminated, never another application's PID.
                process.kill()
                process.communicate(timeout=2)


def create_server(
    application: DesktopApplication | None = None,
    *,
    manage_application: bool = True,
    on_chat_session: Callable[[str], None] | None = None,
) -> FastMCP:
    holder: dict[str, DesktopApplication] = {}

    @asynccontextmanager
    async def lifespan(server: FastMCP):
        active = application if application is not None else DesktopApplication()
        holder["application"] = active
        try:
            if manage_application:
                active.start()
            yield
        finally:
            if manage_application:
                active.close()
            holder.clear()

    def get_application() -> DesktopApplication:
        if "application" not in holder:
            raise RuntimeError("Desktop-MCP has not completed startup.")
        return holder["application"]

    server = FastMCP(
        name="Desktop-MCP",
        instructions=read_agent_guide(),
        lifespan=lifespan,
        middleware=[ControlPolicy(lambda: get_application().controller)],
    )
    register_tools(server, get_application)
    register_teaching_tools(server, get_application, on_chat_session=on_chat_session)

    @server.resource(
        AGENT_GUIDE_URI,
        name="Desktop-MCP agent guide",
        mime_type="text/markdown",
        description="Operating instructions for desktop control, teaching and two-way transcript chat.",
    )
    def agent_guide() -> str:
        return read_agent_guide()

    return server
