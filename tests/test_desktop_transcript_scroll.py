import pytest

from desktop_mcp.transcript_scroll import (
    MIN_THUMB_DIP,
    ScrollState,
    WHEEL_PAGESCROLL,
    dragged_position,
    thumb_geometry,
    wheel_movement,
)


@pytest.mark.parametrize(
    "lines,first,height,pitch,expected",
    [
        (1, 0, 100, 17, (1, 5, 0)),
        (21, 16, 64, 16, (21, 4, 16)),
        (21, 17, 64, 16, (21, 4, 17)),
        (21, 90, 64, 16, (21, 4, 17)),
        (0, -10, 0, 0, (1, 1, 0)),
        (200000, 150000, 60, 20, (200000, 3, 150000)),
    ],
)
def test_native_line_and_format_metrics_define_range_without_a_stock_scrollbar(
    lines, first, height, pitch, expected
):
    state = ScrollState.from_edit(lines, first, height, pitch)
    assert (state.lines, state.page, state.position) == expected
    assert state.at_end == (state.position >= max(0, state.lines - state.page))


@pytest.mark.parametrize("scale", [1, 1.5, 2, 3])
@pytest.mark.parametrize("height", [1, 8, 34, 59, 240, 700])
@pytest.mark.parametrize("lines,page", [(1, 3), (4, 3), (100, 3), (200000, 20)])
def test_thumb_is_bounded_and_can_reach_both_ends_without_16bit_truncation(
    scale, height, lines, page
):
    state = ScrollState(lines, page, max(0, lines - page) // 2)
    thumb = thumb_geometry(state, height, scale)
    assert 0 <= thumb.track_top <= thumb.top < thumb.bottom <= height
    assert thumb.length <= thumb.track_length
    if state.maximum and thumb.travel:
        minimum = min(round(MIN_THUMB_DIP * scale), max(1, thumb.track_length - 1))
        assert thumb.length >= minimum
        assert dragged_position(state, thumb, -10000, 0.5) == 0
        assert dragged_position(state, thumb, 10000, 0.5) == state.maximum
        assert thumb_geometry(ScrollState(lines, page, 0), height, scale).top == thumb.track_top
        end = thumb_geometry(ScrollState(lines, page, state.maximum), height, scale)
        assert end.bottom == thumb.track_top + thumb.track_length
    else:
        assert dragged_position(state, thumb, height, 0.5) == state.position


@pytest.mark.parametrize("fraction", [0.0, 0.25, 0.5, 0.9, 1.0])
def test_drag_uses_the_grab_offset_instead_of_jumping_the_thumb_center(fraction):
    state = ScrollState(100, 10, 45)
    thumb = thumb_geometry(state, 400, 1)
    pointer = thumb.top + fraction * thumb.length
    assert dragged_position(state, thumb, pointer, fraction) == state.position
    assert thumb.grab_fraction(pointer) == pytest.approx(fraction)


def test_partial_wheel_events_accumulate_and_opposite_motion_cancels():
    assert wheel_movement(0, 30, 3, 10) == (0, 30)
    assert wheel_movement(30, 30, 3, 10) == (0, 60)
    assert wheel_movement(60, 60, 3, 10) == (-3, 0)
    assert wheel_movement(0, -60, 3, 10) == (0, -60)
    assert wheel_movement(-60, -60, 3, 10) == (3, 0)
    assert wheel_movement(60, -60, 3, 10) == (0, 0)
    assert wheel_movement(0, 250, 3, 10) == (-6, 10)


def test_wheel_honors_disabled_scrolling_and_the_windows_page_preference():
    assert wheel_movement(0, 120, 0, 10) == (0, 0)
    assert wheel_movement(0, -120, WHEEL_PAGESCROLL, 10) == (9, 0)
    assert wheel_movement(0, 120, WHEEL_PAGESCROLL, 1) == (-1, 0)
