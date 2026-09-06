"""Deterministic laser geometry and pixels; no native windows, capture or input."""

from __future__ import annotations

from dataclasses import replace
import math

from PIL import Image, ImageChops, ImageStat
import pytest

from desktop_mcp import teaching_render as renderer
from desktop_mcp.teaching import MAX_MARKS, MAX_POINTS, Mark, TeachingSnapshot


def laser(
    points=((10, 30), (210, 30)),
    *,
    duration=10.0,
    width=3.0,
    bounds=None,
) -> Mark:
    return Mark("synthetic-laser", "laser", points, "#ffb454", width, 0.0, duration, None, bounds)


def scene(*marks: Mark) -> TeachingSnapshot:
    return TeachingSnapshot(1, (), marks, None, None)


def head(mark: Mark, now: float) -> tuple[float, float]:
    return renderer._laser_frame(mark, now)[0][-1]


def alpha_peak(mark: Mark, now: float) -> int:
    with renderer.render_marks(scene(mark), (-20, -20, 240, 180), now=now) as image:
        with image.getchannel("A") as alpha:
            return alpha.getextrema()[1]


def test_closed_loop_has_no_head_or_tail_reset_at_any_seam() -> None:
    mark = laser(bounds=(20, 20, 220, 160))
    path = renderer._laser_path(mark.points, mark.laser_bounds)
    dt = 1 / 120
    expected_step = path.length / path.period * dt
    for revolution in (1, 2, 4):
        boundary = path.period * revolution
        before, at, after = (head(mark, boundary + offset) for offset in (-dt, 0, dt))
        assert math.dist(before, at) == pytest.approx(expected_step, rel=0.005)
        assert math.dist(at, after) == pytest.approx(expected_step, rel=0.005)
        trail, opacity = renderer._laser_frame(mark, boundary)
        assert opacity == 1.0
        assert sum(math.dist(a, b) for a, b in zip(trail, trail[1:])) == pytest.approx(
            min(renderer._LASER_TRAIL_LENGTH, path.length * 0.22)
        )
        assert trail[0] != trail[-1]


def test_loop_period_is_independent_of_lifetime_and_does_not_stall_at_75_percent() -> None:
    mark = laser(bounds=(20, 20, 180, 120))
    short = replace(mark, expires_at=2.0)
    assert renderer._laser_frame(mark, 0.7) == renderer._laser_frame(short, 0.7)
    path = renderer._laser_path(mark.points, mark.laser_bounds)
    assert 1.2 <= path.period <= 3.0
    assert head(mark, 0.7) == pytest.approx(head(mark, 0.7 + path.period * 3))
    assert math.dist(head(mark, 8.0), head(mark, 8.1)) > 10
    assert math.dist(head(mark, 9.6), head(mark, 9.7)) > 10


def test_motion_uses_absolute_elapsed_time_not_delivered_frame_count() -> None:
    mark = laser(bounds=(-80, -40, 160, 40))
    expected = renderer._laser_frame(mark, 4.3)
    for now in (9.0, 0.1, 2.0, 7.4, 4.3):
        renderer._laser_frame(mark, now)
    assert renderer._laser_frame(mark, 4.3) == expected
    shifted = replace(mark, created_at=100_000.0, expires_at=100_010.0)
    assert head(shifted, 100_004.3) == pytest.approx(expected[0][-1], abs=1e-7)


def test_loop_images_repeat_and_change_continuously_across_the_seam() -> None:
    mark = laser(bounds=(40, 30, 200, 150))
    period = renderer._laser_path(mark.points, mark.laser_bounds).period
    bounds = (0, 0, 240, 180)
    with (
        renderer.render_marks(scene(mark), bounds, now=period) as at,
        renderer.render_marks(scene(mark), bounds, now=period * 2) as repeated,
        renderer.render_marks(scene(mark), bounds, now=period - 1 / 120) as before,
        renderer.render_marks(scene(mark), bounds, now=period + 1 / 120) as after,
    ):
        assert at.tobytes() == repeated.tobytes()
        changes = []
        for other in (before, after):
            with at.getchannel("A") as a, other.getchannel("A") as b:
                with ImageChops.difference(a, b) as difference:
                    changes.append(sum(ImageStat.Stat(difference).sum) / sum(ImageStat.Stat(a).sum))
        assert 0.03 < min(changes) <= max(changes) < 0.25
        assert changes[0] == pytest.approx(changes[1], rel=0.15)


