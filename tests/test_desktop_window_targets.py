import ctypes
from collections import Counter
from ctypes import wintypes
from datetime import datetime
import os
from types import SimpleNamespace

import pytest

from desktop_mcp.native import TargetDenied, WindowsInput
from desktop_mcp.window_targets import GUIThreadInfo
import desktop_mcp.window_targets as window_targets


class WindowPort:
    """Synthetic Win32 only; unexpected calls include all text and input APIs."""

    def __init__(self):
        self.pid = os.getpid()
        self.windows = {}
        self.foreground = 10
        self.hit = 10
        self.z_order = []
        self.gui = {}
        self.calls = Counter()
        self.messages = []
        self.regions = {}
        self.next_region = 1
        self.add(10, owned=False, bounds=(-1000, -1000, 1000, 1000))

    def __getattr__(self, name):
        raise AssertionError(f"Unexpected native API: {name}")

    def add(
        self,
        handle,
        *,
        owned=True,
        root=None,
        owner_root=None,
        bounds=(0, 0, 200, 200),
        visible=True,
        minimized=False,
        style=0,
        thread=None,
        affinity=0x11,
        **extras,
    ):
        root = handle if root is None else root
        owner_root = root if owner_root is None else owner_root
        self.windows[handle] = SimpleNamespace(
            pid=self.pid if owned else self.pid + 1,
            thread=thread or (100 if owned else 200),
            root=root,
            owner_root=owner_root,
            bounds=bounds,
            visible=visible,
            minimized=minimized,
            style=style,
            affinity=affinity,
            enabled=True,
            child=0,
            hit_test=1,
            timeout=False,
            region=None,
            layered=None,
            rect_failure=False,
        )
        for key, value in extras.items():
            setattr(self.windows[handle], key, value)
        self.z_order.append(handle)
        return self.windows[handle]

    def GetForegroundWindow(self):
        self.calls["foreground"] += 1
        return self.foreground() if callable(self.foreground) else self.foreground

    def IsWindow(self, handle):
        self.calls["is_window", handle] += 1
        return handle in self.windows

    def GetWindowThreadProcessId(self, handle, process):
        self.calls["identity", handle] += 1
        ctypes.cast(process, ctypes.POINTER(wintypes.DWORD)).contents.value = self.windows[
            handle
        ].pid
        return self.windows[handle].thread

    def GetAncestor(self, handle, flag):
        assert flag in (2, 3)
        self.calls["ancestor", handle, flag] += 1
        window = self.windows[handle]
        return window.root if flag == 2 else window.owner_root

    def GetGUIThreadInfo(self, thread, pointer):
        self.calls["gui", thread] += 1
        info = ctypes.cast(pointer, ctypes.POINTER(GUIThreadInfo)).contents
        assert info.cbSize == ctypes.sizeof(GUIThreadInfo)
        for name, value in self.gui.get(thread, {}).items():
            setattr(info, name, value)
        return True

    def GetWindowLongW(self, handle, index):
        assert index == -20
        self.calls["style", handle] += 1
        return self.windows[handle].style

    def IsWindowVisible(self, handle):
        self.calls["visible", handle] += 1
        return self.windows[handle].visible

    def IsIconic(self, handle):
        self.calls["iconic", handle] += 1
        return self.windows[handle].minimized

    def GetWindowRect(self, handle, pointer):
        self.calls["rect", handle] += 1
        window = self.windows[handle]
        if window.rect_failure:
            return False
        rect = ctypes.cast(pointer, ctypes.POINTER(wintypes.RECT)).contents
        rect.left, rect.top, rect.right, rect.bottom = window.bounds
        return True

    def GetWindowDisplayAffinity(self, handle, pointer):
        self.calls["affinity", handle] += 1
        affinity = self.windows[handle].affinity
        if affinity is None:
            return False
        ctypes.cast(pointer, ctypes.POINTER(wintypes.DWORD)).contents.value = affinity
        return True

    def WindowFromPoint(self, point):
        self.calls["hit", point.x, point.y] += 1
        return self.hit() if callable(self.hit) else self.hit

    def GetWindow(self, handle, command):
        assert command == 2
        self.calls["next", handle] += 1
        index = self.z_order.index(handle) + 1
        return self.z_order[index] if index < len(self.z_order) else 0

    def IsWindowEnabled(self, handle):
        return self.windows[handle].enabled

    def CreateRectRgn(self, *rect):
        assert rect == (0, 0, 0, 0)
        region = self.next_region
        self.next_region += 1
        self.regions[region] = None
        return region

    def GetWindowRgn(self, handle, region):
        self.regions[region] = self.windows[handle].region
        return 0 if self.regions[region] is None else 2

    def PtInRegion(self, region, x, y):
        value = self.regions[region]
        return value(x, y) if callable(value) else value

    def DeleteObject(self, region):
        del self.regions[region]
        return True

    def GetLayeredWindowAttributes(self, handle, color, alpha, flags):
        value = self.windows[handle].layered
        if value is None:
            return False
        ctypes.cast(alpha, ctypes.POINTER(wintypes.BYTE)).contents.value = value[0]
        ctypes.cast(flags, ctypes.POINTER(wintypes.DWORD)).contents.value = value[1]
        return True

    def SendMessageTimeoutW(self, handle, message, wparam, lparam, flags, timeout, pointer):
        assert message == 0x0084 and wparam == 0
        assert flags == 0x23 and 0 < timeout <= 25
        self.messages.append(
            (handle, ctypes.c_short(lparam & 0xFFFF).value, ctypes.c_short(lparam >> 16).value)
        )
        window = self.windows[handle]
        if window.timeout:
            return False
        ctypes.cast(pointer, ctypes.POINTER(ctypes.c_size_t)).contents.value = ctypes.c_size_t(
            window.hit_test
        ).value
        return True

    def ScreenToClient(self, handle, pointer):
        point = ctypes.cast(pointer, ctypes.POINTER(wintypes.POINT)).contents
        bounds = self.windows[handle].bounds
        point.x -= bounds[0]
        point.y -= bounds[1]
        return True

    def ChildWindowFromPointEx(self, handle, point, flags):
        assert flags == 3
        return self.windows[handle].child or handle


