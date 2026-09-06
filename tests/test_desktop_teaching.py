"""Teaching tests use synthetic geometry, cursor positions, clocks, and images."""

from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError, replace
import math
import threading

from PIL import Image
import pytest

from desktop_mcp.contracts import CaptureContext, Point
from desktop_mcp.runtime import Controller, DesktopStopped
from desktop_mcp.conversation import MAX_ENTRIES
from desktop_mcp import teaching
from desktop_mcp.teaching import (
    MAX_MARKS,
    Mark,
    TeachingSession,
    TeachingSnapshot,
    WaitTarget,
)
from desktop_mcp.teaching_render import render_marks, visible_bounds


class Clock:
    def __init__(self) -> None:
        self.value = 100.0

    def __call__(self) -> float:
        return self.value


class Backend:
    def release_pending(self) -> None:
        pass

    def __getattr__(self, name: str) -> object:
        raise AssertionError(f"Teaching must not use backend input: {name}")


class SyntheticController(Controller):
    def __init__(self, clock: Clock) -> None:
        super().__init__(Backend(), clock=clock)
        self.clock = clock
        self.waits: list[float] = []
        self.after_wait: Callable[[], None] | None = None

    def wait(self, duration: float) -> None:
        self.checkpoint()
        self.waits.append(duration)
        self.clock.value += duration
        if self.after_wait is not None:
            self.after_wait()
        self.checkpoint()


class Rig:
    def __init__(self) -> None:
        self.clock = Clock()
        self.controller = SyntheticController(self.clock)
        self.controller.set_interface_ready(True)
        self.controller.arm_local()
        desktop = (-400, -200, 400, 300)
        self.current: CaptureContext | None = CaptureContext(
            7, (-120, -80, 300, 200), desktop, "Synthetic lesson", (desktop,)
        )
        self.cursor: Point = (-100, 0)
        self.position_calls = 0
        self.context_calls: list[CaptureContext | None] = []
        self.context_hook: Callable[[], None] | None = None
        self.position_hook: Callable[[], None] | None = None
        self.session = TeachingSession(
            self.controller, position=self.position, context=self.context, clock=self.clock
        )

    def position(self) -> Point:
        self.position_calls += 1
        if self.position_hook is not None:
            self.position_hook()
        return self.cursor

    def context(self, expected: CaptureContext | None) -> CaptureContext | None:
        self.context_calls.append(expected)
        if self.context_hook is not None:
            self.context_hook()
        return self.current

    def call(self, name: str, *args: object, **kwargs: object) -> object:
        with self.controller.operation("Synthetic teaching"):
            return getattr(self.session, name)(*args, **kwargs)

    def draw(self, **kwargs: object) -> str:
        return self.call("draw", "path", [(0, 0), (40, 20)], **kwargs)


@pytest.fixture
def rig() -> Rig:
    return Rig()


def scene(*marks: Mark, waiting: WaitTarget | None = None) -> TeachingSnapshot:
    return TeachingSnapshot(1, (), marks, waiting, None)


def mark(
    kind: str = "path",
    points: tuple[Point, ...] = ((10, 20), (60, 20)),
    *,
    width: float = 4.0,
    expires: float | None = None,
) -> Mark:
    return Mark("synthetic-ink", kind, points, "#ffb454", width, 0.0, expires, None)


def test_transcript_is_explicit_bounded_immutable_and_readable_when_stopped(rig: Rig) -> None:
    for index in range(MAX_ENTRIES + 2):
        entry = rig.call("publish", f"Step {index}: 日本語 🖱️", title="Lesson")
    snapshot = rig.session.snapshot()
    assert len(snapshot.entries) == MAX_ENTRIES
    assert snapshot.entries[0].sequence == 3
    assert snapshot.entries[-1] == entry
    with pytest.raises(FrozenInstanceError):
        entry.text = "replace"
    rig.controller.stop()
    assert rig.session.snapshot().entries == snapshot.entries
    rig.session.publish("Chat still works while desktop control is paused.")
    rig.session.clear_transcript_local()
    assert rig.session.snapshot().entries == ()
    assert rig.position_calls == 0
    assert rig.context_calls == []


def test_closed_app_cannot_publish_more_messages(rig: Rig) -> None:
    first = rig.call("publish", "Previously accepted instruction")
    rig.controller.close()
    with pytest.raises(RuntimeError, match="closed"):
        rig.session.publish("After close")
    assert rig.session.snapshot().entries == (first,)


