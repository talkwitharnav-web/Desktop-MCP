"""Synthetic-only observation tests: no screen capture, input, or image files."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from io import BytesIO
import math
from random import Random
from types import SimpleNamespace

from PIL import Image
import pytest

from desktop_mcp.contracts import CaptureContext, CaptureScope, Observation, RawCapture, Rect
from desktop_mcp import vision
from desktop_mcp.vision import CaptureError, StaleFrameError, VisionService


class Stopped(RuntimeError):
    pass


class Clock:
    def __init__(self) -> None:
        self.value = 100.0
        self.waits: list[float] = []

    def __call__(self) -> float:
        return self.value

    def wait(self, seconds: float) -> None:
        self.waits.append(seconds)
        self.value += seconds


class Provider:
    def __init__(self, clock: Clock) -> None:
        self.clock = clock
        desktop = (-500, -300, 700, 500)
        self.current = CaptureContext(
            17, (10, 20, 210, 140), desktop, "Synthetic fixture", (desktop,)
        )
        self.calls: list[tuple[CaptureScope, Rect | None]] = []
        self.context_calls: list[CaptureScope] = []
        self.on_capture: Callable[[], None] | None = None
        self.on_context: Callable[[], None] | None = None
        self.render: Callable[[Rect, int], Image.Image] = lambda bounds, count: Image.new(
            "RGB", (bounds[2] - bounds[0], bounds[3] - bounds[1]), (36, 40, 44)
        )

    def context(self, scope: CaptureScope = "active") -> CaptureContext:
        self.context_calls.append(scope)
        if self.on_context is not None:
            self.on_context()
        return (
            self.current
            if scope == "active"
            else replace(self.current, bounds=self.current.desktop_bounds, scope=scope)
        )

    def capture(self, *, scope: CaptureScope = "active", region: Rect | None = None) -> RawCapture:
        context = (
            self.current
            if scope == "active"
            else replace(self.current, bounds=self.current.desktop_bounds, scope=scope)
        )
        bounds = region if region is not None else context.bounds
        self.calls.append((scope, region))
        if self.on_capture is not None:
            self.on_capture()
        image = self.render(bounds, len(self.calls))
        return RawCapture(image, bounds, context, self.clock())


class Rig:
    def __init__(self) -> None:
        self.clock = Clock()
        self.provider = Provider(self.clock)
        self.revision = 0
        self.stopped = False
        self.checkpoints = 0
        self.after_wait: Callable[[], None] | None = None
        self.vision = self.service()

    def checkpoint(self) -> None:
        self.checkpoints += 1
        if self.stopped:
            raise Stopped("Synthetic stop")

    def wait(self, seconds: float) -> None:
        self.clock.wait(seconds)
        if self.after_wait is not None:
            self.after_wait()

    def service(self, **kwargs: object) -> VisionService:
        return VisionService(
            self.provider,
            revision=lambda: self.revision,
            checkpoint=self.checkpoint,
            wait=self.wait,
            clock=self.clock,
            **kwargs,
        )


@pytest.fixture
def rig() -> Rig:
    return Rig()


def decoded(observation: Observation) -> Image.Image:
    assert isinstance(observation.image, bytes)
    with BytesIO(observation.image) as buffer, Image.open(buffer) as image:
        image.load()
        return image.copy()


def noise(size: tuple[int, int], seed: int = 314) -> Image.Image:
    return Image.frombytes("RGB", size, Random(seed).randbytes(size[0] * size[1] * 3))


def test_single_capture_returns_real_pixels_and_explicit_metadata(rig: Rig, caplog) -> None:
    observation = rig.vision.observe(settle=0)
    with decoded(observation) as image:
        assert image.size == (200, 120)
        assert image.getpixel((10, 10)) == (36, 40, 44)
    metadata = observation.metadata
    assert observation.image.startswith(b"\x89PNG\r\n\x1a\n")
    assert observation.mime_type == "image/png"
    assert metadata["frame_id"] == observation.frame_id
    assert metadata["image_frame_id"] == observation.frame_id
    assert metadata["image_changed"] is True
    assert metadata["pixels_changed"] is None
    assert metadata["input_revision"] == 0
    assert metadata["scope"] == "active"
    assert metadata["window_id"] == 17
    assert metadata["title"] == "Synthetic fixture"
    assert metadata["context_bounds"] == metadata["capture_bounds"] == [10, 20, 210, 140]
    assert metadata["original_dimensions"] == metadata["image_dimensions"] == [200, 120]
    assert metadata["scale_x"] == metadata["scale_y"] == 1
    assert metadata["encoded_bytes"] == metadata["image_encoded_bytes"] == len(observation.image)
    assert metadata["capture_count"] == 1
    assert metadata["poll_count"] == 0
    assert metadata["encoding_attempts"] == 1
    assert metadata["captured_at"] == rig.clock.value
    assert metadata["settled"] is None
    assert rig.provider.calls == [("active", None)]
    assert rig.clock.waits == []
    assert "Synthetic fixture" not in caplog.text


def test_default_settle_is_brief_and_checks_a_second_sample(rig: Rig) -> None:
    observation = rig.vision.observe()
    assert observation.metadata["settled"] is True
    assert observation.metadata["settle_timed_out"] is False
    assert observation.metadata["capture_count"] == 2
    assert rig.clock.waits == pytest.approx([0.06])
    assert observation.metadata["timings"]["wait_seconds"] == pytest.approx(0.06)


@pytest.mark.parametrize("origin", [-1000.1, 0.1, 1_000_000.1, 1_000_000_000.1])
@pytest.mark.parametrize("settle", [0.0001, 0.03, 0.06, 0.11])
def test_settling_uses_absolute_deadlines_without_float_residual_loops(
    rig: Rig, origin: float, settle: float
) -> None:
    rig.clock.value = origin
    observation = rig.vision.observe(settle=settle)
    assert observation.metadata["settled"] is True
    assert observation.metadata["capture_count"] == 2
    assert len(rig.clock.waits) == 1


def test_crop_offsets_negative_origins_and_independent_rounding(rig: Rig) -> None:
    rig.provider.current = replace(rig.provider.current, bounds=(-450, -200, 600, 400))
    crop = (-301, -99, 200, 200)
    observation = rig.vision.observe(region=crop, max_dimension=128, settle=0)
    with decoded(observation) as image:
        assert image.size == (128, 76)
    assert rig.provider.calls == [("active", crop)]
    metadata = observation.metadata
    assert metadata["capture_bounds"] == list(crop)
    assert metadata["context_bounds"] == [-450, -200, 600, 400]
    assert metadata["original_dimensions"] == [501, 299]
    assert metadata["scale_x"] == 501 / 128
    assert metadata["scale_y"] == 299 / 76
    assert metadata["scale_x"] != metadata["scale_y"]
    assert rig.vision.resolve(observation.frame_id, (0, 0)) == (-301, -99)
    assert rig.vision.resolve(observation.frame_id, (64, 38)) == (-51, 50)
    assert rig.vision.resolve(observation.frame_id, (127, 75)) == (
        -301 + 127 * 501 // 128,
        -99 + 75 * 299 // 76,
    )
    assert rig.vision.context_for(observation.frame_id).bounds == (-450, -200, 600, 400)


def test_actual_capture_bounds_not_requested_region_drive_the_transform(
    rig: Rig, monkeypatch
) -> None:
    actual = (-50, -40, 50, 40)
    context = rig.provider.current
    monkeypatch.setattr(
        rig.provider,
        "capture",
        lambda **kwargs: RawCapture(Image.new("RGB", (100, 80)), actual, context, rig.clock()),
    )
    observation = rig.vision.observe(region=(-70, -60, 70, 60), max_dimension=50, settle=0)
    assert observation.metadata["capture_bounds"] == list(actual)
    assert observation.metadata["requested_region"] == [-70, -60, 70, 60]
    assert rig.vision.resolve(observation.frame_id, (25, 20)) == (0, 0)


def test_desktop_scope_is_preserved_when_checking_action_context(rig: Rig) -> None:
    observation = rig.vision.observe(scope="desktop", max_dimension=200, settle=0)
    rig.provider.context_calls.clear()
    context = rig.vision.context_for(observation.frame_id)
    assert context.scope == "desktop"
    assert context.bounds == rig.provider.current.desktop_bounds
    assert rig.vision.resolve(observation.frame_id, (0, 0)) == (-500, -300)
    assert rig.provider.context_calls == ["desktop", "desktop"]


def test_fullscreen_active_and_desktop_contexts_remain_distinct(rig: Rig) -> None:
    from desktop_mcp.capture import context_identity

    rig.provider.current = replace(rig.provider.current, bounds=rig.provider.current.desktop_bounds)
    active = rig.vision.observe(settle=0)
    desktop = rig.vision.observe(scope="desktop", since=active.frame_id, settle=0)
    active_context = rig.vision.context_for(active.frame_id)
    desktop_context = rig.vision.context_for(desktop.frame_id)
    assert active_context.bounds == desktop_context.bounds
    assert active_context.scope == "active"
    assert desktop_context.scope == "desktop"
    assert context_identity(active_context) != context_identity(desktop_context)
    assert desktop.image is not None


@pytest.mark.parametrize("scope", ["active", "desktop"])
def test_windows_capture_context_propagates_scope_without_capturing(scope) -> None:
    from desktop_mcp.capture import WindowsCapture

    provider = WindowsCapture.__new__(WindowsCapture)
    rectangle = SimpleNamespace(left=0, top=0, right=100, bottom=100)
    provider._uia = SimpleNamespace(
        GetVirtualScreenRect=lambda: (0, 0, 100, 100),
        GetDisplays=lambda: [SimpleNamespace(rect=rectangle)],
    )

    def window_rect(handle, pointer):
        for field in ("left", "top", "right", "bottom"):
            setattr(pointer._obj, field, getattr(rectangle, field))
        return True

    provider._user32 = SimpleNamespace(
        GetForegroundWindow=lambda: 17,
        GetWindowTextLengthW=lambda handle: 0,
        GetWindowTextW=lambda *args: 0,
        GetWindowRect=window_rect,
    )
    provider._repair_text = lambda value: value
    provider._control_windows = lambda: ()
    context = provider.context(scope)
    assert context.scope == scope
    assert context.bounds == context.desktop_bounds == (0, 0, 100, 100)


@pytest.mark.parametrize("scope", ["desktop", "invalid", None])
def test_provider_context_with_wrong_or_invalid_scope_is_rejected(rig: Rig, scope) -> None:
    rig.provider.current = replace(rig.provider.current, scope=scope)
    with pytest.raises(CaptureError, match="scope|context"):
        rig.vision.observe(settle=0)
    assert not rig.provider.calls


def test_capture_payload_cannot_substitute_another_scope(rig: Rig, monkeypatch) -> None:
    context = replace(rig.provider.current, scope="desktop")
    monkeypatch.setattr(
        rig.provider,
        "capture",
        lambda **kwargs: RawCapture(
            Image.new("RGB", (200, 120)), context.bounds, context, rig.clock()
        ),
    )
    with pytest.raises(StaleFrameError, match="during capture"):
        rig.vision.observe(settle=0)


def test_mutating_returned_metadata_cannot_change_cached_coordinates(rig: Rig) -> None:
    observation = rig.vision.observe(max_dimension=100, settle=0)
    observation.metadata["capture_bounds"][0] = 999999
    observation.metadata["image_dimensions"][:] = [1, 1]
    observation.metadata["scale_x"] = 999999
    assert rig.vision.resolve(observation.frame_id, (50, 30)) == (110, 80)
    assert rig.vision.context_for(observation.frame_id).bounds == (10, 20, 210, 140)


def test_unchanged_image_omits_encoding_and_refreshes_the_action_reference(
    rig: Rig, monkeypatch
) -> None:
    first = rig.vision.observe(settle=0)
    rig.clock.value += 0.5

    def unexpected_encode(*args: object, **kwargs: object) -> None:
        pytest.fail("An unchanged image must not be re-encoded.")

    monkeypatch.setattr(Image.Image, "save", unexpected_encode)
    second = rig.vision.observe(since=first.frame_id, settle=0)
    assert second.frame_id != first.frame_id
    assert second.image is None
    assert second.mime_type == first.mime_type
    assert second.metadata["image_changed"] is False
    assert second.metadata["pixels_changed"] is False
    assert second.metadata["image_frame_id"] == first.frame_id
    assert second.metadata["reused_from"] == first.frame_id
    assert second.metadata["since_status"] == "valid"
    assert second.metadata["encoded_bytes"] == 0
    assert second.metadata["image_encoded_bytes"] == len(first.image)
    assert second.metadata["encoding_attempts"] == 0
    assert second.metadata["captured_at"] == 100.5
    assert rig.vision.resolve(second.frame_id, (20, 30)) == (30, 50)


@pytest.mark.parametrize("reject_old_actions_first", [False, True])
def test_old_input_revision_can_reuse_pixels_but_never_authorize_input(
    rig: Rig, monkeypatch, reject_old_actions_first: bool
) -> None:
    first = rig.vision.observe(settle=0)
    rig.revision += 1
    rig.clock.value += 0.5
    if reject_old_actions_first:
        with pytest.raises(StaleFrameError, match="Input changed"):
            rig.vision.resolve(first.frame_id, (0, 0))
        with pytest.raises(StaleFrameError, match="Input changed"):
            rig.vision.context_for(first.frame_id)

    def unexpected_encode(*args: object, **kwargs: object) -> None:
        pytest.fail("An input revision alone must not force image encoding.")

    monkeypatch.setattr(Image.Image, "save", unexpected_encode)
    current = rig.vision.observe(since=first.frame_id, settle=0)
    assert current.frame_id != first.frame_id
    assert current.image is None
    assert current.metadata["image_changed"] is False
    assert current.metadata["pixels_changed"] is False
    assert current.metadata["input_revision"] == 1
    assert current.metadata["since_input_revision"] == 0
    assert current.metadata["since_status"] == "valid"
    assert current.metadata["image_frame_id"] == first.frame_id
    assert current.metadata["reused_from"] == first.frame_id
    assert current.metadata["captured_at"] == 100.5
    assert current.metadata["encoding_attempts"] == 0
    assert rig.vision.resolve(current.frame_id, (20, 30)) == (30, 50)
    assert rig.vision.context_for(current.frame_id) == rig.provider.current
    with pytest.raises(StaleFrameError, match="Input changed"):
        rig.vision.resolve(first.frame_id, (0, 0))
    with pytest.raises(StaleFrameError, match="Input changed"):
        rig.vision.context_for(first.frame_id)


def test_omitted_image_aliases_are_bounded_and_survive_root_eviction(rig: Rig) -> None:
    rig.vision = rig.service(max_frames=2)
    first = rig.vision.observe(settle=0)
    second = rig.vision.observe(since=first.frame_id, settle=0)
    third = rig.vision.observe(since=second.frame_id, settle=0)
    assert third.image is None
    assert third.metadata["image_frame_id"] == first.frame_id
    assert third.metadata["reused_from"] == second.frame_id
    assert len(rig.vision._frames) == 2
    with pytest.raises(StaleFrameError, match="evicted"):
        rig.vision.resolve(first.frame_id, (0, 0))
    assert rig.vision.resolve(second.frame_id, (0, 0)) == (10, 20)
    assert rig.vision.resolve(third.frame_id, (0, 0)) == (10, 20)
    fourth = rig.vision.observe(since=third.frame_id, settle=0)
    assert fourth.image is None
    with pytest.raises(StaleFrameError):
        rig.vision.context_for(second.frame_id)
    for frame in rig.vision._frames.values():
        assert len(frame.fingerprint.digest) == 32
        assert not any(isinstance(value, Image.Image) for value in vars(frame).values())


def test_image_aliases_remain_bounded_across_multiple_input_revisions(rig: Rig) -> None:
    rig.vision = rig.service(max_frames=2)
    first = current = rig.vision.observe(settle=0)
    previous_ids: list[str] = []
    for revision in range(1, 6):
        previous_ids.append(current.frame_id)
        rig.revision = revision
        rig.clock.value += 0.1
        current = rig.vision.observe(since=current.frame_id, settle=0)
        assert current.image is None
        assert current.metadata["image_frame_id"] == first.frame_id
        assert current.metadata["input_revision"] == revision
        assert current.metadata["since_input_revision"] == revision - 1
        assert len(rig.vision._frames) == 2
        assert rig.vision.resolve(current.frame_id, (0, 0)) == (10, 20)
        for identifier in previous_ids:
            with pytest.raises(StaleFrameError):
                rig.vision.context_for(identifier)


@pytest.mark.parametrize("revision_changed", [False, True])
def test_full_resolution_single_pixel_change_cannot_hide_in_a_thumbnail(
    rig: Rig, revision_changed: bool
) -> None:
    rig.provider.current = replace(rig.provider.current, bounds=(0, 0, 512, 128))
    source = Image.new("RGB", (512, 128), "white")
    rig.provider.render = lambda bounds, count: source
    first = rig.vision.observe(max_dimension=16, settle=0)
    rig.revision += int(revision_changed)
    source.putpixel((400, 90), (0, 0, 0))
    second = rig.vision.observe(max_dimension=16, since=first.frame_id, settle=0)
    assert second.image is not None
    assert second.metadata["image_changed"] is True
    assert second.metadata["pixels_changed"] is True
    assert source.getpixel((400, 90)) == (0, 0, 0)
    source.close()


@pytest.mark.parametrize("change", ["palette", "transparency", "mode"])
def test_pixel_comparison_includes_palette_transparency_and_mode(rig: Rig, change: str) -> None:
    source = Image.new("P", (200, 120), 0)
    source.putpalette([255, 0, 0, 0, 0, 255] + [0] * 762)
    rig.provider.render = lambda bounds, count: source
    first = rig.vision.observe(settle=0)
    if change == "palette":
        source.putpalette([0, 255, 0, 0, 0, 255] + [0] * 762)
    elif change == "transparency":
        source.info["transparency"] = 0
    else:
        replacement = source.convert("RGB")
        source.close()
        source = replacement
    second = rig.vision.observe(since=first.frame_id, settle=0)
    assert second.image is not None
    assert second.metadata["pixels_changed"] is True
    source.close()


@pytest.mark.parametrize(
    "settings",
    [
        {"encoding": "jpeg"},
        {"encoding": "png"},
        {"quality": 86},
        {"max_dimension": 201},
        {"region": (10, 20, 210, 140)},
        {"region": (20, 30, 200, 130)},
        {"scope": "desktop"},
    ],
)
@pytest.mark.parametrize("revision_changed", [False, True])
def test_changed_output_or_capture_options_never_quietly_reuse_pixels(
    rig: Rig, settings: dict[str, object], revision_changed: bool
) -> None:
    first = rig.vision.observe(settle=0)
    rig.revision += int(revision_changed)
    second = rig.vision.observe(since=first.frame_id, settle=0, **settings)
    assert second.image is not None
    assert second.metadata["image_changed"] is True
    assert second.metadata["reused_from"] is None
    assert second.metadata["image_frame_id"] == second.frame_id
    with decoded(second) as image:
        assert list(image.size) == second.metadata["image_dimensions"]


@pytest.mark.parametrize("method", ["resolve", "context_for"])
def test_input_revision_invalidates_action_references(rig: Rig, method: str) -> None:
    first = rig.vision.observe(settle=0)
    rig.revision += 1
    with pytest.raises(StaleFrameError, match="Input changed"):
        if method == "resolve":
            rig.vision.resolve(first.frame_id, (0, 0))
        else:
            rig.vision.context_for(first.frame_id)


@pytest.mark.parametrize(
    ("change", "status"),
    [
        ("ttl", "expired"),
        ("context", "context_changed"),
        ("eviction", "unknown_or_evicted"),
        ("unknown", "unknown_or_evicted"),
    ],
)
@pytest.mark.parametrize("revision_changed", [False, True])
def test_stale_since_is_a_fresh_capture_not_a_success_shaped_fallback(
    rig: Rig, change: str, status: str, revision_changed: bool
) -> None:
    rig.vision = rig.service(max_frames=1, max_age=1.0)
    first = rig.vision.observe(settle=0)
    rig.revision += int(revision_changed)
    since = first.frame_id
    if change == "ttl":
        rig.clock.value += 1.0
    elif change == "context":
        rig.provider.current = replace(rig.provider.current, window_id=29)
    elif change == "eviction":
        rig.vision.observe(settle=0)
    else:
        since = "not-a-cached-frame"
    current = rig.vision.observe(since=since, settle=0)
    assert current.image is not None
    assert current.metadata["since_status"] == status
    assert current.metadata["image_frame_id"] == current.frame_id
    assert current.metadata["input_revision"] == rig.revision
    with decoded(current) as image:
        assert image.size == (200, 120)


@pytest.mark.parametrize(
    "fields",
    [
        {"window_id": 99},
        {"bounds": (11, 20, 211, 140)},
        {"bounds": (10, 20, 220, 140)},
        {"desktop_bounds": (-501, -300, 700, 500)},
        {"display_bounds": ((-500, -300, 200, 500), (300, -300, 700, 500))},
    ],
)
@pytest.mark.parametrize("method", ["resolve", "context_for"])
def test_changed_window_or_display_geometry_rejects_actions(
    rig: Rig, fields: dict[str, object], method: str
) -> None:
    observation = rig.vision.observe(settle=0)
    rig.provider.current = replace(rig.provider.current, **fields)
    with pytest.raises(StaleFrameError, match="layout changed"):
        if method == "resolve":
            rig.vision.resolve(observation.frame_id, (0, 0))
        else:
            rig.vision.context_for(observation.frame_id)


def test_detected_context_change_cannot_resurrect_an_old_frame(rig: Rig) -> None:
    original_context = rig.provider.current
    observation = rig.vision.observe(settle=0)
    rig.provider.current = replace(original_context, window_id=999)
    with pytest.raises(StaleFrameError):
        rig.vision.context_for(observation.frame_id)
    rig.provider.current = original_context
    with pytest.raises(StaleFrameError, match="unknown"):
        rig.vision.context_for(observation.frame_id)


def test_title_and_monitor_order_changes_do_not_invalidate_geometry(rig: Rig) -> None:
    displays = ((-500, -300, 0, 500), (0, -300, 700, 500))
    rig.provider.current = replace(rig.provider.current, display_bounds=displays)
    first = rig.vision.observe(settle=0)
    rig.provider.current = replace(
        rig.provider.current,
        title="Synthetic title edited",
        display_bounds=tuple(reversed(displays)),
    )
    assert rig.vision.context_for(first.frame_id).title == "Synthetic fixture"
    assert rig.vision.resolve(first.frame_id, (0, 0)) == (10, 20)
    second = rig.vision.observe(since=first.frame_id, settle=0)
    assert second.image is None
    assert second.metadata["title"] == "Synthetic title edited"


@pytest.mark.parametrize("method", ["resolve", "context_for"])
def test_ttl_expires_at_the_exact_boundary(rig: Rig, method: str) -> None:
    rig.vision = rig.service(max_age=0.1)
    observation = rig.vision.observe(settle=0)
    rig.clock.value = observation.metadata["expires_at"]
    with pytest.raises(StaleFrameError, match="expired"):
        if method == "resolve":
            rig.vision.resolve(observation.frame_id, (0, 0))
        else:
            rig.vision.context_for(observation.frame_id)


def test_alias_ttl_uses_its_new_capture_not_the_old_image_timestamp(rig: Rig) -> None:
    rig.vision = rig.service(max_age=1.0)
    first = rig.vision.observe(settle=0)
    rig.clock.value += 0.75
    alias = rig.vision.observe(since=first.frame_id, settle=0)
    rig.clock.value += 0.5
    assert rig.vision.resolve(alias.frame_id, (0, 0)) == (10, 20)
    with pytest.raises(StaleFrameError):
        rig.vision.context_for(first.frame_id)


def test_expiring_since_during_wait_requires_fresh_bytes(rig: Rig) -> None:
    rig.vision = rig.service(max_age=0.1)
    first = rig.vision.observe(settle=0)
    observation = rig.vision.observe(since=first.frame_id, wait_for_change=0.2)
    assert observation.image is not None
    assert observation.metadata["since_status"] == "expired"
    assert observation.metadata["timed_out"] is True
    assert rig.vision.resolve(observation.frame_id, (0, 0)) == (10, 20)


def test_since_expiring_during_delivery_is_reencoded_before_returning(rig: Rig) -> None:
    rig.vision = rig.service(max_age=1.0)
    first = rig.vision.observe(settle=0)
    rig.clock.value += 0.9
    calls = 0

    def context_check() -> None:
        nonlocal calls
        calls += 1
        if calls == 3:
            rig.clock.value += 0.2

    rig.provider.on_context = context_check
    observation = rig.vision.observe(since=first.frame_id, settle=0)
    assert observation.image is not None
    assert observation.metadata["since_status"] == "expired"
    assert observation.metadata["encoding_attempts"] == 1
    assert observation.metadata["capture_count"] == 1
    assert rig.vision.resolve(observation.frame_id, (0, 0)) == (10, 20)


def test_invalidate_clears_all_references_without_provider_or_checkpoint_calls(rig: Rig) -> None:
    first = rig.vision.observe(settle=0)
    alias = rig.vision.observe(since=first.frame_id, settle=0)
    before = len(rig.provider.context_calls), len(rig.provider.calls), rig.checkpoints
    rig.vision.invalidate()
    assert before == (len(rig.provider.context_calls), len(rig.provider.calls), rig.checkpoints)
    assert not rig.vision._frames
    for identifier in (first.frame_id, alias.frame_id):
        with pytest.raises(StaleFrameError):
            rig.vision.context_for(identifier)


@pytest.mark.parametrize("race", ["revision", "invalidation", "window"])
def test_races_during_capture_are_rejected_without_caching_a_frame(rig: Rig, race: str) -> None:
    def interrupt() -> None:
        if race == "revision":
            rig.revision += 1
        elif race == "invalidation":
            rig.vision.invalidate()
        else:
            rig.provider.current = replace(rig.provider.current, window_id=41)

    rig.provider.on_capture = interrupt
    with pytest.raises(StaleFrameError):
        rig.vision.observe(settle=0)
    assert not rig.vision._frames


def test_reusable_old_revision_does_not_allow_a_new_revision_race_during_capture(rig: Rig) -> None:
    first = rig.vision.observe(settle=0)
    rig.revision += 1
    rig.provider.on_capture = lambda: setattr(rig, "revision", rig.revision + 1)
    with pytest.raises(StaleFrameError, match="Input changed during"):
        rig.vision.observe(since=first.frame_id, settle=0)
    assert list(rig.vision._frames) == [first.frame_id]
    with pytest.raises(StaleFrameError, match="Input changed"):
        rig.vision.context_for(first.frame_id)


@pytest.mark.parametrize("race", ["revision", "invalidation", "window", "ttl", "stop"])
def test_races_during_encoding_are_rejected(rig: Rig, monkeypatch, race: str) -> None:
    original = rig.vision._encode

    def encode(*args: object, **kwargs: object) -> tuple:
        result = original(*args, **kwargs)
        if race == "revision":
            rig.revision += 1
        elif race == "invalidation":
            rig.vision.invalidate()
        elif race == "window":
            rig.provider.current = replace(rig.provider.current, window_id=41)
        elif race == "ttl":
            rig.clock.value += 60.0
        else:
            rig.stopped = True
        return result

    monkeypatch.setattr(rig.vision, "_encode", encode)
    with pytest.raises(Stopped if race == "stop" else StaleFrameError):
        rig.vision.observe(settle=0)
    assert not rig.vision._frames


@pytest.mark.parametrize("stage", ["fingerprint", "encoding"])
@pytest.mark.parametrize("error_type", [ValueError, OSError])
def test_checkpoint_exceptions_are_not_wrapped_as_image_failures(
    rig: Rig, monkeypatch, stage: str, error_type: type[Exception]
) -> None:
    cancelled = False
    error = error_type("Synthetic cancellation sentinel")

    def checkpoint() -> None:
        if cancelled:
            raise error

    monkeypatch.setattr(rig.vision, "_checkpoint", checkpoint)
    if stage == "fingerprint":
        original = vision._fingerprint

        def fingerprint(*args: object, **kwargs: object) -> object:
            nonlocal cancelled
            result = original(*args, **kwargs)
            cancelled = True
            return result

        monkeypatch.setattr(vision, "_fingerprint", fingerprint)
    else:
        original_save = Image.Image.save

        def save(*args: object, **kwargs: object) -> None:
            nonlocal cancelled
            original_save(*args, **kwargs)
            cancelled = True

        monkeypatch.setattr(Image.Image, "save", save)
    with pytest.raises(error_type) as result:
        rig.vision.observe(settle=0)
    assert result.value is error
    assert not rig.vision._frames


@pytest.mark.parametrize("race", ["revision", "ttl", "invalidation"])
def test_action_context_validation_rechecks_revision_ttl_and_invalidation(
    rig: Rig, race: str
) -> None:
    observation = rig.vision.observe(settle=0)

    def interrupt() -> None:
        if race == "revision":
            rig.revision += 1
        elif race == "ttl":
            rig.clock.value += 60.0
        else:
            rig.vision.invalidate()

    rig.provider.on_context = interrupt
    with pytest.raises(StaleFrameError):
        rig.vision.resolve(observation.frame_id, (0, 0))


def test_monitor_gaps_reject_image_points_even_inside_virtual_desktop(rig: Rig) -> None:
    rig.provider.current = CaptureContext(
        17,
        (-300, -100, 300, 100),
        (-300, -100, 300, 100),
        "Synthetic monitors",
        ((-300, -100, -100, 100), (100, -100, 300, 100)),
    )
    observation = rig.vision.observe(scope="desktop", max_dimension=300, settle=0)
    assert rig.vision.resolve(observation.frame_id, (0, 0)) == (-300, -100)
    assert rig.vision.resolve(observation.frame_id, (200, 50)) == (100, 0)
    for point in ((100, 50), (150, 50), (199, 50)):
        with pytest.raises(ValueError, match="gap"):
            rig.vision.resolve(observation.frame_id, point)


def test_empty_display_inventory_does_not_invent_a_monitor_gap(rig: Rig) -> None:
    rig.provider.current = replace(rig.provider.current, display_bounds=())
    observation = rig.vision.observe(settle=0)
    assert rig.vision.resolve(observation.frame_id, (0, 0)) == (10, 20)


@pytest.mark.parametrize("encoding", ["png", "jpeg", "auto"])
def test_encoders_bound_bytes_and_dimensions_and_return_decodable_images(
    rig: Rig, monkeypatch, encoding: str
) -> None:
    monkeypatch.setattr(vision, "_MAX_IMAGE_BYTES", 4096)
    source = noise((200, 120))
    rig.provider.render = lambda bounds, count: source
    observation = rig.vision.observe(max_dimension=160, encoding=encoding, settle=0)
    assert len(observation.image) <= 4096
    with decoded(observation) as image:
        width, height = image.size
        assert width <= 160
        assert height <= 96
        assert list(image.size) == observation.metadata["image_dimensions"]
        assert rig.vision.resolve(observation.frame_id, (width - 1, height - 1)) == (
            10 + (width - 1) * 200 // width,
            20 + (height - 1) * 120 // height,
        )
    assert observation.metadata["budget_downscaled"] is True
    assert 1 < observation.metadata["encoding_attempts"] <= vision._MAX_ENCODING_ATTEMPTS
    assert observation.metadata["scale_x"] == 200 / width
    assert observation.metadata["scale_y"] == 120 / height
    assert observation.metadata["encoded_bytes"] == len(observation.image)
    assert observation.mime_type == f"image/{observation.metadata['encoding']}"
    if encoding != "auto":
        assert observation.metadata["encoding"] == encoding
    source.close()


def test_auto_prefers_lossless_flat_ui_and_jpeg_for_high_color_content(rig: Rig) -> None:
    flat = rig.vision.observe(settle=0)
    assert flat.mime_type == "image/png"
    source = noise((200, 120))
    rig.provider.render = lambda bounds, count: source
    textured = rig.vision.observe(settle=0)
    assert textured.mime_type == "image/jpeg"
    assert textured.metadata["quality"] == 85
    source.close()


def test_default_byte_budget_applies_to_full_size_high_entropy_images(rig: Rig) -> None:
    bounds = (0, 0, 1400, 900)
    rig.provider.current = CaptureContext(17, bounds, bounds, display_bounds=(bounds,))
    with noise((1400, 900)) as source:
        rig.provider.render = lambda bounds, count: source
        observation = rig.vision.observe(encoding="png", settle=0)
        assert len(observation.image) <= 750_000
        assert observation.metadata["budget_downscaled"] is True
        assert observation.metadata["encoding"] == "png"
        with decoded(observation) as image:
            assert list(image.size) == observation.metadata["image_dimensions"]


def test_small_sources_are_not_upscaled(rig: Rig) -> None:
    observation = rig.vision.observe(max_dimension=4096, settle=0)
    assert observation.metadata["image_dimensions"] == [200, 120]


def test_transparency_is_preserved_for_png_and_explicitly_flattened_for_jpeg(rig: Rig) -> None:
    source = Image.new("RGBA", (200, 120), (255, 0, 0, 0))
    source.info["private_title"] = "Synthetic private metadata"
    rig.provider.render = lambda bounds, count: source
    png = rig.vision.observe(settle=0)
    assert png.mime_type == "image/png"
    assert png.metadata["alpha_flattened"] is False
    with decoded(png) as image:
        assert image.mode == "RGBA"
        assert image.getpixel((10, 10))[3] == 0
        assert "private_title" not in image.info
    jpeg = rig.vision.observe(encoding="jpeg", settle=0)
    assert jpeg.metadata["alpha_flattened"] is True
    with decoded(jpeg) as image:
        assert image.getpixel((10, 10)) == (255, 255, 255)
    assert source.info["private_title"] == "Synthetic private metadata"
    assert source.getpixel((0, 0)) == (255, 0, 0, 0)
    source.close()


def test_unachievable_byte_budget_fails_without_truncation_or_fallback(
    rig: Rig, monkeypatch
) -> None:
    monkeypatch.setattr(vision, "_MAX_IMAGE_BYTES", 8)
    with pytest.raises(CaptureError, match="byte budget"):
        rig.vision.observe(encoding="png", settle=0)
    assert not rig.vision._frames


def test_encoder_errors_are_explicit(rig: Rig, monkeypatch) -> None:
    def fail(*args: object, **kwargs: object) -> None:
        raise OSError("Synthetic encoder failure")

    monkeypatch.setattr(Image.Image, "save", fail)
    with pytest.raises(CaptureError, match="could not be encoded") as error:
        rig.vision.observe(settle=0)
    assert isinstance(error.value.__cause__, OSError)
    assert not rig.vision._frames


@pytest.mark.parametrize("revision_changed", [False, True])
def test_wait_for_change_backs_off_and_times_out_without_resending_image(
    rig: Rig, revision_changed: bool
) -> None:
    first = rig.vision.observe(settle=0)
    rig.revision += int(revision_changed)
    started = rig.clock.value
    observation = rig.vision.observe(since=first.frame_id, wait_for_change=0.5)
    assert observation.image is None
    assert observation.metadata["wait_status"] == "timeout_no_change"
    assert observation.metadata["timed_out"] is True
    assert observation.metadata["change_detected"] is False
    assert observation.metadata["settled"] is True
    assert rig.clock.value - started == pytest.approx(0.5)
    assert 3 < observation.metadata["capture_count"] < 15
    assert rig.clock.waits[0] < rig.clock.waits[1] < rig.clock.waits[2]
    assert max(rig.clock.waits) <= 0.25
    assert all(delay > 0 for delay in rig.clock.waits)


def test_wait_after_input_detects_pixels_already_changed_since_the_prior_revision(rig: Rig) -> None:
    first = rig.vision.observe(settle=0)
    rig.revision += 1
    rig.provider.render = lambda bounds, count: Image.new("RGB", (200, 120), (120, 100, 80))
    observation = rig.vision.observe(since=first.frame_id, wait_for_change=1.0)
    assert observation.image is not None
    assert observation.metadata["input_revision"] == 1
    assert observation.metadata["since_input_revision"] == 0
    assert observation.metadata["pixels_changed"] is True
    assert observation.metadata["change_detected"] is True
    assert observation.metadata["wait_status"] == "change_detected"
    assert observation.metadata["settled"] is True
    assert rig.clock.value == pytest.approx(100.06)


def test_wait_without_since_uses_the_first_capture_as_baseline(rig: Rig) -> None:
    observation = rig.vision.observe(wait_for_change=0.2)
    assert observation.image is not None
    assert observation.metadata["since_status"] == "not_provided"
    assert observation.metadata["wait_status"] == "timeout_no_change"
    assert rig.clock.value == pytest.approx(100.2)


def test_sampling_accelerates_after_change_and_returns_a_settled_image(rig: Rig) -> None:
    first = rig.vision.observe(settle=0)
    captured: list[float] = []

    def render(bounds: Rect, count: int) -> Image.Image:
        captured.append(rig.clock.value)
        color = (36, 40, 44) if rig.clock.value < 100.12 else (100, 110, 120)
        return Image.new("RGB", (200, 120), color)

    rig.provider.render = render
    observation = rig.vision.observe(since=first.frame_id, wait_for_change=1.0)
    assert observation.image is not None
    assert observation.metadata["wait_status"] == "change_detected"
    assert observation.metadata["settled"] is True
    assert observation.metadata["timed_out"] is False
    assert rig.clock.value < 100.3
    change_index = next(index for index, value in enumerate(captured) if value >= 100.12)
    assert captured[change_index + 1] - captured[change_index] == pytest.approx(0.02)
    assert captured[change_index] - captured[change_index - 1] > 0.02
    with decoded(observation) as image:
        assert image.getpixel((0, 0)) == (100, 110, 120)


def test_transient_change_can_finish_with_the_original_image_reused(rig: Rig) -> None:
    first = rig.vision.observe(settle=0)

    def render(bounds: Rect, count: int) -> Image.Image:
        color = (100, 110, 120) if 100.07 <= rig.clock.value < 100.15 else (36, 40, 44)
        return Image.new("RGB", (200, 120), color)

    rig.provider.render = render
    observation = rig.vision.observe(since=first.frame_id, wait_for_change=0.5)
    assert observation.metadata["change_detected"] is True
    assert observation.metadata["changed_samples"] == 2
    assert observation.metadata["settled"] is True
    assert observation.image is None
    assert observation.metadata["image_changed"] is False
    assert observation.metadata["pixels_changed"] is False
    assert observation.metadata["wait_status"] == "change_detected"


@pytest.mark.parametrize("wait_for_change", [0.0, 1.0])
def test_continuous_animation_cannot_extend_settling_indefinitely(
    rig: Rig, wait_for_change: float
) -> None:
    first = rig.vision.observe(settle=0)
    rig.provider.render = lambda bounds, count: Image.new("RGB", (200, 120), (count, 0, 0))
    started = rig.clock.value
    observation = rig.vision.observe(
        since=first.frame_id, wait_for_change=wait_for_change, settle=0.05
    )
    assert observation.image is not None
    assert observation.metadata["settled"] is False
    assert observation.metadata["settle_timed_out"] is True
    assert observation.metadata["timed_out"] is False
    assert rig.clock.value - started <= 0.2 + 1e-9
    assert 1 < observation.metadata["capture_count"] < 20


def test_change_near_wait_deadline_does_not_extend_the_requested_wait(rig: Rig) -> None:
    first = rig.vision.observe(settle=0)
    rig.provider.render = lambda bounds, count: Image.new(
        "RGB", (200, 120), (36, 40, 44) if rig.clock.value < 100.49 else (20, 0, 0)
    )
    observation = rig.vision.observe(since=first.frame_id, wait_for_change=0.5, settle=0.1)
    assert rig.clock.value == pytest.approx(100.5)
    assert observation.metadata["change_detected"] is True
    assert observation.metadata["settled"] is False
    assert observation.metadata["settle_timed_out"] is True
    assert observation.image is not None


def test_settle_zero_is_one_capture_even_when_a_wait_is_requested(rig: Rig) -> None:
    observation = rig.vision.observe(wait_for_change=5.0, settle=0)
    assert len(rig.provider.calls) == 1
    assert rig.clock.waits == []
    assert observation.metadata["wait_status"] == "single_capture"
    assert observation.metadata["timed_out"] is False
    assert observation.metadata["settled"] is None


def test_slow_capture_overrun_is_measured_and_does_not_start_more_captures(rig: Rig) -> None:
    def slow() -> None:
        rig.clock.value += 0.8

    rig.provider.on_capture = slow
    observation = rig.vision.observe(wait_for_change=0.2)
    assert observation.metadata["capture_count"] == 1
    assert observation.metadata["timed_out"] is True
    assert observation.metadata["timings"]["capture_seconds"] == pytest.approx(0.8)
    assert observation.metadata["poll_deadline_overrun_seconds"] == pytest.approx(0.6)
    assert rig.clock.waits == []


def test_oversleeping_wait_does_not_start_a_capture_after_deadline(rig: Rig) -> None:
    rig.after_wait = lambda: setattr(rig.clock, "value", rig.clock.value + 1)
    observation = rig.vision.observe(wait_for_change=0.1)
    assert observation.metadata["capture_count"] == 1
    assert observation.metadata["timed_out"] is True
    assert observation.metadata["settled"] is False
    assert observation.metadata["poll_deadline_overrun_seconds"] == pytest.approx(0.92)


def test_slow_poll_context_check_cannot_start_capture_after_deadline(rig: Rig) -> None:
    calls = 0

    def context_check() -> None:
        nonlocal calls
        calls += 1
        if calls == 3:
            rig.clock.value += 0.5

    rig.provider.on_context = context_check
    observation = rig.vision.observe(wait_for_change=0.1)
    assert observation.metadata["capture_count"] == 1
    assert len(rig.provider.calls) == 1
    assert observation.metadata["timed_out"] is True
    assert observation.metadata["poll_deadline_overrun_seconds"] == pytest.approx(0.42)


def test_stalled_wait_clock_fails_instead_of_busy_polling(rig: Rig, monkeypatch) -> None:
    monkeypatch.setattr(rig.vision, "_wait", lambda seconds: None)
    with pytest.raises(CaptureError, match="did not advance"):
        rig.vision.observe(wait_for_change=0.5)
    assert len(rig.provider.calls) == 1


def test_bad_short_wait_callback_cannot_produce_unbounded_capture_count(
    rig: Rig, monkeypatch
) -> None:
    monkeypatch.setattr(vision, "_MAX_CAPTURES", 4)
    monkeypatch.setattr(
        rig.vision, "_wait", lambda seconds: setattr(rig.clock, "value", rig.clock.value + 1e-6)
    )
    with pytest.raises(CaptureError, match="capture limit"):
        rig.vision.observe(wait_for_change=1.0)
    assert len(rig.provider.calls) == 4


def test_stop_aware_wait_cancels_before_the_next_capture(rig: Rig) -> None:
    rig.after_wait = lambda: setattr(rig, "stopped", True)
    with pytest.raises(Stopped, match="Synthetic stop"):
        rig.vision.observe(wait_for_change=1.0)
    assert len(rig.provider.calls) == 1
    assert not rig.vision._frames


def test_stopped_service_does_not_access_provider(rig: Rig) -> None:
    rig.stopped = True
    with pytest.raises(Stopped):
        rig.vision.observe()
    assert rig.provider.calls == []
    assert rig.provider.context_calls == []


def test_context_switch_during_wait_rejects_instead_of_capturing_another_app(rig: Rig) -> None:
    rig.after_wait = lambda: setattr(
        rig.provider, "current", replace(rig.provider.current, window_id=777)
    )
    with pytest.raises(StaleFrameError, match="while waiting"):
        rig.vision.observe(wait_for_change=1.0)
    assert len(rig.provider.calls) == 1


def test_backend_failure_is_not_reported_as_a_no_change_timeout(rig: Rig) -> None:
    def fail() -> None:
        raise OSError("Synthetic capture unavailable")

    rig.provider.on_capture = fail
    with pytest.raises(OSError, match="capture unavailable"):
        rig.vision.observe(wait_for_change=0.5)
    assert rig.clock.waits == []
    assert not rig.vision._frames


def test_context_failure_propagates_without_capture(rig: Rig) -> None:
    def fail() -> None:
        raise RuntimeError("Synthetic context unavailable")

    rig.provider.on_context = fail
    with pytest.raises(RuntimeError, match="context unavailable"):
        rig.vision.observe(settle=0)
    assert rig.provider.calls == []


@pytest.mark.parametrize(
    "arguments",
    [
        {"scope": "window"},
        {"scope": True},
        {"scope": ["active"]},
        {"encoding": "PNG"},
        {"encoding": None},
        {"encoding": True},
        {"quality": 0},
        {"quality": 101},
        {"quality": True},
        {"quality": 85.0},
        {"quality": math.nan},
        {"max_dimension": 0},
        {"max_dimension": -1},
        {"max_dimension": 4097},
        {"max_dimension": True},
        {"max_dimension": 100.0},
        {"max_dimension": math.nan},
        {"region": ()},
        {"region": (0, 0, 1)},
        {"region": (0, 0, 1, 1, 2)},
        {"region": (False, 0, 1, 1)},
        {"region": (0, 0, math.nan, 1)},
        {"region": (0, 0, 1.0, 1)},
        {"region": (0, 0, 0, 1)},
        {"region": (0, 1, 1, 0)},
        {"region": "0,0,1,1"},
        {"since": ""},
        {"since": "   "},
        {"since": True},
        {"since": ["frame"]},
        {"wait_for_change": -0.1},
        {"wait_for_change": 5.001},
        {"wait_for_change": math.nan},
        {"wait_for_change": math.inf},
        {"wait_for_change": 10**1000},
        {"wait_for_change": True},
        {"wait_for_change": "1"},
        {"settle": -0.1},
        {"settle": 1.001},
        {"settle": math.nan},
        {"settle": math.inf},
        {"settle": True},
    ],
)
def test_invalid_observation_arguments_fail_before_provider_access(
    rig: Rig, arguments: dict[str, object]
) -> None:
    with pytest.raises(ValueError):
        rig.vision.observe(**arguments)
    assert rig.provider.calls == []
    assert rig.provider.context_calls == []
    assert rig.checkpoints == 0


@pytest.mark.parametrize(
    "point",
    [
        None,
        (),
        (0,),
        (0, 0, 0),
        (True, 0),
        (0, False),
        (0.0, 0),
        (math.nan, 0),
        (0, math.inf),
        {"x": 0, "y": 0},
        "0,0",
        (-1, 0),
        (0, -1),
        (200, 0),
        (0, 120),
    ],
)
def test_invalid_or_out_of_image_points_are_rejected(rig: Rig, point: object) -> None:
    observation = rig.vision.observe(settle=0)
    with pytest.raises(ValueError):
        rig.vision.resolve(observation.frame_id, point)


@pytest.mark.parametrize("identifier", [None, True, 1, "", " ", [], "x" * 129])
def test_malformed_action_frame_ids_are_rejected(rig: Rig, identifier: object) -> None:
    with pytest.raises(ValueError):
        rig.vision.context_for(identifier)
    with pytest.raises(ValueError):
        rig.vision.resolve(identifier, (0, 0))
    assert rig.provider.context_calls == []


@pytest.mark.parametrize(
    "arguments",
    [
        {"max_frames": 0},
        {"max_frames": -1},
        {"max_frames": True},
        {"max_frames": 2.0},
        {"max_frames": 257},
        {"max_age": 0},
        {"max_age": -1},
        {"max_age": math.inf},
        {"max_age": math.nan},
        {"max_age": 10**1000},
        {"max_age": True},
    ],
)
def test_invalid_cache_limits_are_rejected(rig: Rig, arguments: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        rig.service(**arguments)


@pytest.mark.parametrize("callback", ["revision", "checkpoint", "wait", "clock"])
def test_callbacks_must_be_callable(rig: Rig, callback: str) -> None:
    kwargs = {
        "revision": lambda: 0,
        "checkpoint": lambda: None,
        "wait": lambda seconds: None,
        "clock": lambda: 100.0,
    }
    kwargs[callback] = None
    with pytest.raises(ValueError, match="callable"):
        VisionService(rig.provider, **kwargs)


@pytest.mark.parametrize(
    "fields",
    [
        {"window_id": True},
        {"window_id": -1},
        {"bounds": (0, 0, 0, 1)},
        {"desktop_bounds": (0, 0, True, 1)},
        {"display_bounds": None},
        {"display_bounds": ((-999, 0, 0, 100),)},
        {"title": None},
    ],
)
def test_malformed_provider_context_is_an_explicit_error(
    rig: Rig, fields: dict[str, object]
) -> None:
    rig.provider.current = replace(rig.provider.current, **fields)
    with pytest.raises(CaptureError):
        rig.vision.observe(settle=0)
    assert rig.provider.calls == []


@pytest.mark.parametrize(
    "fields",
    [
        {"image": None},
        {"image": Image.new("RGB", (3, 3))},
        {"bounds": (0, 0, 0, 1)},
        {"bounds": (-1000, -1000, -800, -880)},
        {"captured_at": math.nan},
        {"captured_at": math.inf},
        {"captured_at": True},
        {"captured_at": 101.0},
        {"context": None},
    ],
)
def test_malformed_raw_captures_do_not_return_images(
    rig: Rig, monkeypatch, fields: dict[str, object]
) -> None:
    original = rig.provider.capture
    monkeypatch.setattr(
        rig.provider,
        "capture",
        lambda **kwargs: replace(original(**kwargs), **fields),
    )
    with pytest.raises(CaptureError):
        rig.vision.observe(settle=0)
    assert not rig.vision._frames


def test_already_expired_provider_capture_is_not_delivered(rig: Rig, monkeypatch) -> None:
    original = rig.provider.capture
    monkeypatch.setattr(
        rig.provider, "capture", lambda **kwargs: replace(original(**kwargs), captured_at=1.0)
    )
    with pytest.raises(StaleFrameError, match="expired"):
        rig.vision.observe(settle=0)
    assert not rig.vision._frames


def test_closed_provider_image_is_an_explicit_capture_error(rig: Rig) -> None:
    source = Image.new("RGB", (200, 120))
    source.close()
    rig.provider.render = lambda bounds, count: source
    with pytest.raises(CaptureError, match="unreadable"):
        rig.vision.observe(settle=0)


@pytest.mark.parametrize("value", [True, math.nan, math.inf])
def test_clock_must_be_finite_and_numeric(rig: Rig, value: object) -> None:
    rig.clock.value = value
    with pytest.raises(CaptureError, match="clock"):
        rig.vision.observe(settle=0)
    assert rig.provider.calls == []


def test_clock_cannot_go_backwards(rig: Rig) -> None:
    rig.vision.observe(settle=0)
    rig.clock.value -= 0.1
    with pytest.raises(CaptureError, match="monotonic"):
        rig.vision.observe(settle=0)


@pytest.mark.parametrize("value", [True, -1, 0.0, math.nan])
def test_invalid_revision_callback_is_explicit(rig: Rig, value: object) -> None:
    rig.revision = value
    with pytest.raises(RuntimeError, match="revision callback"):
        rig.vision.observe(settle=0)
    assert rig.provider.calls == []
