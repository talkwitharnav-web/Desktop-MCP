from itertools import combinations

import pytest

from desktop_mcp.transcript_layout import (
    BOTTOM,
    CLEAR,
    COMPOSER,
    COMPOSER_LABEL,
    EXPAND,
    HISTORY,
    HISTORY_LABEL,
    LATEST,
    PIN,
    SEND,
    STATUS,
    STOP,
    TASKBAR,
    TOP,
    fit_window,
    layout_client,
    minimum_client_height,
    preferred_size,
    usable_area,
)


REQUIRED = {
    PIN,
    TOP,
    BOTTOM,
    CLEAR,
    STOP,
    SEND,
    EXPAND,
    TASKBAR,
    LATEST,
    HISTORY,
    STATUS,
    COMPOSER,
    HISTORY_LABEL,
}


def assert_contained(layout, width, height):
    assert REQUIRED <= layout.controls.keys()
    for left, top, right, bottom in layout.controls.values():
        assert 0 <= left < right <= width
        assert 0 <= top < bottom <= height
    for a, b in combinations(layout.controls.values(), 2):
        assert a[2] <= b[0] or b[2] <= a[0] or a[3] <= b[1] or b[3] <= a[1]


@pytest.mark.parametrize("dpi", [96, 144, 192, 288])
def test_default_is_a_short_wide_readable_ribbon_including_window_chrome(dpi):
    scale = dpi / 96
    work = (0, 0, round(1920 * scale), round(1040 * scale))
    chrome = round(16 * scale), round(39 * scale)
    width, height = preferred_size(work, scale, chrome, compact=True, dock="bottom")
    assert (width, height) == (round(1120 * scale), round(164 * scale))
    assert height < 0.4 * 430 * scale
    client_width, client_height = width - chrome[0], height - chrome[1]
    layout = layout_client(client_width, client_height, scale, compact=True)
    assert layout.split
    assert layout.font_height == round(14 * scale)
    assert layout.scale == scale
    for identifier in (HISTORY, COMPOSER, SEND):
        rect = layout.controls[identifier]
        assert rect[3] - rect[1] >= 3 * layout.font_height
    assert layout.controls[HISTORY][2] < layout.controls[COMPOSER][0]
    assert COMPOSER_LABEL in layout.controls
    assert_contained(layout, client_width, client_height)


@pytest.mark.parametrize("dpi", [96, 144, 192, 288])
@pytest.mark.parametrize("compact", [True, False])
@pytest.mark.parametrize(
    "client",
    [
        (1104, 125),
        (920, 190),
        (744, 125),
        (624, 177),
        (344, 190),
        (304, 160),
        (200, 91),
        (120, 70),
        (80, 50),
        (1400, 700),
    ],
)
def test_actual_client_bounds_win_over_every_fixed_minimum(dpi, compact, client):
    layout = layout_client(*client, dpi / 96, compact=compact)
    assert 1 <= layout.font_height <= round(14 * dpi / 96)
    assert_contained(layout, *client)


@pytest.mark.parametrize("dpi", [96, 144, 192, 288])
@pytest.mark.parametrize("dock", ["top", "bottom", "taskbar-edge", "floating"])
@pytest.mark.parametrize(
    "area",
    [(0, 0, 640, 360), (-800, -600, -160, -240), (1920, 40, 2400, 340), (-320, 90, 0, 330)],
)
def test_small_and_negative_origin_areas_bound_defaults_and_oversized_minima(dpi, dock, area):
    scale = dpi / 96
    chrome = round(16 * scale), round(39 * scale)
    size = preferred_size(area, scale, chrome, compact=True, dock=dock)
    rectangle = fit_window(
        (-5000, 8000, -5000 + size[0], 8000 + size[1]),
        area,
        scale,
        dock=dock,
        minimum=(5000, 5000),
    )
    left, top, right, bottom = usable_area(area, scale, dock)
    assert left <= rectangle[0] < rectangle[2] <= right
    assert top <= rectangle[1] < rectangle[3] <= bottom
    if dock == "top":
        assert rectangle[1] == top
    elif dock in {"bottom", "taskbar-edge"}:
        assert rectangle[3] == bottom
    client = rectangle[2] - rectangle[0] - chrome[0], rectangle[3] - rectangle[1] - chrome[1]
    layout = layout_client(*client, scale, compact=True)
    assert_contained(layout, *client)


def test_taskbar_edge_is_explicit_and_does_not_inset_the_monitor_bottom():
    work, monitor = (0, 0, 1920, 1040), (0, 0, 1920, 1080)
    current = (400, 500, 1520, 664)
    bottom = fit_window(current, work, 1, dock="bottom")
    edge = fit_window(current, monitor, 1, dock="taskbar-edge")
    assert bottom == (400, 868, 1520, 1032)
    assert edge == (400, 916, 1520, 1080)
    assert bottom[3] <= work[3] < edge[3]


def test_narrow_default_stacks_and_toolbar_wraps_without_losing_controls():
    width, height = preferred_size((0, 0, 380, 800), 1, (16, 39), compact=True, dock="bottom")
    assert width == 364
    assert 216 <= height < 260
    layout = layout_client(width - 16, height - 39, 1, compact=True)
    assert not layout.split
    assert layout.font_height == 14
    assert layout.controls[HISTORY][3] < layout.controls[COMPOSER][1]
    assert layout.controls[PIN][1] < layout.controls[STOP][1]
    assert_contained(layout, width - 16, height - 39)


def test_expanded_and_manually_taller_windows_spend_extra_space_on_history():
    compact = layout_client(1104, 125, 1, compact=True)
    expanded = layout_client(1104, 401, 1, compact=False)
    taller = layout_client(1104, 700, 1, compact=True)
    assert compact.split and not expanded.split and not taller.split
    small = compact.controls[HISTORY]
    big = expanded.controls[HISTORY]
    assert big[3] - big[1] > 4 * (small[3] - small[1])
    assert big[2] - big[0] > small[2] - small[0]
    assert taller.controls[COMPOSER][3] - taller.controls[COMPOSER][1] == 34
    assert taller.controls[HISTORY][3] - taller.controls[HISTORY][1] > 500
    assert expanded.controls[COMPOSER][3] - expanded.controls[COMPOSER][1] == 54


@pytest.mark.parametrize("width", [320, 360, 640, 900, 1120])
@pytest.mark.parametrize("compact", [True, False])
def test_readable_minimum_and_layout_share_the_same_constraints(width, compact):
    height = minimum_client_height(width, 1, compact=compact)
    layout = layout_client(width, height, 1, compact=compact)
    assert layout.font_height == 14
    assert layout.scale == 1
    assert_contained(layout, width, height)


@pytest.mark.parametrize(
    "width,height,scale",
    [(0, 10, 1), (10, 0, 1), (10, 10, 0), (10, 10, float("nan")), (10, 10, float("inf"))],
)
def test_invalid_geometry_is_not_silently_published(width, height, scale):
    with pytest.raises(ValueError, match="positive"):
        layout_client(width, height, scale, compact=True)