def test_text_validation_does_not_echo_content_or_accept_invalid_unicode(rig: Rig) -> None:
    for text in ("", "\ud800", "private\x00content", "x" * 16_001):
        with pytest.raises(ValueError) as error:
            rig.call("publish", text)
        assert "private" not in str(error.value)
    with pytest.raises(ValueError):
        rig.call("publish", "valid", title="multi\nline")
    assert rig.session.snapshot().entries == ()


def test_annotations_require_an_operation_but_text_and_ui_reads_do_not(rig: Rig) -> None:
    with pytest.raises(RuntimeError, match="active controller operation"):
        rig.session.draw("path", [(0, 0), (20, 20)])
    assert rig.session.snapshot().marks == ()
    rig.session.clear_local()
    rig.session.clear_transcript_local()
    assert rig.call("publish", "Guidance in the same session").sequence == 1
    assert rig.draw()
    assert rig.call("wait_for_cursor", (0, 0), timeout=0)["status"] == "timeout"
    assert not rig.controller.snapshot().awaiting_user


@pytest.mark.parametrize("method", ["draw", "wait_for_cursor"])
def test_mapping_revision_cannot_be_rebased_after_physical_input(rig: Rig, method) -> None:
    revision = rig.controller.input_revision
    rig.controller.notify_human_input(kind="button")
    arguments = ("path", [(0, 0), (40, 20)]) if method == "draw" else ((0, 0),)
    with pytest.raises(RuntimeError, match="Input changed"):
        rig.call(method, *arguments, expected_input_revision=revision)
    assert not rig.session.snapshot().marks
    assert rig.session.snapshot().waiting is None
    assert rig.controller.snapshot().armed


@pytest.mark.parametrize("combined", [False, True])
def test_oversized_desktop_scenes_are_rejected_before_mark_commit(rig: Rig, combined) -> None:
    desktop = (0, 0, 11520, 4320)
    rig.current = CaptureContext(7, desktop, desktop, display_bounds=(desktop,), scope="desktop")
    if combined:
        rig.call("draw", "path", [(100, 100), (120, 100)])
    before = rig.session.snapshot().marks
    points = [(9000, 100), (9020, 100)] if combined else [(100, 100), (9000, 100)]
    with pytest.raises(ValueError, match="renderer size"):
        rig.call("draw", "path", points)
    assert rig.session.snapshot().marks == before
    assert rig.controller.snapshot().armed


def test_combined_wait_ring_and_ink_obey_canvas_limits(rig: Rig) -> None:
    desktop = (0, 0, 11520, 4320)
    rig.current = CaptureContext(7, desktop, desktop, display_bounds=(desktop,), scope="desktop")
    rig.call("draw", "path", [(100, 100), (120, 100)])
    rig.cursor = (9000, 100)
    with pytest.raises(ValueError, match="renderer size"):
        rig.call("wait_for_cursor", rig.cursor, dwell=0)
    assert rig.session.snapshot().waiting is None
    assert len(rig.session.snapshot().marks) == 1
    assert rig.controller.snapshot().armed


def test_combined_scene_pixel_area_is_bounded_before_commit(rig: Rig) -> None:
    desktop = (0, 0, 11520, 4320)
    rig.current = CaptureContext(7, desktop, desktop, display_bounds=(desktop,), scope="desktop")
    with pytest.raises(ValueError, match="renderer size"):
        rig.call("draw", "rectangle", [(100, 100), (6500, 3500)])
    assert not rig.session.snapshot().marks


def test_stop_rearm_across_checkpoint_cannot_stamp_an_old_rpc_with_the_new_generation(
    rig: Rig, monkeypatch
) -> None:
    checkpoint = rig.controller.checkpoint
    original_generation = rig.controller.snapshot().generation

    def stop_after_checkpoint() -> None:
        checkpoint()
        rig.controller.stop()
        rig.controller.arm_local()
        monkeypatch.setattr(rig.controller, "checkpoint", checkpoint)

    with pytest.raises(DesktopStopped):
        with rig.controller.operation("Synthetic generation race"):
            monkeypatch.setattr(rig.controller, "checkpoint", stop_after_checkpoint)
            rig.session.draw("laser", [(0, 0)])
    assert rig.controller.snapshot().generation > original_generation
    assert rig.context_calls == []
    assert rig.session.snapshot().marks == ()


