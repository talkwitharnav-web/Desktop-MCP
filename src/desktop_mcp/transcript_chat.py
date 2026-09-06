"""Bounded message geometry and reading positions, without native windows."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import math

from desktop_mcp.conversation import MAX_ENTRIES, MAX_TEXT

BACKGROUND = (23, 26, 31)
ASSISTANT_BACKGROUND = (33, 39, 47)
USER_BACKGROUND = (39, 49, 61)
TEXT_COLOR = (237, 240, 244)
LABEL_COLOR = (177, 193, 210)
BORDER_COLOR = (57, 66, 79)
FOCUS_COLOR = (133, 157, 184)
ARRIVAL_SECONDS = 0.18

EntryTuple = tuple[int, str, str, str]


@dataclass(frozen=True)
class ChatEntry:
    sequence: int
    title: str
    text: str
    role: str

    @property
    def label(self) -> str:
        if self.role == "user":
            return "You"
        return "Assistant" if self.title == "Assistant" else f"Assistant · {self.title}"


@dataclass(frozen=True)
class MessageView:
    """Native text offsets are UTF-16 code units, local to one stable sequence."""

    sequence: int
    anchor: int = 0
    selection: tuple[int, int] = (0, 0)


@dataclass(frozen=True)
class ReadingAnchor:
    sequence: int
    offset: int = 0


@dataclass(frozen=True)
class ChatView:
    anchor: ReadingAnchor | None = None
    messages: tuple[MessageView, ...] = ()
    following: bool = False


@dataclass(frozen=True)
class BubbleSize:
    x: int
    width: int
    padding: int
    label_height: int
    body_width: int
    body_cap: int


@dataclass(frozen=True)
class BubbleBox:
    sequence: int
    role: str
    x: int
    y: int
    width: int
    height: int
    body_height: int
    size: BubbleSize

    @property
    def bottom(self) -> int:
        return self.y + self.height


def native_text(text: str) -> str:
    """Normalize line endings without changing any other conversation characters."""
    return text.replace("\r\n", "\n").replace("\r", "\n").replace("\n", "\r\n")


def utf16_length(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def validate_entries(entries: Sequence[EntryTuple]) -> tuple[ChatEntry, ...]:
    """Reject invalid/unbounded input rather than silently dropping message text."""
    if len(entries) > MAX_ENTRIES:
        raise ValueError("Chat history exceeds the bounded conversation size.")
    result = []
    previous = -1
    for sequence, title, text, role in entries:
        if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence <= previous:
            raise ValueError("Chat sequences must be distinct and increasing.")
        if role not in ("assistant", "user"):
            raise ValueError("Chat messages require an assistant or user role.")
        for value, limit in ((title, 256), (text, MAX_TEXT)):
            if not isinstance(value, str) or len(value) > limit or "\0" in value:
                raise ValueError("Chat text is invalid or exceeds its native display bound.")
            try:
                value.encode("utf-16-le")
            except UnicodeEncodeError as error:
                raise ValueError("Chat text contains invalid Unicode.") from error
        result.append(ChatEntry(sequence, title, native_text(text), role))
        previous = sequence
    return tuple(result)


def bubble_size(width: int, height: int, scale: float, line_height: int, role: str) -> BubbleSize:
    """Cap each native text viewport; never create a document-height child HWND."""
    if (
        not 1 <= width <= 32760
        or not 1 <= height <= 32760
        or not 1 <= line_height <= 1024
        or not math.isfinite(scale)
        or not 0 < scale <= 16
    ):
        raise ValueError("Chat geometry exceeds its bounded native viewport or font metrics.")
    margin = min(round(8 * scale), max(0, (width - 1) // 4))
    available = max(1, width - 2 * margin)
    inset = min(round(38 * scale), available // 10)
    bubble_width = max(1, min(available - inset, round(820 * scale)))
    padding = min(round(10 * scale), max(0, (bubble_width - 1) // 4))
    label_height = line_height + max(1, round(4 * scale))
    cap = max(
        line_height,
        min(round(300 * scale), height - label_height - 2 * padding - round(8 * scale)),
    )
    x = margin if role == "assistant" else width - margin - bubble_width
    return BubbleSize(
        x, bubble_width, padding, label_height, max(1, bubble_width - 2 * padding), cap
    )


def layout_bubbles(
    entries: Sequence[ChatEntry],
    body_heights: Mapping[int, int],
    width: int,
    height: int,
    scale: float,
    line_height: int,
) -> tuple[BubbleBox, ...]:
    gap = max(1, round(8 * scale))
    y = max(0, round(4 * scale))
    boxes = []
    for entry in entries:
        size = bubble_size(width, height, scale, line_height, entry.role)
        body_height = max(1, min(size.body_cap, body_heights[entry.sequence]))
        box_height = 2 * size.padding + size.label_height + body_height
        boxes.append(
            BubbleBox(
                entry.sequence,
                entry.role,
                size.x,
                y,
                size.width,
                box_height,
                body_height,
                size,
            )
        )
        y += box_height + gap
    return tuple(boxes)


def content_height(boxes: Sequence[BubbleBox], scale: float) -> int:
    return max(1, boxes[-1].bottom + round(4 * scale)) if boxes else 1


def anchor_at(boxes: Sequence[BubbleBox], position: int) -> ReadingAnchor | None:
    if not boxes:
        return None
    box = boxes[0]
    for candidate in boxes:
        if candidate.y > position:
            break
        box = candidate
    return ReadingAnchor(box.sequence, max(0, position) - box.y)


def remap_anchor(boxes: Sequence[BubbleBox], anchor: ReadingAnchor | None) -> ReadingAnchor | None:
    if not boxes:
        return None
    if anchor is not None:
        for box in boxes:
            if box.sequence == anchor.sequence:
                # A temporary smaller viewport must not erase the intended reading offset.
                return anchor
        for box in boxes:
            if box.sequence > anchor.sequence:
                return ReadingAnchor(box.sequence)
    return ReadingAnchor(boxes[0].sequence)


def anchor_position(boxes: Sequence[BubbleBox], anchor: ReadingAnchor | None) -> int:
    anchor = remap_anchor(boxes, anchor)
    if anchor is None:
        return 0
    index = next(index for index, box in enumerate(boxes) if box.sequence == anchor.sequence)
    box = boxes[index]
    extent = boxes[index + 1].y - box.y - 1 if index + 1 < len(boxes) else box.height
    return box.y + max(-box.y, min(anchor.offset, extent))


def arrival_offset(now: float, started: float, scale: float) -> int:
    """A short ease-out slide; the native text itself is never withheld or streamed."""
    progress = max(0.0, min(1.0, (now - started) / ARRIVAL_SECONDS))
    return max(0, round(6 * scale * (1 - progress) ** 3))