def test_arc_length_progress_ignores_uneven_and_repeated_vertices() -> None:
    points = ((0, 0), (1, 0), (1, 99), (200, 99), (200, 0), (0, 0))
    path = renderer._laser_path(points)
    assert path.at(50) == (1, 49)
    assert path.at(150) == pytest.approx((51, 99))
    assert path.at(-1) == (1, 0)
    assert path.at(path.length + 50) == path.at(50)
    repeated = tuple(point for point in points for _ in range(3))
    assert renderer._laser_path(repeated) == path
    for now in (0.4, 0.9, 1.7, 3.9, 8.8):
        assert renderer._laser_frame(laser(points), now) == renderer._laser_frame(
            laser(repeated), now
        )


@pytest.mark.parametrize("bounds", [(10, 11, 31, 22), (-110, -60, 900, -51), (-8, -7, -7, -6)])
def test_ellipses_keep_fractional_geometry_and_bounded_sampling(bounds) -> None:
    path = renderer._laser_path(((0, 0),), bounds)
    assert path.closed
    assert path.points[0] == path.points[-1]
    assert 97 <= len(path.points) <= MAX_POINTS
    left, top, right, bottom = bounds
    cx, cy = (left + right) / 2, (top + bottom) / 2
    rx, ry = (right - left) / 2, (bottom - top) / 2
    assert path.points[0] == (right, cy)
    for x, y in path.points:
        assert ((x - cx) / rx) ** 2 + ((y - cy) / ry) ** 2 == pytest.approx(1.0)
    for index in range(100):
        x, y = path.at(path.length * index / 100)
        assert 0.998 < ((x - cx) / rx) ** 2 + ((y - cy) / ry) ** 2 <= 1.000001


def test_open_path_sweeps_once_then_retracts_to_a_stationary_endpoint() -> None:
    mark = laser()
    assert head(mark, 0) == mark.points[0]
    positions = [head(mark, now)[0] for now in (0.1, 0.3, 0.6, 0.9, 1.1)]
    assert positions == sorted(positions)
    assert 10 < positions[0] < positions[-1] < 210
    for now in (1.4, 3.0, 9.0):
        trail, opacity = renderer._laser_frame(mark, now)
        assert trail == ((210, 30),)
        assert opacity == 1.0
    assert alpha_peak(mark, 1e-12) == 0


def test_open_trail_is_distance_faded_even_with_only_two_vertices() -> None:
    mark = laser(((0, 30), (300, 30)))
    with renderer.render_marks(scene(mark), (-10, 0, 310, 60), now=0.6) as image:
        alphas = [image.getpixel((x + 10, 30))[3] for x in (88, 98, 108, 118, 128, 138)]
        assert alphas == sorted(alphas)
        assert alphas[0] < 35
        assert alphas[-1] > 200
        assert image.getpixel((20, 0))[3] == 0


@pytest.mark.parametrize("duration", [1e-6, 0.01, 0.1, 2.0, 10.0])
def test_point_laser_fades_in_and_out_even_for_tiny_lifetimes(duration) -> None:
    mark = laser(((40, 40),), duration=duration)
    entry = min(0.14, duration * 0.2)
    exit_time = min(0.3, duration * 0.25)
    before = alpha_peak(mark, -duration)
    start = alpha_peak(mark, 0)
    rising = [alpha_peak(mark, entry * fraction) for fraction in (0.1, 0.5, 1.0)]
    falling = [alpha_peak(mark, duration - exit_time * fraction) for fraction in (1.0, 0.5, 0.1)]
    assert before == start == alpha_peak(mark, duration) == 0
    assert 0 < rising[0] < rising[1] < rising[2]
    assert falling[0] > falling[1] > falling[2] > 0
    assert rising[2] > 240
    assert rising[1] == pytest.approx(falling[1], abs=2)


@pytest.mark.parametrize("duration", [1e-6, 0.01])
@pytest.mark.parametrize("bounds", [None, (20, 20, 180, 120)])
def test_tiny_moving_laser_lifetimes_stay_finite_and_expire(duration, bounds) -> None:
    mark = laser(duration=duration, bounds=bounds)
    for fraction in (0.1, 0.5, 0.9):
        trail, opacity = renderer._laser_frame(mark, duration * fraction)
        assert 0 < opacity <= 1
        assert all(math.isfinite(value) for point in trail for value in point)
        assert alpha_peak(mark, duration * fraction) > 0
    assert alpha_peak(mark, duration) == 0


