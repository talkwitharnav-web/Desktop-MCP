"""Validated input operations and smooth, minimum-jerk pointer trajectories."""

from __future__ import annotations

import math
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictInt, model_validator

from desktop_mcp.contracts import Point

Button = Literal["left", "right", "middle", "x1", "x2"]
ActionKind = Literal[
    "move",
    "click",
    "drag",
    "scroll",
    "key",
    "text",
    "wait",
    "key_down",
    "key_up",
    "button_down",
    "button_up",
]

_KEYS = {
    "backspace": 0x08,
    "back": 0x08,
    "tab": 0x09,
    "enter": 0x0D,
    "return": 0x0D,
    "shift": 0x10,
    "ctrl": 0x11,
    "control": 0x11,
    "alt": 0x12,
    "pause": 0x13,
    "capslock": 0x14,
    "esc": 0x1B,
    "escape": 0x1B,
    "space": 0x20,
    "pageup": 0x21,
    "pgup": 0x21,
    "pagedown": 0x22,
    "pgdn": 0x22,
    "end": 0x23,
    "home": 0x24,
    "left": 0x25,
    "up": 0x26,
    "right": 0x27,
    "down": 0x28,
    "printscreen": 0x2C,
    "insert": 0x2D,
    "delete": 0x2E,
    "win": 0x5B,
    "windows": 0x5B,
    "lwin": 0x5B,
    "rwin": 0x5C,
    "menu": 0x5D,
    "multiply": 0x6A,
    "add": 0x6B,
    "plus": 0x6B,
    "subtract": 0x6D,
    "decimal": 0x6E,
    "divide": 0x6F,
    "numlock": 0x90,
    "scrolllock": 0x91,
    "lshift": 0xA0,
    "rshift": 0xA1,
    "lctrl": 0xA2,
    "rctrl": 0xA3,
    "lalt": 0xA4,
    "ralt": 0xA5,
    "browserback": 0xA6,
    "browserforward": 0xA7,
    "volumemute": 0xAD,
    "volumedown": 0xAE,
    "volumeup": 0xAF,
    "medianext": 0xB0,
    "mediaprevious": 0xB1,
    "mediastop": 0xB2,
    "mediaplaypause": 0xB3,
    "semicolon": 0xBA,
    "equals": 0xBB,
    "comma": 0xBC,
    "minus": 0xBD,
    "period": 0xBE,
    "slash": 0xBF,
    "backtick": 0xC0,
    "leftbracket": 0xDB,
    "backslash": 0xDC,
    "rightbracket": 0xDD,
    "quote": 0xDE,
}
_KEYS.update({f"f{index}": 0x6F + index for index in range(1, 25)})
_KEYS.update({f"numpad{index}": 0x60 + index for index in range(10)})


def key_code(name: str) -> int:
    """Resolve a named physical key; literal Unicode text belongs in Type."""
    normalized = name.casefold().replace("_", "").replace("-", "").replace(" ", "")
    if len(normalized) == 1 and normalized.isascii() and normalized.isalnum():
        return ord(normalized.upper())
    try:
        return _KEYS[normalized]
    except KeyError:
        raise ValueError(f"Unknown key name: {name!r}. Use Type for literal text.") from None


def parse_shortcut(shortcut: str) -> list[str]:
    """Parse a plus-separated chord, including a trailing literal plus key."""
    if shortcut.endswith("++"):
        keys = shortcut[:-1].split("+")[:-1] + ["plus"]
    else:
        keys = shortcut.split("+")
    if not keys or any(not key.strip() for key in keys):
        raise ValueError("A shortcut needs nonempty key names separated by +.")
    for key in keys:
        key_code(key)
    return keys


def ease_motion(progress: float) -> float:
    """Minimum-jerk easing has zero velocity and acceleration at both endpoints."""
    progress = min(1.0, max(0.0, progress))
    return progress**3 * (10.0 + progress * (-15.0 + 6.0 * progress))


def motion_duration(start: Point, end: Point) -> float:
    """Keep small moves responsive and long moves visibly accelerated."""
    distance = math.hypot(end[0] - start[0], end[1] - start[1])
    return min(0.65, max(0.08, 0.06 + math.sqrt(distance) / 85.0))


class Action(BaseModel):
    """One input step; held inputs are scoped to the enclosing batch."""

    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)

    kind: ActionKind
    loc: tuple[StrictInt, StrictInt] | None = None
    start: tuple[StrictInt, StrictInt] | None = None
    button: Button = "left"
    keys: list[str] = Field(default_factory=list, max_length=16)
    text: str | None = None
    duration: float | None = Field(default=None, ge=0, le=10, strict=True)
    clicks: int = Field(default=1, ge=1, le=3, strict=True)
    repeat: int = Field(default=1, ge=1, le=1000, strict=True)
    delta_x: int = Field(default=0, ge=-120000, le=120000, strict=True)
    delta_y: int = Field(default=0, ge=-120000, le=120000, strict=True)
    clear: bool = Field(default=False, strict=True)
    submit: bool = Field(default=False, strict=True)
    frame_id: str | None = None

    @model_validator(mode="after")
    def validate_operation(self) -> Action:
        if self.kind in {"move", "drag"} and self.loc is None:
            raise ValueError("move and drag require loc=[x, y].")
        if self.start is not None and self.kind != "drag":
            raise ValueError("start is only valid for a drag.")
        if self.kind in {"key", "key_down", "key_up"} and not self.keys:
            raise ValueError("Keyboard operations require at least one key.")
        codes = [key_code(key) for key in self.keys]
        if len(codes) != len(set(codes)):
            raise ValueError("A chord cannot contain the same key twice.")
        if self.kind == "text":
            if self.text is None:
                raise ValueError("A text operation requires text.")
            try:
                self.text.encode("utf-16-le")
            except UnicodeEncodeError:
                raise ValueError("Text contains an unpaired Unicode surrogate.") from None
        elif self.text is not None or self.clear or self.submit:
            raise ValueError("text, clear and submit are only valid for text input.")
        if self.kind == "wait" and self.duration is None:
            raise ValueError("A wait requires duration in seconds.")
        if self.duration == 0 and self.kind in {"move", "click", "drag"}:
            raise ValueError("Motion duration must be positive; pointer moves are smooth.")
        if self.frame_id is not None and self.loc is None and self.start is None:
            raise ValueError("frame_id needs an image coordinate in loc or start.")
        if self.frame_id is not None and not self.frame_id.strip():
            raise ValueError("frame_id must not be empty.")
        if self.kind not in {"key", "key_down", "key_up", "click", "drag", "scroll"} and self.keys:
            raise ValueError("keys are only valid for keyboard operations or mouse modifiers.")
        return self
