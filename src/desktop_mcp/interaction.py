"""Task-level desktop ownership and post-input observation bookkeeping."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
from importlib.resources import files
import os
import threading
import time
from typing import TYPE_CHECKING
from uuid import uuid4

from desktop_mcp import __version__
from desktop_mcp.runtime import DesktopStopped

if TYPE_CHECKING:
    from desktop_mcp.contracts import Observation
    from desktop_mcp.runtime import Controller


@dataclass(frozen=True)
class RequestActor:
    session_id: str
    request_id: str
    tool: str
    generation: int | None = None


_actor: ContextVar[RequestActor | None] = ContextVar("desktop_mcp_actor", default=None)


@contextmanager
def request_actor(actor: RequestActor) -> Iterator[None]:
    token = _actor.set(actor)
    try:
        yield
    finally:
        _actor.reset(token)


def current_actor() -> RequestActor | None:
    return _actor.get()


def host_identity() -> dict[str, object]:
    """Describe this instance, not an unverified Git checkout revision."""
    digest = sha256()
    for entry in sorted(files("desktop_mcp").iterdir(), key=lambda item: item.name):
        if entry.is_file() and (entry.name.endswith(".py") or entry.name == "AGENT_GUIDE.md"):
            digest.update(entry.name.encode("utf-8"))
            digest.update(entry.read_bytes())
    return {
        "version": __version__,
        "pid": os.getpid(),
        "instance_id": uuid4().hex,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "package_fingerprint": digest.hexdigest()[:24],
        "fingerprint_scope": "packaged Python and operating guide at host startup, not a Git revision",
        "workspace": {
            "kind": "shared-windows-desktop",
            "independent_input_cursor": False,
            "independent_visual_guidance": True,
        },
    }


class DesktopInUse(RuntimeError):
    def __init__(self, owner: dict[str, object]) -> None:
        self.details = {"code": "desktop_in_use", "owner": owner}
        super().__init__(
            f"Another MCP session owns this desktop task ({owner['task']}, "
            f"session {owner['session_id']}). Return findings to that coordinator; "
            "do not start a second interactive workflow."
        )


class Interaction:
    def __init__(
        self, controller: Controller, *, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self.controller = controller
        self._clock = clock
        self._lock = threading.RLock()
        self._owner: dict[str, object] | None = None
        self._generation = -1
        self._pending: dict[str, object] | None = None
        self._last_action: dict[str, object] | None = None
        self._last_observation: dict[str, object] | None = None
        self._last_denial: dict[str, object] | None = None

    def _sync(self):
        state = self.controller.snapshot()
        if state.generation != self._generation or not state.armed:
            self._owner = None
            self._pending = None
            self._generation = state.generation
        return state

    def claim(
        self, session_id: str, *, generation: int, task: str | None = None
    ) -> dict[str, object]:
        if not isinstance(session_id, str) or not session_id:
            raise ValueError("An MCP session identity is required.")
        if task is not None and (
            not isinstance(task, str)
            or not task.strip()
            or len(task) > 120
            or any(ord(char) < 32 for char in task)
        ):
            raise ValueError("task must be 1..120 printable characters.")
        with self._lock:
            state = self._sync()
            if not state.armed or not state.interface_ready or state.generation != generation:
                raise DesktopStopped("This desktop request was stopped or superseded.")
            if self._owner is not None and self._owner["session_id"] != session_id:
                raise DesktopInUse(dict(self._owner))
            if self._owner is None:
                self._owner = {
                    "session_id": session_id,
                    "task": task or "Desktop task",
                    "generation": generation,
                }
            elif task is not None:
                self._owner = {**self._owner, "task": task}
            return dict(self._owner)

    def release(self, session_id: str, *, disconnected: bool = False) -> bool:
        with self._lock:
            self._sync()
            if self._owner is None:
                return False
            if self._owner["session_id"] != session_id:
                if disconnected:
                    return False
                raise DesktopInUse(dict(self._owner))
            self._owner = None
            self._pending = None
            self.controller.stop(
                "The interactive desktop client disconnected."
                if disconnected
                else "The desktop task owner released control."
            )
            return True

    def record_input(self, *, tool: str, completed: int, partial: bool = False) -> None:
        actor = current_actor()
        now = self._clock()
        with self._lock:
            state = self._sync()
            receipt = {
                "tool": tool,
                "completed_actions": completed,
                "input_revision": state.input_revision,
                "at": now,
                "session_id": actor.session_id if actor else None,
                "delivery": "partial" if partial else "complete",
                "application_outcome": "unverified",
            }
            self._last_action = receipt
            if state.armed:
                self._pending = dict(receipt)

    def record_observation(self, observation: Observation) -> None:
        actor = current_actor()
        self.controller.checkpoint()
        with self._lock:
            self.controller.checkpoint()
            state = self._sync()
            observed_revision = observation.metadata.get("input_revision")
            if observed_revision is not None and observed_revision != state.input_revision:
                raise RuntimeError("Input changed before the observation was returned.")
            self._last_observation = {
                "frame_id": observation.frame_id,
                "at": self._clock(),
                "captured_at": observation.metadata.get("captured_at"),
                "scope": observation.metadata.get("scope", "active"),
                "session_id": actor.session_id if actor else None,
                "generation": state.generation,
            }
            self._pending = None

    def observation_reference(self, scope: str) -> str | None:
        actor = current_actor()
        with self._lock:
            state = self._sync()
            if (
                actor is not None
                and self._last_observation is not None
                and self._last_observation["scope"] == scope
                and self._last_observation["session_id"] == actor.session_id
                and self._last_observation["generation"] == state.generation
            ):
                return str(self._last_observation["frame_id"])
            return None

    def observation_due(self, session_id: str) -> bool:
        with self._lock:
            self._sync()
            return self._pending is not None and self._pending["session_id"] == session_id

    def record_denial(self, details: dict[str, object]) -> None:
        with self._lock:
            self._last_denial = deepcopy(details)

    def status(self, session_id: str | None = None) -> dict[str, object]:
        now = self._clock()
        with self._lock:
            self._sync()
            return {
                "owner": dict(self._owner) if self._owner is not None else None,
                "owned_by_this_client": (
                    self._owner is not None and self._owner["session_id"] == session_id
                ),
                "observation_due": self._pending is not None,
                "unobserved_action_age": (
                    max(0.0, now - self._pending["at"]) if self._pending is not None else None
                ),
                "last_action": dict(self._last_action) if self._last_action is not None else None,
                "last_observation": (
                    dict(self._last_observation) if self._last_observation is not None else None
                ),
                "last_denial": deepcopy(self._last_denial),
            }
