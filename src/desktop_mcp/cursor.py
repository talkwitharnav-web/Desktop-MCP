"""Pure, DPI-scaled artwork for the visible Desktop-MCP pointer.

The overlay does not replace or hide the Windows system cursor. Its hotspot
follows the physical pointer; the input backend alone controls movement timing.
"""

from __future__ import annotations

from dataclasses import dataclass
from math import hypot

from PIL import Image, ImageDraw, ImageFilter

from desktop_mcp.contracts import Point


@dataclass(frozen=True)
class CursorSprite:
    """A straight-alpha cursor image and its physical-pixel hotspot."""

    image: Image.Image
    hotspot: Point


def _rounded_contour(
    vertices: tuple[tuple[float, float], ...], radius: float
) -> list[tuple[float, float]]:
    contour = []
    for index, vertex in enumerate(vertices):
        previous = vertices[index - 1]
        following = vertices[(index + 1) % len(vertices)]
        before = hypot(previous[0] - vertex[0], previous[1] - vertex[1])
        after = hypot(following[0] - vertex[0], following[1] - vertex[1])
        distance = min(radius, before / 3, after / 3)
        entry = tuple(
            vertex[axis] + (previous[axis] - vertex[axis]) * distance / before for axis in (0, 1)
        )
        exit_point = tuple(
            vertex[axis] + (following[axis] - vertex[axis]) * distance / after for axis in (0, 1)
        )
        for step in range(9):
            t = step / 8
            contour.append(
                tuple(
                    (1 - t) ** 2 * entry[axis]
                    + 2 * (1 - t) * t * vertex[axis]
                    + t**2 * exit_point[axis]
                    for axis in (0, 1)
                )
            )
    return contour


def render_cursor(dpi: int = 96) -> CursorSprite:
    """Render a rounded, recognizable arrow in black and neutral greys.

    Args:
        dpi: Effective monitor DPI, from 48 through 768.

    Returns:
        Artwork with transparent margins, an antialiased edge, and a soft shadow.
    """
    if isinstance(dpi, bool) or not isinstance(dpi, int) or not 48 <= dpi <= 768:
        raise ValueError("Cursor DPI must be an integer between 48 and 768")
    scale = dpi / 96
    size = (round(36 * scale), round(44 * scale))
    supersample = 4
    factor = scale * supersample
    large_size = tuple(dimension * supersample for dimension in size)
    vertices = (
        (6.0, 4.0),
        (6.0, 30.0),
        (12.0, 24.8),
        (17.4, 36.0),
        (22.4, 33.5),
        (17.1, 22.4),
        (27.0, 22.0),
    )
    contour = [(x * factor, y * factor) for x, y in _rounded_contour(vertices, radius=1.15)]
    mask = Image.new("L", large_size)
    ImageDraw.Draw(mask).polygon(contour, fill=255)

    shadow_mask = Image.new("L", large_size)
    shadow_mask.paste(mask, (round(1.2 * factor), round(1.6 * factor)))
    shadow_mask = shadow_mask.filter(ImageFilter.GaussianBlur(1.25 * factor))
    shadow_mask = shadow_mask.point(lambda alpha: round(alpha * 0.40))
    artwork = Image.new("RGBA", large_size, (0, 0, 0, 0))
    artwork.putalpha(shadow_mask)

    arrow = Image.new("RGBA", large_size)
    draw = ImageDraw.Draw(arrow)
    for y in range(large_size[1]):
        grey = round(43 - 21 * y / max(1, large_size[1] - 1))
        draw.line((0, y, large_size[0], y), fill=(grey, grey, grey, 255))
    arrow.putalpha(mask)
    draw.line(
        contour + [contour[0]],
        fill=(170, 170, 170, 255),
        width=max(1, round(1.15 * factor)),
        joint="curve",
    )
    artwork = Image.alpha_composite(artwork, arrow)
    artwork = artwork.resize(size, Image.Resampling.LANCZOS)
    return CursorSprite(artwork, (round(6 * scale), round(5 * scale)))


def premultiplied_bgra(image: Image.Image) -> bytes:
    """Return the premultiplied BGRA bytes required by UpdateLayeredWindow."""
    if image.mode != "RGBA":
        raise ValueError("A layered cursor requires an RGBA image")
    pixels = bytearray(image.tobytes("raw", "BGRA"))
    for offset in range(0, len(pixels), 4):
        alpha = pixels[offset + 3]
        for channel in range(3):
            pixels[offset + channel] = (pixels[offset + channel] * alpha + 127) // 255
    return bytes(pixels)
