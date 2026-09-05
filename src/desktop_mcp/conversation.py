"""Bounded, two-way transcript messages; never grants desktop permissions."""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
import math
import threading
import time
from typing import Literal

MAX_ENTRIES = 32
MAX_PENDING = 32
MAX_TEXT = 16_000
_LEASE_SECONDS = 120.0


@dataclass(frozen=True)
class TranscriptEntry:
    sequence: int
    title: str
    text: str
    created_at: float
    role: Literal["assistant", "user"] = "assistant"


def message_text(value: object, name: str, maximum: int, *, multiline: bool = False) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise ValueError(f"{name} must contain 1..{maximum} characters.")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as error:
        raise ValueError(f"{name} contains invalid Unicode.") from error
    allowed = "\t\r\n" if multiline else ""
    if any((ord(char) < 32 or 127 <= ord(char) <= 159) and char not in allowed for char in value):
        raise ValueError(f"{name} contains unsupported control characters.")
    return value


class Conversation:
    """Messages are retained until answered; only one MCP session listens at a time."""

    def __init__(
        self, *, is_closed: Callable[[], bool], clock: Callable[[], float] = time.monotonic
    ) -> None:
        self._is_closed = is_closed
        self._clock = clock
        self._lock = threading.RLock()
        self._entries: tuple[TranscriptEntry, ...] = ()
        self._pending: OrderedDict[int, TranscriptEntry] = OrderedDict()
        self._sequence = 0
        self._owner: str | None = None
        self._owner_label = ""
        self._lease_until = 0.0
        self._waiting = False
        self._delivered: int | None = None
        self._closed = False

    def ensure_open(self) -> None:
        if self._closed or self._is_closed():
            raise RuntimeError("Desktop-MCP has closed; the conversation is no longer available.")

    def _now(self) -> float:
        value = self._clock()
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
        ):
            raise RuntimeError("The conversation clock must return finite monotonic seconds.")
        return float(value)

    def entries(self) -> tuple[TranscriptEntry, ...]:
        with self._lock:
            return self._entries

    def _expire(self, now: float) -> None:
        if self._owner is not None and now >= self._lease_until:
            self._owner = None
            self._owner_label = ""
            self._waiting = False
            self._delivered = None

    def status(self) -> dict[str, object]:
        now = self._now()
        with self._lock:
            self._expire(now)
            return {
                "pending_messages": len(self._pending),
                "listener_connected": self._owner is not None,
                "listener_waiting": self._waiting,
                "listener_name": self._owner_label or None,
                "awaiting_reply": self._delivered is not None,
            }

    def _append(
        self, text: str, title: str, role: Literal["assistant", "user"], now: float
    ) -> TranscriptEntry:
        if self._closed:
            raise RuntimeError("Desktop-MCP has closed; the message was not accepted.")
        self._sequence += 1
        entry = TranscriptEntry(self._sequence, title, text, now, role)
        self._entries = (*self._entries, entry)[-MAX_ENTRIES:]
        return entry

    def send_user(self, text: str) -> TranscriptEntry:
        """Local composer only. A failed send leaves the draft with its caller."""
        text = message_text(text, "Message", MAX_TEXT, multiline=True)
        self.ensure_open()
        now = self._now()
        with self._lock:
            if len(self._pending) >= MAX_PENDING:
                raise ValueError(
                    "The message queue is full. Wait for an agent reply before sending more."
                )
            entry = self._append(text, "You", "user", now)
            self._pending[entry.sequence] = entry
            return entry

    def reply(
        self,
        text: str,
        *,
        title: str = "Assistant",
        reply_to: int | None = None,
        owner: str | None = None,
    ) -> TranscriptEntry:
        text = message_text(text, "text", MAX_TEXT, multiline=True)
        title = message_text(title, "title", 256)
        self.ensure_open()
        now = self._now()
        with self._lock:
            self._expire(now)
            if reply_to is not None:
                if isinstance(reply_to, bool) or not isinstance(reply_to, int):
                    raise ValueError("reply_to must be the received message id.")
                if (
                    owner != self._owner
                    or reply_to != self._delivered
                    or reply_to not in self._pending
                ):
                    raise ValueError(
                        "That message is not assigned to this listener. Read it again first."
                    )
            entry = self._append(text, title, "assistant", now)
            if reply_to is not None:
                del self._pending[reply_to]
                self._delivered = None
                self._lease_until = now + _LEASE_SECONDS
            return entry

    async def listen(
        self, owner: str, *, label: str = "Copilot", timeout: float = 25.0
    ) -> dict[str, object]:
        if (
            isinstance(timeout, bool)
            or not isinstance(timeout, (int, float))
            or not math.isfinite(timeout)
            or not 0 <= timeout <= 30
        ):
            raise ValueError("timeout must be a finite number between 0 and 30 seconds.")
        if not isinstance(owner, str) or not owner:
            raise ValueError("A connected MCP session is required to listen.")
        label = message_text(label, "listener name", 120)
        self.ensure_open()
        started = self._now()
        with self._lock:
            if self._closed:
                raise RuntimeError("Desktop-MCP has closed; the listener cannot start.")
            self._expire(started)
            if self._owner is not None and self._owner != owner:
                raise RuntimeError("Another Copilot session is handling this transcript.")
            if self._waiting:
                raise RuntimeError("A transcript read is already waiting.")
            self._owner, self._owner_label = owner, label
            self._lease_until = started + _LEASE_SECONDS
            self._waiting = True
        try:
            while True:
                self.ensure_open()
                now = self._now()
                with self._lock:
                    if self._owner != owner:
                        raise RuntimeError("The transcript listener disconnected.")
                    if self._pending:
                        entry = next(iter(self._pending.values()))
                        self._delivered = entry.sequence
                        self._lease_until = now + _LEASE_SECONDS
                        return {
                            "message": {"id": entry.sequence, "text": entry.text, "role": "user"},
                            "pending_messages": len(self._pending),
                            "timed_out": False,
                        }
                    if now >= started + timeout:
                        self._lease_until = now + _LEASE_SECONDS
                        return {"message": None, "pending_messages": 0, "timed_out": True}
                await asyncio.sleep(min(0.1, max(0.0, started + timeout - now)))
        finally:
            with self._lock:
                if self._owner == owner:
                    self._waiting = False

    def release_listener(self, owner: str) -> bool:
        with self._lock:
            if self._owner == owner:
                self._owner = None
                self._owner_label = ""
                self._waiting = False
                self._delivered = None
                return True
            return False

    def clear_local(self) -> None:
        with self._lock:
            self._entries = ()
            self._pending.clear()
            self._delivered = None

    def close(self) -> None:
        with self._lock:
            self._closed = True
            self._owner = None
            self._owner_label = ""
            self._waiting = False
            self._delivered = None