def backend_for(port, handles=(20, 21), roles=None):
    backend = WindowsInput.__new__(WindowsInput)
    backend._user32 = port
    backend._gdi32 = port
    backend._control_windows = lambda: handles
    backend._window_roles = (
        (lambda: roles)
        if roles is not None
        else lambda: {20: "transcript", 21: "transcript-composer"}
    )
    backend._last_denial = None
    return backend


@pytest.fixture
def target():
    port = WindowPort()
    port.add(20)
    port.add(21, root=20, bounds=(20, 20, 180, 180))
    return backend_for(port), port


@pytest.mark.parametrize("visible,minimized", [(True, False), (True, True), (False, False)])
def test_occluded_or_minimized_roots_do_not_block_an_external_receiver(target, visible, minimized):
    backend, port = target
    port.windows[20].visible = visible
    port.windows[20].minimized = minimized
    port.gui[100] = {"hwndFocus": 21, "hwndActive": 20}
    backend.ensure_target((50, 50), 10)
    assert backend.last_denial is None
    assert not port.calls["rect", 20] and not port.calls["rect", 21]
    rows = {row["window_id"]: row for row in backend.protected_windows()}
    child = rows[21]
    assert child["visible"] is True
    assert child["minimized"] is minimized
    assert child["effective_visible"] is (visible and not minimized)
    assert child["root_id"] == 20
    assert port.calls["iconic", 21] == 0
    assert port.calls["affinity", 20] == 1