def test_generation_is_rechecked_after_clock_before_annotation_commit(
    rig: Rig, monkeypatch
) -> None:
    calls = 0
    created: list[Mark] = []

    def clock() -> float:
        nonlocal calls
        calls += 1
        if calls == 2:
            rig.controller.stop()
            rig.controller.arm_local()
        return rig.clock.value

    def record_mark(*args: object, **kwargs: object) -> Mark:
        result = Mark(*args, **kwargs)
        created.append(result)
        return result

    monkeypatch.setattr(rig.session, "_clock", clock)
    monkeypatch.setattr(teaching, "Mark", record_mark)
    with pytest.raises(DesktopStopped):
        rig.draw()
    assert created == []
    assert rig.session.snapshot().marks == ()


def test_erase_and_expiry_only_remove_our_annotations(rig: Rig) -> None:
    persistent = rig.draw()
    rig.draw(lifetime=0.1)
    laser = rig.call("draw", "laser", [(20, 20)])
    snapshot = rig.session.snapshot()
    assert len(snapshot.marks) == 3
    assert next(item for item in snapshot.marks if item.identifier == laser).expires_at == 102.0
    rig.clock.value = 100.1
    assert {item.identifier for item in rig.session.snapshot().marks} == {persistent, laser}
    assert rig.call("erase", persistent) == 1
    with pytest.raises(ValueError, match="not found"):
        rig.call("erase", persistent)
    rig.clock.value = 102.0
    assert rig.session.snapshot().marks == ()
    assert rig.call("erase") == 0
    rig.draw()
    rig.draw()
    assert rig.call("erase") == 2


def test_looping_laser_keeps_exact_bounds_until_its_bounded_deadline(rig: Rig) -> None:
    bounds = (-30, -21, 51, 40)
    identifier = rig.call(
        "draw",
        "laser",
        [(-30, -20), (50, 40), (-30, -20)],
        lifetime=10.0,
        laser_bounds=bounds,
    )
    for now in (100.0, 102.0, 107.6, 109.99):
        rig.clock.value = now
        marks = rig.session.snapshot().marks
        assert len(marks) == 1
        assert marks[0].identifier == identifier
        assert marks[0].laser_bounds == bounds
        assert marks[0].expires_at == 110.0
    rig.clock.value = 110.0
    assert rig.session.snapshot().marks == ()
    assert rig.position_calls == 0


@pytest.mark.parametrize("invalidation", ["stop", "context", "input"])
def test_looping_laser_is_cleared_before_deadline_on_invalidation(rig: Rig, invalidation) -> None:
    rig.call("draw", "laser", [(0, 0)], lifetime=10.0, laser_bounds=(-20, -20, 20, 20))
    if invalidation == "stop":
        rig.controller.stop()
        rig.controller.arm_local()
    elif invalidation == "context":
        rig.current = None
    else:
        rig.controller.notify_human_input(kind="button")
    rig.clock.value += 0.2
    assert rig.session.snapshot().marks == ()
    assert rig.position_calls == 0


@pytest.mark.parametrize("bounds", [(0, 0, 0, 1), (-500, 0, 20, 20)])
def test_invalid_laser_bounds_cannot_publish_state(rig: Rig, bounds) -> None:
    with pytest.raises(ValueError):
        rig.call("draw", "laser", [(0, 0)], laser_bounds=bounds)
    assert rig.session.snapshot().marks == ()
    with pytest.raises(ValueError, match="Only a laser"):
        rig.draw(laser_bounds=(-20, -20, 20, 20))


def test_resource_and_shape_limits_are_explicit_without_silent_eviction(rig: Rig) -> None:
    invalid = [
        ("path", [(0, 0)], {}),
        ("rectangle", [(0, 0), (0, 20)], {}),
        ("ellipse", [(0, 0), (20, 20), (30, 30)], {}),
        ("laser", [(0, 0)], {"lifetime": 10.1}),
        ("path", [(0, 0), (1, 1)], {"width": math.nan}),
        ("path", [(0, 0), (1, 1)], {"color": "transparent"}),
        ("path", [(True, 0), (1, 1)], {}),
        ("path", [(0, 0)] * 513, {}),
        ("path", [(0, 0), (400, 0)], {}),
    ]
    for kind, points, kwargs in invalid:
        with pytest.raises(ValueError):
            rig.call("draw", kind, points, **kwargs)
    assert rig.session.snapshot().marks == ()
    identifiers = [rig.draw() for _ in range(MAX_MARKS)]
    with pytest.raises(ValueError, match="erase"):
        rig.draw()
    assert len(rig.session.snapshot().marks) == MAX_MARKS
    rig.call("erase", identifiers[0])
    assert rig.draw()
    assert len(rig.session.snapshot().marks) == MAX_MARKS


