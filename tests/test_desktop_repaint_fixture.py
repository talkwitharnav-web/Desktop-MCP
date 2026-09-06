from PIL import Image, ImageDraw
import pytest

from tests.desktop_repaint_fixture import assert_repaint_matches, scrollbar_from_pixels


def test_repaint_comparison_rejects_pixels_repaired_only_by_forced_erase(tmp_path):
    before, after = tmp_path / "natural.png", tmp_path / "forced.png"
    with Image.new("RGB", (120, 80), (23, 24, 27)) as image:
        image.save(after)
        ImageDraw.Draw(image).rectangle((20, 30, 70, 45), fill=(235, 235, 235))
        image.save(before)
    with pytest.raises(AssertionError, match="forced erase/repaint"):
        assert_repaint_matches(before, after, (100, 200, 220, 280))
    assert before.with_name("natural-repaint-difference.png").is_file()


def test_repaint_comparison_can_exclude_only_a_known_blinking_caret(tmp_path):
    before, after = tmp_path / "natural.png", tmp_path / "forced.png"
    with Image.new("RGB", (120, 80), (23, 24, 27)) as image:
        image.save(after)
        ImageDraw.Draw(image).rectangle((20, 30, 21, 50), fill="white")
        image.save(before)
    result = assert_repaint_matches(
        before, after, (100, 200, 220, 280), excluded=((120, 230, 122, 251),)
    )
    assert result["changed_pixels"] == 0
    assert result["checked_pixels"] > 9500


def test_scroll_probe_measures_dark_thumb_pixels_not_window_styles(tmp_path):
    path = tmp_path / "bar.png"
    with Image.new("RGB", (20, 120), (28, 29, 32)) as image:
        ImageDraw.Draw(image).rectangle((2, 10, 13, 35), fill=(115, 118, 125))
        image.save(path)
    result = scrollbar_from_pixels(path, (100, 200, 120, 320), (102, 202, 114, 302), 144)
    assert result["width"] == 12
    assert result["mean_luminance"] < 140
    assert 8 <= result["thumb_point"][1] <= 33


def test_scroll_probe_rejects_a_light_stock_looking_bar(tmp_path):
    path = tmp_path / "bar.png"
    with Image.new("RGB", (12, 100), (240, 240, 240)) as image:
        ImageDraw.Draw(image).rectangle((0, 10, 11, 30), fill=(180, 180, 180))
        image.save(path)
    with pytest.raises(AssertionError, match="light stock"):
        scrollbar_from_pixels(path, (100, 200, 112, 300), (100, 200, 112, 300), 144)