def test_capture_excluded_transcript_composer_on_top_is_still_protected(target):
    backend, port = target
    port.hit = 21
    port.windows[20].bounds = (-300, -200, 0, 200)
    port.windows[21].bounds = (-200, -100, -50, 100)
    with pytest.raises(TargetDenied, match="transcript-composer.*HWND 21") as caught:
        backend.ensure_target((-100, -50), 10)
    details = caught.value.details
    assert isinstance(caught.value, RuntimeError)
    assert details["code"] == "protected_target"
    assert details["operation"] == "input"
    assert details["routing"] == "pointer_hit"
    assert details["target_point"] == (-100, -50)
    assert details["expected_window"] == details["actual_foreground"] == 10
    assert datetime.fromisoformat(details["timestamp"]).tzinfo is not None
    assert details["matched"] == {
        "window_id": 21,
        "root_id": 20,
        "owner_root_id": 20,
        "role": "transcript-composer",
        "bounds": (-200, -100, -50, 100),
        "visible": True,
        "minimized": False,
        "effective_visible": True,
        "click_through": False,
        "capture_excluded": True,
        "status": "ok",
    }
    assert "(-100, -50)" in str(caught.value)
    assert "(-200, -100, -50, 100)" in str(caught.value)


def test_unknown_same_process_popup_is_protected_without_registration(target):
    backend, port = target
    port.add(30, owner_root=20)
    port.hit = 30
    with pytest.raises(TargetDenied) as caught:
        backend.ensure_target((50, 50))
    matched = caught.value.details["matched"]
    assert matched["role"] == "owned-popup"
    assert matched["window_id"] == matched["root_id"] == 30
    assert matched["owner_root_id"] == 20


def test_foreign_child_of_owned_root_is_protected_by_root_ownership(target):
    backend, port = target
    port.windows[21].pid = port.pid + 1
    port.hit = 21
    with pytest.raises(TargetDenied) as caught:
        backend.ensure_target((50, 50))
    matched = caught.value.details["matched"]
    assert matched["root_id"] == 20
    assert matched["role"] == "transcript"
    assert matched["status"] == "ok"


def test_owned_window_without_a_role_does_not_claim_to_be_a_main_panel(target):
    backend, port = target
    backend.set_window_roles(None)
    port.hit = 21
    with pytest.raises(TargetDenied) as caught:
        backend.ensure_target((50, 50))
    assert caught.value.details["matched"]["role"] == "owned-window"
    assert "main" not in str(caught.value)


@pytest.mark.parametrize("foreground", [20, 21])
@pytest.mark.parametrize("point", [None, (700, 700)])
def test_keyboard_ownership_is_independent_of_pointer_location(target, foreground, point):
    backend, port = target
    port.foreground = foreground
    with pytest.raises(TargetDenied) as caught:
        backend.ensure_target(point)
    assert caught.value.details["routing"] == "foreground"
    assert caught.value.details["matched"]["root_id"] == 20
    assert not any(key[0] == "hit" for key in port.calls if isinstance(key, tuple))


def test_keyboard_focus_child_cannot_bypass_foreign_foreground_guard(target):
    backend, port = target
    port.gui[200] = {"hwndFocus": 21}
    with pytest.raises(TargetDenied) as caught:
        backend.ensure_target((700, 700), 10)
    assert caught.value.details["routing"] == "keyboard_focus"
    assert caught.value.details["matched"]["role"] == "transcript-composer"


def test_owned_active_window_on_the_foreground_queue_remains_protected(target):
    backend, port = target
    port.gui[200] = {"hwndActive": 20}
    with pytest.raises(TargetDenied) as caught:
        backend.ensure_target((700, 700), 10)
    assert caught.value.details["routing"] == "keyboard_active"
    assert caught.value.details["matched"]["role"] == "transcript"


@pytest.mark.parametrize("foreground", [10, 20])
def test_expected_foreground_mismatch_has_its_own_diagnostic_code(target, foreground):
    backend, port = target
    port.foreground = foreground
    with pytest.raises(TargetDenied, match="foreground window changed") as caught:
        backend.ensure_target((-100, 30), 55)
    assert caught.value.details["code"] == "foreground_mismatch"
    assert caught.value.details["expected_window"] == 55
    assert caught.value.details["actual_foreground"] == foreground
    assert caught.value.details["matched"] is None
    assert port.calls == {"foreground": 1}