def test_physical_motion_preserves_ink_but_buttons_invalidate_anchored_state(rig: Rig) -> None:
    rig.call("publish", "Keep this instruction")
    identifier = rig.draw()
    revision = rig.controller.input_revision
    rig.controller.notify_human_input(kind="move", position=(12, 20))
    snapshot = rig.session.snapshot()
    assert snapshot.cursor == (12, 20)
    assert snapshot.marks[0].identifier == identifier
    assert rig.controller.input_revision == revision
    rig.controller.notify_human_input(kind="button", position=(12, 20))
    count = len(rig.context_calls)
    snapshot = rig.session.snapshot()
    assert snapshot.marks == ()
    assert snapshot.entries
    assert len(rig.context_calls) == count
    assert rig.controller.snapshot().armed


def test_stop_and_rearm_without_an_intermediate_read_cannot_resurrect_ink(rig: Rig) -> None:
    rig.draw()
    rig.controller.stop()
    rig.controller.arm_local()
    assert rig.session.snapshot().marks == ()
    rig.draw()
    rig.controller.stop()
    count = len(rig.context_calls)
    assert rig.session.snapshot().marks == ()
    assert len(rig.context_calls) == count


def test_stopped_ui_reads_do_not_depend_on_clock_or_other_providers(rig: Rig, monkeypatch) -> None:
    entry = rig.call("publish", "Still readable")
    rig.draw()
    rig.controller.stop()

    def unexpected() -> None:
        pytest.fail("A stopped snapshot must not wait on external providers.")

    monkeypatch.setattr(rig.session, "_clock", unexpected)
    rig.position_hook = rig.context_hook = unexpected
    snapshot = rig.session.snapshot()
    assert snapshot.entries == (entry,)
    assert snapshot.marks == ()
    assert snapshot.waiting is None


def test_ui_snapshot_rechecks_generation_after_its_last_clock_read(rig: Rig, monkeypatch) -> None:
    rig.draw()
    calls = 0

    def clock() -> float:
        nonlocal calls
        calls += 1
        if calls == 2:
            rig.controller.stop()
            rig.controller.arm_local()
        return rig.clock.value

    monkeypatch.setattr(rig.session, "_clock", clock)
    assert rig.session.snapshot().marks == ()


def test_context_queries_are_grouped_debounced_and_prune_unavailable_targets(rig: Rig) -> None:
    for _ in range(4):
        rig.draw()
    initial = len(rig.context_calls)
    for _ in range(4):
        assert len(rig.session.snapshot().marks) == 4
    assert len(rig.context_calls) == initial
    rig.current = replace(rig.current, title="Same geometry, new title")
    rig.clock.value += 0.1
    assert len(rig.session.snapshot().marks) == 4
    assert len(rig.context_calls) == initial + 1
    rig.current = None
    rig.clock.value += 0.1
    assert rig.session.snapshot().marks == ()
    with pytest.raises(RuntimeError, match="unavailable"):
        rig.draw()


def test_geometry_change_is_not_mistaken_for_the_original_anchor(rig: Rig) -> None:
    expected = rig.current
    rig.draw(expected_context=expected)
    rig.current = replace(rig.current, bounds=(-100, -80, 320, 200))
    rig.clock.value += 0.1
    assert rig.session.snapshot().marks == ()
    with pytest.raises(RuntimeError, match="changed"):
        rig.draw(expected_context=expected)


@pytest.mark.parametrize("action", ["clear", "stop"])
def test_blocking_context_reads_do_not_hold_the_model_lock_or_resurrect_state(
    rig: Rig, action: str
) -> None:
    rig.draw()
    rig.clock.value += 0.1
    entered, release = threading.Event(), threading.Event()

    def block() -> None:
        entered.set()
        if not release.wait(2):
            raise RuntimeError("Synthetic callback was not released")

    rig.context_hook = block
    with ThreadPoolExecutor(max_workers=2) as pool:
        reading = pool.submit(rig.session.snapshot)
        assert entered.wait(1)
        try:
            if action == "clear":
                pool.submit(rig.session.clear_local).result(timeout=0.5)
            else:
                rig.controller.stop()
                immediate = pool.submit(rig.session.snapshot).result(timeout=0.5)
                assert immediate.marks == ()
        finally:
            release.set()
        assert reading.result(timeout=1).marks == ()


