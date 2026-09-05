"""Shared, bounded uploads to a Windows layered overlay."""

from __future__ import annotations

from PIL.Image import Image

from desktop_mcp.contracts import Point


def upload_rgba(handle: int, origin: Point, image: Image) -> None:
    """Reuse the retained engine's checked DIB lifecycle, with C-level premultiplication."""
    if image.mode != "RGBA" or min(image.size) < 1 or image.width * image.height > 16_777_216:
        raise ValueError("A layered image must be bounded, nonempty RGBA pixels.")
    from windows_mcp.desktop.flash_overlay import _push_bitmap

    pixels = image.convert("RGBa").tobytes("raw", "BGRa")
    _push_bitmap(handle, origin[0], origin[1], image.width, image.height, pixels)