def test_last_denial_is_copied_and_success_does_not_erase_it(target):
    backend, port = target
    assert backend.last_denial is None
    port.hit = 21
    with pytest.raises(TargetDenied) as caught:
        backend.ensure_target((50, 50))
    caught.value.details["matched"]["role"] = "changed-exception"
    snapshot = backend.last_denial
    snapshot["matched"]["role"] = "changed-copy"
    snapshot["target_point"] = (0, 0)
    port.hit = 10
    backend.ensure_target((500, 500))
    assert backend.last_denial["matched"]["role"] == "transcript-composer"
    assert backend.last_denial["target_point"] == (50, 50)


def test_read_only_foreground_guard_uses_ownership_and_preserves_absence(target):
    backend, port = target
    assert backend.ensure_observable_foreground() == 10
    port.foreground = 0
    assert backend.ensure_observable_foreground() == 0
    port.foreground = 21
    with pytest.raises(TargetDenied) as caught:
        backend.ensure_observable_foreground()
    assert caught.value.details["operation"] == "observe_foreground"
    assert caught.value.details["target_point"] is None
    assert caught.value.details["matched"]["role"] == "transcript-composer"
    assert backend.last_denial == caught.value.details


def test_missing_and_recycled_registered_handles_are_not_open_control_panels(target):
    backend, port = target
    del port.windows[20]
    del port.windows[21]
    port.add(21, owned=False)
    port.hit = 21
    backend.ensure_target((50, 50), 10)
    rows = {row["window_id"]: row for row in backend.protected_windows()}
    assert rows[20]["status"] == "missing"
    assert rows[20]["effective_visible"] is False
    assert rows[21]["status"] == "stale"
    assert rows[21]["bounds"] is None
    assert rows[21]["visible"] is None
    assert not port.calls["rect", 21]


def test_geometry_failure_is_diagnostic_not_a_phantom_rectangle_block(target):
    backend, port = target
    port.windows[20].rect_failure = True
    backend.ensure_target((50, 50))
    rows = {row["window_id"]: row for row in backend.protected_windows()}
    assert rows[20]["status"] == "unavailable"
    assert "GetWindowRect" in rows[20]["reason"]
    assert rows[20]["bounds"] is None


def test_registered_child_with_a_missing_root_is_reported_unavailable(target):
    backend, port = target
    del port.windows[20]
    port.gui[100] = {"hwndFocus": 21}
    backend.ensure_target((500, 500))
    rows = {row["window_id"]: row for row in backend.protected_windows()}
    assert rows[20]["status"] == "missing"
    assert rows[21]["status"] == "unavailable"
    assert rows[21]["root_id"] is None
    assert "root" in rows[21]["reason"]


def test_missing_hit_handle_is_an_explicit_indeterminate_denial(target):
    backend, port = target
    port.hit = 999
    with pytest.raises(TargetDenied) as caught:
        backend.ensure_target((-500, 0))
    assert caught.value.details["code"] == "target_indeterminate"
    assert caught.value.details["matched"]["status"] == "missing"
    assert "no longer exists" in str(caught.value)


def test_affinity_query_failure_is_unknown_not_success(target):
    backend, port = target
    port.windows[20].affinity = None
    rows = backend.protected_windows()
    assert all(row["capture_excluded"] is None for row in rows)
    assert all(row["status"] == "ok" for row in rows)


def test_role_callback_can_supply_handles_and_be_replaced(target):
    backend, port = target
    backend.set_control_windows(lambda: ())
    backend.set_window_roles(lambda: {20: "main-control", 21: "main-stop"})
    port.hit = 21
    with pytest.raises(TargetDenied) as caught:
        backend.ensure_target((50, 50))
    assert caught.value.details["matched"]["role"] == "main-stop"
    backend.set_window_roles(None)
    with pytest.raises(TargetDenied) as caught:
        backend.ensure_target((50, 50))
    assert caught.value.details["matched"]["role"] == "owned-popup"


@pytest.mark.parametrize("style", [0x08000000, 0x20, 0x08000020])
def test_noactivate_or_paint_transparency_alone_never_exempts_owned_controls(target, style):
    backend, port = target
    port.windows[20].style = style
    port.hit = 20
    with pytest.raises(TargetDenied) as caught:
        backend.ensure_target((50, 50))
    assert caught.value.details["code"] == "protected_target"
    assert caught.value.details["matched"]["click_through"] is False


