"""Content-free, request-local evidence for protected-target errors."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy
from dataclasses import asdict, dataclass, field
from typing import TYPE_CHECKING, Literal

from fastmcp.tools.tool import ToolResult
from mcp.types import TextContent

from desktop_mcp.native import TargetDenied

if TYPE_CHECKING:
    from desktop_mcp.app import DesktopApplication
    from desktop_mcp.interaction import RequestActor


@dataclass
class CallDiagnostics:
    frame_ids: set[str] = field(default_factory=set)
    input: dict[str, object] | None = None


_call: ContextVar[CallDiagnostics | None] = ContextVar("desktop_mcp_diagnostics", default=None)
_DENIAL_FIELDS = (
    "code",
    "operation",
    "target_point",
    "expected_window",
    "actual_foreground",
    "routing",
    "timestamp",
    "reason",
)
_WINDOW_FIELDS = (
    "window_id",
    "root_id",
    "owner_root_id",
    "role",
    "bounds",
    "visible",
    "minimized",
    "effective_visible",
    "click_through",
    "capture_excluded",
    "status",
    "reason",
)


@contextmanager
def call_diagnostics() -> Iterator[CallDiagnostics]:
    evidence = CallDiagnostics()
    token = _call.set(evidence)
    try:
        yield evidence
    finally:
        _call.reset(token)


def validated_frame(frame_id: str) -> None:
    """Record only an identifier already accepted by the observation service."""
    evidence = _call.get()
    if evidence is not None:
        evidence.frame_ids.add(frame_id)


def input_delivery(
    completed: int = 0, *, delivery: Literal["not_started", "complete", "partial"] = "not_started"
) -> None:
    evidence = _call.get()
    if evidence is not None:
        evidence.input = {
            "delivery": delivery,
            "completed_actions": completed,
            "current_action_may_be_partial": delivery == "partial",
            "application_outcome": "unverified",
        }


def _target_denial(error: Exception) -> TargetDenied | None:
    seen: set[int] = set()
    current: BaseException | None = error
    while current is not None and id(current) not in seen and len(seen) < 16:
        if isinstance(current, TargetDenied):
            return current
        seen.add(id(current))
        current = current.__cause__ or (
            None if current.__suppress_context__ else current.__context__
        )
    return None


def protected_target_error(
    error: Exception,
    *,
    app: DesktopApplication,
    actor: RequestActor,
    evidence: CallDiagnostics,
) -> ToolResult | None:
    denial = _target_denial(error)
    if denial is None:
        return None
    details = {key: deepcopy(denial.details[key]) for key in _DENIAL_FIELDS if key in denial.details}
    matched = denial.details.get("matched")
    details["matched"] = (
        {key: deepcopy(matched[key]) for key in _WINDOW_FIELDS if key in matched}
        if isinstance(matched, dict)
        else None
    )
    details["request"] = asdict(actor)
    details["frame_ids"] = sorted(evidence.frame_ids)
    summary = str(denial)
    if evidence.input is not None:
        details["input"] = dict(evidence.input)
        delivery = evidence.input["delivery"]
        completed = evidence.input["completed_actions"]
        if delivery == "partial":
            summary += (
                f" Batch stopped after {completed} completed action(s). "
                "The current action may be partially applied. "
                "Do not blindly replay input; obtain a fresh observation."
            )
        elif delivery == "complete":
            summary += (
                f" {completed} action(s) completed, but the follow-up observation failed. "
                "Do not replay the input; request a fresh Screenshot if allowed."
            )
        else:
            summary += " No input from this request was delivered."
    app.interaction.record_denial(details)
    return ToolResult(
        content=[TextContent(type="text", text=summary)],
        structured_content={
            "is_error": True,
            "denial": details,
            "host": app.host_info,
            "observation_due": app.interaction.status()["observation_due"],
            "pending_messages": app.teaching.conversation.status()["pending_messages"],
        },
        is_error=True,
    )