def test_draw_rechecks_revocation_after_external_context_callback(rig: Rig) -> None:
    rig.context_hook = rig.controller.stop
    with pytest.raises(DesktopStopped):
        rig.draw()
    assert rig.session.snapshot().marks == ()


def test_local_clear_cancels_an_annotation_still_being_prepared(rig: Rig) -> None:
    rig.context_hook = rig.session.clear_local
    with pytest.raises(RuntimeError, match="cleared locally"):
        rig.draw()
    assert rig.session.snapshot().marks == ()


def test_cursor_position_reads_the_getter_not_laser_or_controller_motion(rig: Rig) -> None:
    rig.call("draw", "laser", [(100, 100)])
    assert rig.call("cursor_position") == (-100, 0)
    assert rig.position_calls == 1
    assert rig.cursor == (-100, 0)
    rig.session.snapshot()
    assert rig.position_calls == 1


def test_dwell_resets_after_leaving_and_only_reports_cursor_vicinity(rig: Rig) -> None:
    observed: list[WaitTarget | None] = []

    def move() -> None:
        elapsed = round(rig.clock.value - 100.0, 6)
        rig.cursor = (0, 0) if 0.05 <= elapsed < 0.15 or elapsed >= 0.2 else (40, 0)
        rig.controller.notify_human_input(kind="move", position=rig.cursor)
        observed.append(rig.session.snapshot().waiting)

    rig.controller.after_wait = move
    result = rig.call("wait_for_cursor", (0, 0), radius=10, dwell=0.15, timeout=1)
    assert result["status"] == "reached"
    assert result["evidence"] == "cursor_vicinity"
    assert result["cursor"] == [0, 0]
    assert 0.35 - 1e-9 <= result["elapsed"] < 0.45
    assert any(item is not None and 0 < item.dwell_progress < 1 for item in observed)
    assert any(
        item is not None and not item.inside and item.dwell_progress == 0 for item in observed
    )
    assert rig.session.snapshot().waiting is None


def test_cursor_timeout_is_bounded_and_wait_state_clears(rig: Rig) -> None:
    result = rig.call("wait_for_cursor", (0, 0), radius=10, dwell=0.2, timeout=0.21)
    assert result["status"] == "timeout"
    assert result["distance"] == 100.0
    assert result["elapsed"] == pytest.approx(0.21)
    assert sum(rig.controller.waits) == pytest.approx(0.21)
    assert max(rig.controller.waits) <= 0.05
    assert len(rig.context_calls) <= 4
    assert rig.session.snapshot().waiting is None


def test_dwell_can_complete_exactly_at_the_timeout_without_float_roundoff(rig: Rig) -> None:
    rig.cursor = (0, 0)
    result = rig.call("wait_for_cursor", (0, 0), dwell=0.1, timeout=0.1)
    assert result["status"] == "reached"
    assert result["elapsed"] == pytest.approx(0.1)
    assert rig.session.snapshot().waiting is None


def test_zero_dwell_still_validates_context_and_input_before_reached(rig: Rig) -> None:
    rig.cursor = (0, 0)
    expected = rig.current
    rig.current = None
    result = rig.call("wait_for_cursor", (0, 0), dwell=0, expected_context=expected)
    assert result["status"] == "context_changed"
    assert rig.controller.waits == []
    rig.current = expected
    rig.context_hook = lambda: rig.controller.notify_human_input(kind="key")
    result = rig.call("wait_for_cursor", (0, 0), dwell=0)
    assert result["status"] == "input_changed"
    assert rig.session.snapshot().waiting is None


def test_input_revision_is_rechecked_after_publishing_wait_progress(rig: Rig, monkeypatch) -> None:
    rig.cursor = (0, 0)
    set_cursor = rig.session._set_cursor

    def late_input(point: Point) -> None:
        set_cursor(point)
        rig.controller.notify_human_input(kind="key")

    monkeypatch.setattr(rig.session, "_set_cursor", late_input)
    result = rig.call("wait_for_cursor", (0, 0), dwell=0)
    assert result["status"] == "input_changed"
    assert rig.session.snapshot().waiting is None


