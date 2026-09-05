"""Shared contracts for control, observation and the local Windows interface."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, Protocol

if TYPE_CHECKING:
    from PIL.Image import Image

Point = tuple[int, int]
Rect = tuple[int, int, int, int]
CaptureScope = Literal["active", "desktop"]
INJECTED_INPUT_TAG = 0x444D4350


@dataclass(frozen=True)
class ControlSnapshot:
    state: Literal["stopped", "ready", "running", "error", "closed"]
    reason: str
    action: str | None = None
    cursor: Point | None = None
    generation: int = 0
    input_revision: int = 0
    completed_actions: int = 0
    interface_ready: bool = False
    human_takeover: bool = True
    last_error: str | None = None
    mode: Literal["control", "teach"] = "control"
    user_cursor: Point | None = None

    @property
    def armed(self) -> bool:
        return self.state in {"ready", "running"}


class LocalControl(Protocol):
    def snapshot(self) -> ControlSnapshot: ...
    def arm_local(self) -> None: ...
    def stop(self, reason: str = "Stopped locally") -> None: ...
    def set_interface_ready(self, ready: bool, error: str | None = None) -> None: ...
    def set_human_takeover(self, enabled: bool) -> None: ...
    def set_mode_local(self, mode: Literal["control", "teach"]) -> None: ...
    def notify_human_input(
        self, *, kind: Literal["move", "button", "key"] = "move", position: Point | None = None
    ) -> None: ...


@dataclass(frozen=True)
class CaptureContext:
    window_id: int
    bounds: Rect
    desktop_bounds: Rect
    title: str = ""
    display_bounds: tuple[Rect, ...] = ()


@dataclass(frozen=True)
class RawCapture:
    image: Image
    bounds: Rect
    context: CaptureContext
    captured_at: float


class CaptureProvider(Protocol):
    def context(self, scope: CaptureScope = "active") -> CaptureContext: ...
    def capture(
        self, *, scope: CaptureScope = "active", region: Rect | None = None
    ) -> RawCapture: ...


@dataclass(frozen=True)
class Observation:
    frame_id: str
    metadata: dict[str, object]
    image: bytes | None
    mime_type: str