def add_overlay(backend, port):
    port.add(40, style=0x00080020 | 0x08000000, bounds=(-1000, -1000, 1000, 1000))
    backend.set_window_roles(
        lambda: {20: "transcript", 21: "transcript-composer", 40: "cursor-overlay"}
    )
    port.hit = 40
    port.z_order = [40, 10, 20]


def test_overlay_resolves_underlying_z_order_not_all_owned_rectangles(target):
    backend, port = target
    add_overlay(backend, port)
    backend.ensure_target((-100, -200), 10)
    assert port.messages == [(10, -100, -200)]
    assert not port.calls["rect", 20]
    assert port.regions == {}
    rows = {row["window_id"]: row for row in backend.protected_windows()}
    assert rows[40]["click_through"] is True


def test_overlay_does_not_expose_a_protected_window_actually_under_it(target):
    backend, port = target
    add_overlay(backend, port)
    port.z_order = [40, 20, 10]
    port.windows[20].child = 21
    with pytest.raises(TargetDenied) as caught:
        backend.ensure_target((50, 50))
    assert caught.value.details["code"] == "protected_target"
    assert caught.value.details["matched"]["role"] == "transcript-composer"
    assert caught.value.details["matched"]["window_id"] == 21
    assert port.regions == {}


def test_transparent_child_cannot_be_used_to_skip_its_protected_root(target):
    backend, port = target
    port.windows[21].style = 0x80020
    port.hit = 21
    with pytest.raises(TargetDenied, match="sibling routing") as caught:
        backend.ensure_target((50, 50))
    assert caught.value.details["code"] == "target_indeterminate"
    assert caught.value.details["matched"]["click_through"] is True


@pytest.mark.parametrize("minimized,visible", [(True, True), (False, False)])
def test_overlay_skips_ineffective_roots_before_an_external_receiver(target, minimized, visible):
    backend, port = target
    add_overlay(backend, port)
    port.z_order = [40, 20, 10]
    port.windows[20].minimized = minimized
    port.windows[20].visible = visible
    backend.ensure_target((50, 50))
    assert not port.calls["rect", 20]


@pytest.mark.parametrize("field,value", [("region", False), ("hit_test", 0)])
def test_overlay_fallback_does_not_turn_a_rectangular_hole_into_a_protected_hit(
    target, field, value
):
    backend, port = target
    add_overlay(backend, port)
    port.z_order = [40, 20, 10]
    setattr(port.windows[20], field, value)
    backend.ensure_target((50, 50))
    assert port.regions == {}


@pytest.mark.parametrize(
    "gui,role,routing",
    [
        ({"hwndCapture": 21}, "transcript-composer", "mouse_capture"),
        ({"hwndCapture": 40}, "cursor-overlay", "mouse_capture"),
        ({"flags": 4, "hwndMenuOwner": 30}, "owned-popup", "menu_owner"),
        ({"flags": 2, "hwndMoveSize": 20}, "transcript", "move_size"),
    ],
)
def test_owned_capture_and_modal_routing_outrank_pointer_geometry(target, gui, role, routing):
    backend, port = target
    add_overlay(backend, port)
    port.add(30, owner_root=20)
    port.gui[100] = gui
    with pytest.raises(TargetDenied) as caught:
        backend.ensure_target((700, 700))
    assert caught.value.details["code"] == "protected_target"
    assert caught.value.details["routing"] == routing
    assert caught.value.details["matched"]["role"] == role
    assert not port.messages


def test_capture_on_a_second_owned_gui_thread_is_checked(target):
    backend, port = target
    port.windows[21].thread = 101
    port.gui[101] = {"hwndCapture": 21}
    with pytest.raises(TargetDenied) as caught:
        backend.ensure_target((700, 700))
    assert caught.value.details["routing"] == "mouse_capture"
    assert caught.value.details["matched"]["window_id"] == 21