def test_repeated_point_is_finite_stationary_and_has_the_same_pixels() -> None:
    mark = laser(((60, 60),))
    repeated = replace(mark, points=mark.points * MAX_POINTS)
    path = renderer._laser_path(repeated.points)
    assert not path.closed and path.length == 0
    for now in (0.3, 2.0, 8.0):
        assert head(repeated, now) == (60, 60)
        with (
            renderer.render_marks(scene(mark), (30, 30, 90, 90), now=now) as single,
            renderer.render_marks(scene(repeated), (30, 30, 90, 90), now=now) as duplicate,
        ):
            assert single.tobytes() == duplicate.tobytes()


@pytest.mark.parametrize("width", [0.5, 3.0, 32.0])
@pytest.mark.parametrize("ellipse", [False, True])
def test_visible_bounds_cover_negative_origin_laser_and_all_glow(width, ellipse) -> None:
    mark = laser(
        ((-15, -12),),
        width=width,
        bounds=(-50, -40, 10, 16) if ellipse else None,
    )
    snapshot = scene(mark)
    declared = renderer.visible_bounds(snapshot, now=0.7)
    left, top, right, bottom = declared
    bounds = (left - 10, top - 10, right + 10, bottom + 10)
    with renderer.render_marks(snapshot, bounds, now=0.7) as image:
        actual = image.getbbox()
        assert actual is not None
        assert actual[0] + bounds[0] >= left
        assert actual[1] + bounds[1] >= top
        assert actual[2] + bounds[0] <= right
        assert actual[3] + bounds[1] <= bottom
        assert image.getpixel((0, 0))[3] == 0


@pytest.mark.parametrize("scale", [0.1, 1.0, 4.0])
def test_distant_coordinates_are_clipped_before_supersampled_allocation(monkeypatch, scale) -> None:
    allocations = []
    original = Image.new

    def allocate(mode, size, *args, **kwargs):
        allocations.append(size)
        assert size[0] <= 256 * scale and size[1] <= 256 * scale
        return original(mode, size, *args, **kwargs)

    monkeypatch.setattr(Image, "new", allocate)
    mark = laser(((-2_000_000_000, 32), (2_000_000_000, 32)))
    with renderer.render_marks(scene(mark), (-32, 0, 32, 64), now=1.5, scale=scale) as image:
        assert image.size == (math.ceil(64 * scale), math.ceil(64 * scale))
        assert image.getbbox() is not None
    assert 3 <= len(allocations) <= 5


def test_off_canvas_laser_does_not_allocate_a_glow_patch(monkeypatch) -> None:
    allocations = []
    original = Image.new

    def allocate(mode, size, *args, **kwargs):
        allocations.append(size)
        return original(mode, size, *args, **kwargs)

    monkeypatch.setattr(Image, "new", allocate)
    mark = laser(((2_000_000_000, -2_000_000_000),))
    with renderer.render_marks(scene(mark), (0, 0, 64, 64), now=1) as image:
        assert image.getbbox() is None
    assert allocations == [(64, 64)]


def test_large_laser_only_canvas_supersamples_only_the_local_patch(monkeypatch) -> None:
    allocations = []
    original = Image.new

    def allocate(mode, size, *args, **kwargs):
        allocations.append((mode, size))
        return original(mode, size, *args, **kwargs)

    monkeypatch.setattr(Image, "new", allocate)
    mark = laser(((100, 100),))
    with renderer.render_marks(scene(mark), (0, 0, 1024, 768), now=1) as image:
        assert image.size == (1024, 768)
    assert allocations[0] == ("RGBA", (1024, 768))
    assert all(math.prod(size) <= 128 * 128 for _, size in allocations[1:])
    assert ("L", (50, 50)) in allocations
    assert ("RGBA", (100, 100)) in allocations


