"""Pure artwork checks; no native cursor APIs or desktop captures."""

import pytest
from PIL import Image

from desktop_mcp.cursor import premultiplied_bgra, render_cursor


@pytest.mark.parametrize(
    "dpi,size,hotspot",
    [
        (96, (36, 44), (6, 5)),
        (144, (54, 66), (9, 8)),
        (192, (72, 88), (12, 10)),
    ],
)
def test_cursor_scales_artwork_and_hotspot(dpi, size, hotspot):
    sprite = render_cursor(dpi)
    assert sprite.image.mode == "RGBA"
    assert sprite.image.size == size
    assert sprite.hotspot == hotspot
    assert sprite.image.getpixel(hotspot)[3] > 160


def test_cursor_has_only_neutral_greys_and_transparent_margins():
    image = render_cursor().image
    pixels = [image.getpixel((x, y)) for y in range(image.height) for x in range(image.width)]
    assert all(red == green == blue for red, green, blue, _ in pixels)
    assert any(alpha == 255 and red < 50 for red, _, _, alpha in pixels)
    assert any(alpha == 255 and 80 < red < 200 for red, _, _, alpha in pixels)
    assert any(0 < alpha < 255 for _, _, _, alpha in pixels)
    for point in ((0, 0), (35, 0), (0, 43), (35, 43)):
        assert image.getpixel(point)[3] == 0


def test_cursor_is_an_arrow_with_a_narrow_tip_wide_head_and_slanted_stem():
    alpha = render_cursor().image.getchannel("A")

    def solid_width(y):
        return sum(alpha.getpixel((x, y)) > 200 for x in range(alpha.width))

    assert 0 < solid_width(6) < solid_width(20)
    assert solid_width(20) > 15
    assert 3 <= solid_width(32) < solid_width(20) / 2
    for point in ((7, 12), (12, 18), (20, 20), (18, 32)):
        assert alpha.getpixel(point) > 200
    for point in ((3, 20), (25, 8), (26, 32)):
        assert alpha.getpixel(point) < 80


def test_cursor_rendering_is_deterministic_and_returns_independent_images():
    first = render_cursor(144)
    second = render_cursor(144)
    assert first.image is not second.image
    assert first.image.tobytes() == second.image.tobytes()
    first.image.putpixel(first.hotspot, (0, 0, 0, 0))
    assert second.image.getpixel(second.hotspot)[3] > 0


@pytest.mark.parametrize("dpi", [0, -96, 47, 769, True, 96.0, "96", None])
def test_invalid_dpi_is_rejected(dpi):
    with pytest.raises(ValueError, match="DPI"):
        render_cursor(dpi)


def test_premultiplication_clears_transparent_rgb_and_orders_bgra():
    image = Image.new("RGBA", (3, 1))
    image.putdata([(20, 40, 60, 128), (255, 90, 40, 0), (20, 20, 20, 255)])
    assert premultiplied_bgra(image) == bytes((30, 20, 10, 128) + (0, 0, 0, 0) + (20, 20, 20, 255))
    assert image.getpixel((0, 0)) == (20, 40, 60, 128)


def test_premultiplication_requires_explicit_alpha():
    with pytest.raises(ValueError, match="RGBA"):
        premultiplied_bgra(Image.new("RGB", (1, 1)))