def test_popup_of_a_minimized_owner_is_not_reported_effectively_visible(target):
    backend, port = target
    port.add(30, owner_root=20)
    port.windows[20].minimized = True
    port.gui[100] = {"hwndActive": 30}
    rows = {row["window_id"]: row for row in backend.protected_windows()}
    assert rows[30]["visible"] is True
    assert rows[30]["minimized"] is True
    assert rows[30]["effective_visible"] is False
    backend.ensure_target((50, 50))


def test_protected_metadata_includes_unregistered_owned_menu_and_capture(target):
    backend, port = target
    port.add(30, owner_root=20)
    port.gui[100] = {"flags": 4, "hwndMenuOwner": 30, "hwndCapture": 30}
    rows = {row["window_id"]: row for row in backend.protected_windows()}
    assert set(rows) == {20, 21, 30}
    assert rows[30]["role"] == "owned-popup"
    assert port.calls["gui", 100] == 1


def test_foreign_capture_does_not_misidentify_a_protected_target(target):
    backend, port = target
    port.gui[200] = {"hwndCapture": 10, "flags": 4, "hwndMenuOwner": 10}
    backend.ensure_target((50, 50))
    assert backend.last_denial is None


@pytest.mark.parametrize("layered", [None, (255, 1), (128, 2)])
def test_layered_pixel_shape_beneath_overlay_is_indeterminate_not_allowed(target, layered):
    backend, port = target
    add_overlay(backend, port)
    port.windows[10].style = 0x80000
    port.windows[10].layered = layered
    with pytest.raises(TargetDenied, match="pixel hit region") as caught:
        backend.ensure_target((50, 50))
    assert caught.value.details["code"] == "target_indeterminate"
    assert caught.value.details["matched"]["role"] == "external-window"


def test_opaque_constant_alpha_layered_window_beneath_overlay_is_resolvable(target):
    backend, port = target
    add_overlay(backend, port)
    port.windows[10].style = 0x80000
    port.windows[10].layered = (255, 2)
    backend.ensure_target((50, 50))


@pytest.mark.parametrize("hit_test", [-1, -2])
def test_ambiguous_nonlayered_hit_routing_is_not_assumed_click_through(target, hit_test):
    backend, port = target
    add_overlay(backend, port)
    port.windows[10].hit_test = hit_test
    with pytest.raises(TargetDenied, match="ambiguous transparent") as caught:
        backend.ensure_target((50, 50))
    assert caught.value.details["code"] == "target_indeterminate"


def test_timed_out_overlay_probe_is_an_explicit_denial(target):
    backend, port = target
    add_overlay(backend, port)
    port.windows[10].timeout = True
    with pytest.raises(TargetDenied, match="WM_NCHITTEST") as caught:
        backend.ensure_target((50, 50))
    assert caught.value.details["code"] == "target_indeterminate"
    assert port.regions == {}


def test_overlay_probe_never_calls_an_unbounded_same_thread_window_procedure(target, monkeypatch):
    backend, port = target
    add_overlay(backend, port)
    port.z_order = [40, 20, 10]
    monkeypatch.setattr(window_targets, "get_native_id", lambda: 100)
    with pytest.raises(TargetDenied, match="own GUI thread") as caught:
        backend.ensure_target((50, 50))
    assert caught.value.details["code"] == "target_indeterminate"
    assert not port.messages


def test_overlay_walk_is_bounded_and_cycles_are_explicit(target, monkeypatch):
    backend, port = target
    add_overlay(backend, port)
    port.GetWindow = lambda handle, flag: 40
    with pytest.raises(TargetDenied, match="cycled"):
        backend.ensure_target((50, 50))
    monkeypatch.setattr(window_targets, "_MAX_HIT_WINDOWS", 1)
    port.GetWindow = lambda handle, flag: 20
    port.windows[20].visible = False
    with pytest.raises(TargetDenied, match="limit was exceeded"):
        backend.ensure_target((50, 50))