def test_huge_ellipse_is_bounded_and_does_not_rasterize_off_canvas(monkeypatch) -> None:
    def unexpected(*args, **kwargs):
        pytest.fail("A distant ellipse has no local pixels to stroke.")

    monkeypatch.setattr(renderer, "_stroke", unexpected)
    mark = laser(bounds=(-2_000_000_000, -2_000_000_000, 2_000_000_000, 2_000_000_000))
    assert len(renderer._laser_path(mark.points, mark.laser_bounds).points) <= MAX_POINTS
    with renderer.render_marks(scene(mark), (-32, -32, 32, 32), now=1) as image:
        assert image.getbbox() is None


def test_supersampling_obeys_both_dimension_and_pixel_budgets(monkeypatch) -> None:
    monkeypatch.setattr(renderer, "MAX_RENDER_DIMENSION", 128)
    monkeypatch.setattr(renderer, "MAX_RENDER_PIXELS", 8192)
    allocations = []
    original = Image.new

    def allocate(mode, size, *args, **kwargs):
        allocations.append(size)
        assert max(size) <= 128 and math.prod(size) <= 8192
        return original(mode, size, *args, **kwargs)

    monkeypatch.setattr(Image, "new", allocate)
    mark = laser(((0, 0),), width=32)
    with renderer.render_marks(scene(mark), (-32, -32, 32, 32), now=1) as image:
        assert image.size == (64, 64)
    assert len(allocations) >= 3
    assert renderer._sampling((128, 1), 4) == 1


@pytest.mark.parametrize(
    "mark",
    [
        laser(duration=10.01),
        laser(points=((0, 0),) * (MAX_POINTS + 1)),
        laser(bounds=(0, 0, 0, 10)),
        replace(laser(bounds=(0, 0, 10, 10)), kind="path"),
    ],
)
def test_invalid_laser_geometry_is_rejected_before_image_allocation(monkeypatch, mark) -> None:
    def unexpected(*args, **kwargs):
        pytest.fail("Invalid laser metadata must not allocate images.")

    monkeypatch.setattr(Image, "new", unexpected)
    with pytest.raises(ValueError):
        renderer.render_marks(scene(mark), (0, 0, 64, 64), now=0.5)


def test_geometry_cache_is_bounded_and_cannot_extend_visibility() -> None:
    for index in range(MAX_MARKS + 1):
        renderer._laser_path(((index, 10),))
    assert renderer._laser_path.cache_info().currsize <= MAX_MARKS
    mark = laser(bounds=(0, 0, 20, 20), duration=0.1)
    renderer._laser_frame(mark, 0.05)
    assert renderer.visible_bounds(scene(mark), now=0.1) is None
    with renderer.render_marks(scene(), (-20, -20, 40, 40), now=0.05) as cleared:
        assert cleared.getbbox() is None


@pytest.mark.parametrize("fail", [None, "stroke", "blur", "resize"])
@pytest.mark.parametrize("mixed", [False, True])
def test_rendered_output_is_caller_owned_and_work_images_close_on_success_or_failure(
    monkeypatch, fail, mixed
) -> None:
    work = []
    for owner, name in ((Image, "new"), (Image.Image, "filter"), (Image.Image, "resize")):
        original = getattr(owner, name)

        def track(*args, _original=original, **kwargs):
            image = _original(*args, **kwargs)
            if image.mode in ("RGBA", "L"):
                work.append(image)
            return image

        monkeypatch.setattr(owner, name, track)
    mark = laser(bounds=(0, 0, 100, 60))
    marks = [mark]
    if mixed:
        marks.append(replace(laser(), kind="path", expires_at=None))
    snapshot = scene(*marks)
    returned = None
    if fail:

        def fail_raster(*args, **kwargs):
            raise RuntimeError("Synthetic raster failure")

        if fail == "stroke":
            monkeypatch.setattr(renderer, "_stroke", fail_raster)
        else:
            monkeypatch.setattr(Image.Image, "filter" if fail == "blur" else "resize", fail_raster)
        with pytest.raises(RuntimeError, match="Synthetic raster failure"):
            renderer.render_marks(snapshot, (-20, -20, 120, 80), now=0.5)
    else:
        returned = renderer.render_marks(snapshot, (-20, -20, 120, 80), now=0.5)
        assert returned.mode == "RGBA" and returned.size == (140, 100)
        assert returned.getbbox() is not None
    try:
        assert len(work) >= 3
        for image in work:
            if image is not returned:
                with pytest.raises(ValueError, match="closed image"):
                    image.getbbox()
    finally:
        if returned is not None:
            returned.close()