@pytest.mark.parametrize("change", ["input", "context", "stop", "clear"])
def test_wait_exits_without_advancement_on_invalidated_or_cancelled_targets(
    rig: Rig, change: str
) -> None:
    def interrupt() -> None:
        if change == "input":
            rig.controller.notify_human_input(kind="key")
        elif change == "context":
            rig.current = replace(rig.current, window_id=8)
        elif change == "stop":
            rig.controller.stop()
        else:
            rig.session.clear_local()

    rig.controller.after_wait = interrupt
    if change in ("stop", "clear"):
        with pytest.raises(DesktopStopped if change == "stop" else RuntimeError):
            rig.call("wait_for_cursor", (0, 0), timeout=1)
    else:
        result = rig.call("wait_for_cursor", (0, 0), timeout=1)
        assert result["status"] == ("input_changed" if change == "input" else "context_changed")
    assert rig.session.snapshot().waiting is None


def test_ui_detected_context_loss_stays_invalid_even_if_the_app_returns(rig: Rig) -> None:
    original = rig.current

    def blink() -> None:
        if rig.clock.value >= 100.1:
            rig.current = None
            assert rig.session.snapshot().waiting is None
            rig.current = original

    rig.controller.after_wait = blink
    result = rig.call("wait_for_cursor", (0, 0), timeout=1)
    assert result["status"] == "context_changed"
    assert result["elapsed"] < 0.2


def test_slow_context_lookup_does_not_claim_dwell_using_an_old_cursor_sample(rig: Rig) -> None:
    rig.cursor = (0, 0)

    def slow() -> None:
        rig.clock.value += 0.3
        rig.cursor = (100, 0)

    rig.context_hook = slow
    result = rig.call("wait_for_cursor", (0, 0), radius=10, dwell=0.2, timeout=0.25)
    assert result["status"] == "timeout"
    assert result["cursor"] == [100, 0]
    assert rig.session.snapshot().waiting is None


def test_provider_failures_propagate_and_clear_pending_wait_state(rig: Rig) -> None:
    error = OSError("Synthetic context failure")

    def fail() -> None:
        raise error

    rig.context_hook = fail
    with pytest.raises(OSError) as result:
        rig.call("wait_for_cursor", (0, 0))
    assert result.value is error
    assert rig.session.snapshot().waiting is None
    with pytest.raises(OSError) as result:
        rig.draw()
    assert result.value is error


def test_wait_validation_and_nonadvancing_clock_fail_explicitly(rig: Rig) -> None:
    for kwargs in ({"timeout": 31}, {"radius": math.nan}, {"dwell": True}, {"radius": -1}):
        with pytest.raises(ValueError):
            rig.call("wait_for_cursor", (0, 0), **kwargs)
    assert rig.position_calls == 0
    assert rig.context_calls == []
    rig.controller.after_wait = lambda: setattr(rig.clock, "value", 100.0)
    with pytest.raises(RuntimeError, match="advance"):
        rig.call("wait_for_cursor", (0, 0))
    assert rig.session.snapshot().waiting is None


def test_rendered_ink_is_transparent_outlined_and_has_round_antialiased_endpoints() -> None:
    snapshot = scene(mark(), mark("rectangle", ((15, 40), (60, 70))))
    with render_marks(snapshot, (0, 0, 80, 80), now=0.0) as image:
        assert image.mode == "RGBA"
        assert image.size == (80, 80)
        assert image.getpixel((8, 20))[3] > 0
        assert image.getpixel((30, 20))[3] > 200
        assert image.getpixel((30, 55))[3] == 0
        assert image.getpixel((0, 0))[3] == 0
        assert any(0 < alpha < 255 for alpha in image.getchannel("A").tobytes())