def test_overlay_child_walk_is_bounded_and_cycles_are_explicit(target):
    backend, port = target
    add_overlay(backend, port)
    port.add(11, owned=False, root=10)
    port.windows[10].child = 11
    port.windows[11].child = 10
    with pytest.raises(TargetDenied, match="cycled") as caught:
        backend.ensure_target((50, 50))
    assert caught.value.details["code"] == "target_indeterminate"


def test_missing_underlying_receiver_is_not_a_success(target):
    backend, port = target
    add_overlay(backend, port)
    port.z_order = [40]
    with pytest.raises(TargetDenied, match="No receiver") as caught:
        backend.ensure_target((50, 50))
    assert caught.value.details["code"] == "target_indeterminate"


def test_large_physical_point_is_not_silently_truncated_in_overlay_hit_testing(target):
    backend, port = target
    add_overlay(backend, port)
    port.windows[10].bounds = port.windows[40].bounds = (0, 0, 40000, 1000)
    with pytest.raises(TargetDenied, match="cannot represent") as caught:
        backend.ensure_target((35000, 500))
    assert caught.value.details["target_point"] == (35000, 500)
    assert not port.messages


def test_input_query_failure_is_explicit_and_does_not_become_a_protected_target(target):
    backend, port = target
    port.GetGUIThreadInfo = lambda thread, pointer: False
    with pytest.raises(TargetDenied, match="GetGUIThreadInfo") as caught:
        backend.ensure_target((50, 50))
    assert caught.value.details["code"] == "target_indeterminate"
    assert backend.last_denial == caught.value.details


def test_registry_limits_are_explicit_instead_of_silently_dropping_protection(target, monkeypatch):
    backend, port = target
    monkeypatch.setattr(window_targets, "_MAX_WINDOWS", 1)
    with pytest.raises(ValueError, match="registered-window query limit"):
        backend.ensure_target((50, 50))


def test_registered_window_status_queries_no_control_text_or_input_apis(target):
    backend, port = target
    rows = backend.protected_windows()
    assert {row["window_id"] for row in rows} == {20, 21}
    assert port.calls["iconic", 20] == 1
    assert port.calls["style", 20] == 1
    assert not port.messages
    assert not ({"title", "text", "composer", "history"} & set(rows[0]))


def test_receiver_recycled_to_our_process_during_query_is_not_allowed(target):
    backend, port = target

    def hit():
        if port.calls["hit", 50, 50] == 2:
            port.windows[10].pid = port.pid
        return 10

    port.hit = hit
    with pytest.raises(TargetDenied, match="identity changed") as caught:
        backend.ensure_target((50, 50))
    assert caught.value.details["code"] == "target_indeterminate"


def test_foreground_change_during_query_retains_expected_window_mismatch(target):
    backend, port = target
    foregrounds = iter((10, 11))
    port.foreground = lambda: next(foregrounds)
    with pytest.raises(TargetDenied) as caught:
        backend.ensure_target((50, 50), 10)
    assert caught.value.details["code"] == "foreground_mismatch"
    assert caught.value.details["actual_foreground"] == 11


def test_absent_foreground_denies_input_without_claiming_an_owned_panel(target):
    backend, port = target
    port.foreground = 0
    with pytest.raises(TargetDenied) as caught:
        backend.ensure_target((50, 50))
    assert caught.value.details["code"] == "target_indeterminate"
    assert caught.value.details["matched"] is None


def test_receiver_change_during_query_is_not_a_success(target):
    backend, port = target
    hits = iter((10, 21))
    port.hit = lambda: next(hits)
    with pytest.raises(TargetDenied, match="hit window changed") as caught:
        backend.ensure_target((50, 50))
    assert caught.value.details["code"] == "target_indeterminate"


def test_focus_cannot_target_unregistered_owned_popup_or_permission_child(target):
    backend, port = target
    port.add(30)
    for handle in (21, 30):
        with pytest.raises(TargetDenied) as caught:
            backend.focus(handle)
        assert caught.value.details["operation"] == "focus"
        assert caught.value.details["matched"]["window_id"] == handle


def test_missing_requested_focus_preserves_existing_validation(target):
    backend, port = target
    with pytest.raises(ValueError, match="no longer exists"):
        backend.focus(999)