@pytest.mark.parametrize("kind", ["path", "rectangle", "ellipse"])
def test_outline_ink_keeps_its_hue_and_a_contrasting_edge_on_light_surfaces(kind) -> None:
    points = ((10, 20), (60, 20)) if kind == "path" else ((10, 10), (60, 60))
    snapshot = scene(mark(kind, points=points, width=3))

    def luminance(rgb):
        values = [value / 255 for value in rgb[:3]]
        linear = [
            value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4
            for value in values
        ]
        return sum(weight * value for weight, value in zip((0.2126, 0.7152, 0.0722), linear))

    with render_marks(snapshot, (0, 0, 80, 80), now=0) as ink:
        pixels = [ink.getpixel((x, y)) for y in range(ink.height) for x in range(ink.width)]
        assert any(
            alpha > 230
            and max(abs(value - target) for value, target in zip((r, g, b), (255, 180, 84))) < 15
            for r, g, b, alpha in pixels
        )
        with Image.new("RGBA", ink.size, (242, 242, 242, 255)) as composite:
            composite.alpha_composite(ink)
            darkest = min(
                luminance(composite.getpixel((x, y)))
                for y in range(composite.height)
                for x in range(composite.width)
            )
            assert (luminance((242, 242, 242)) + 0.05) / (darkest + 0.05) >= 4.5
        declared = visible_bounds(snapshot, now=0)
        actual = ink.getbbox()
        assert declared[0] <= actual[0] and declared[1] <= actual[1]
        assert declared[2] >= actual[2] and declared[3] >= actual[3]
        assert any(0 < alpha < 255 for alpha in ink.getchannel("A").tobytes())


def test_renderer_handles_negative_origins_ellipses_and_wait_progress() -> None:
    snapshot = scene(
        mark("ellipse", ((-20, -10), (20, 10))),
        waiting=WaitTarget((40, 0), 10.0, True, 0.5, 0.25),
    )
    with render_marks(snapshot, (-40, -30, 70, 30), now=0.0, scale=2) as image:
        assert image.size == (220, 120)
        assert image.getpixel((80, 60))[3] == 0
        assert image.getpixel((120, 60))[3] > 0
        assert image.getpixel((160, 40))[3] > 0


def test_laser_animates_and_fades_without_drawing_an_opaque_surface() -> None:
    snapshot = scene(mark("laser", ((10, 20), (80, 20)), expires=2.0))
    with (
        render_marks(snapshot, (0, 0, 100, 40), now=0.0) as first,
        render_marks(snapshot, (0, 0, 100, 40), now=0.6) as moving,
        render_marks(snapshot, (0, 0, 100, 40), now=1.9) as fading,
        render_marks(snapshot, (0, 0, 100, 40), now=2.0) as expired,
    ):
        assert first.getbbox() is None
        assert moving.getpixel((45, 20))[3] > 200
        assert first.getpixel((90, 39))[3] == 0
        assert fading.getchannel("A").getextrema()[1] < moving.getchannel("A").getextrema()[1]
        assert expired.getbbox() is None


def test_renderer_clips_distant_segments_without_giant_allocations() -> None:
    snapshot = scene(
        mark("ellipse", ((-1_000_000, -1_000_000), (1_000_000, 1_000_000))),
        mark(points=((-1_000_000, 16), (1_000_000, 16))),
    )
    with render_marks(snapshot, (0, 0, 32, 32), now=0.0) as image:
        assert image.getpixel((16, 16))[3] > 200
        assert image.getpixel((16, 0))[3] == 0


def test_renderer_rejects_unsafe_dimensions_scales_and_scenes_before_allocation(
    monkeypatch,
) -> None:
    def no_allocation(*args: object, **kwargs: object) -> None:
        pytest.fail("Invalid rendering requests must not allocate an image.")

    monkeypatch.setattr(Image, "new", no_allocation)
    for bounds, scale in (((0, 0, 100_000, 100_000), 1), ((0, 0, 100, 100), math.nan)):
        with pytest.raises(ValueError):
            render_marks(scene(), bounds, now=0.0, scale=scale)
    with pytest.raises(ValueError):
        render_marks(scene(*(mark() for _ in range(65))), (0, 0, 100, 100), now=0.0)
    with pytest.raises(ValueError):
        render_marks(
            scene(replace(mark(), points=((math.nan, 0), (1, 1)))), (0, 0, 100, 100), now=0.0
        )


def test_visible_bounds_include_glow_and_target_but_exclude_expired_marks() -> None:
    snapshot = scene(
        mark("laser", ((-10, -5),), expires=2),
        mark(points=((500, 500), (600, 500)), expires=0.1),
        waiting=WaitTarget((20, 10), 8, False, 0, 0.1),
    )
    bounds = visible_bounds(snapshot, now=0.2, clip=(-100, -100, 100, 100))
    assert bounds[0] < -10
    assert bounds[1] < -5
    assert bounds[2] > 28
    assert bounds[2] < 100
    assert visible_bounds(scene(), now=0.0) is None
