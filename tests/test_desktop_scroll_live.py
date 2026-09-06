"""Opt-in native resize/scroll QA on owned UI and an opaque owned backdrop."""

import asyncio
import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path

from fastmcp import Client
import pytest
import win32api
import win32con
import win32gui

from desktop_mcp.app import DesktopApplication, create_server
from tests.desktop_repaint_fixture import (
    assert_repaint_matches,
    capture_owner,
    capture_without_repaint,
    caret_bounds,
    edit_state,
    scrollbar_from_pixels,
)
from tests.test_desktop_live import (
    FixtureWindow,
    enable_owned_appearance_capture,
)
from tests.test_desktop_launch_live import control_text

pytestmark = pytest.mark.skipif(
    os.getenv("DESKTOP_MCP_LIVE") != "1" or os.getenv("DESKTOP_MCP_LIVE_APPEARANCE") != "1",
    reason="Requires the explicit owned native appearance opt-in; never exercises a user's app.",
)


def _read_anchor(handle):
    state = edit_state(handle)
    anchor = win32gui.SendMessage(handle, win32con.EM_LINEINDEX, state["first_line"], 0)
    return state, anchor


def _assert_contained(surface):
    panel = surface._panel
    origin = win32gui.ClientToScreen(panel, (0, 0))
    _, _, width, height = win32gui.GetClientRect(panel)
    for handle in surface.window_handles():
        if not win32gui.IsChild(panel, handle) or not win32gui.IsWindowVisible(handle):
            continue
        left, top, right, bottom = win32gui.GetWindowRect(handle)
        assert origin[0] <= left < right <= origin[0] + width
        assert origin[1] <= top < bottom <= origin[1] + height


def _mouse_message(handle, message, point, keys=0):
    x, y = point
    assert -32768 <= x <= 32767 and -32768 <= y <= 32767
    win32gui.SendMessage(handle, message, keys, (x & 0xFFFF) | ((y & 0xFFFF) << 16))


def _scroll_top(handle):
    win32gui.SendMessage(handle, win32con.EM_SETSEL, 0, 0)
    win32gui.SendMessage(handle, win32con.EM_SCROLLCARET, 0, 0)
    win32gui.SendMessage(
        handle, win32con.EM_LINESCROLL, 0, -edit_state(handle)["first_line"]
    )


async def _exercise_scrollbars(client, application, backdrop, root, evidence):
    surface = application.teaching_surface
    panel = surface._panel
    roles = surface.window_roles()
    bars = {
        "history": next(handle for handle, role in roles.items() if role == "transcript-history-scrollbar"),
        "composer": next(handle for handle, role in roles.items() if role == "transcript-composer-scrollbar"),
    }
    dpi = surface.layout_status()["dpi"]
    for name, edit in (("history", surface._editor), ("composer", surface._composer)):
        _scroll_top(edit)
        await asyncio.sleep(0.08)
        before = edit_state(edit)
        assert before["first_line"] == 0
        point = win32gui.ClientToScreen(edit, (12, 12))
        _mouse_message(edit, win32con.WM_MOUSEWHEEL, point, (-120 & 0xFFFF) << 16)
        await asyncio.sleep(0.05)
        assert edit_state(edit)["first_line"] > 0, f"{name} wheel scrolling did not move the viewport"
        _scroll_top(edit)
        win32gui.SendMessage(edit, win32con.WM_KEYDOWN, win32con.VK_NEXT, 1)
        win32gui.SendMessage(edit, win32con.WM_KEYUP, win32con.VK_NEXT, 1 | (3 << 30))
        await asyncio.sleep(0.05)
        assert edit_state(edit)["first_line"] > 0, f"{name} Page Down did not scroll the native EDIT"
        _scroll_top(edit)
        await asyncio.sleep(0.08)
        bar = bars[name]
        bar_bounds = win32gui.GetWindowRect(bar)
        path = root / f"{name}-scrollbar-top.png"
        bounds = capture_without_repaint(panel, backdrop, path)
        measured = scrollbar_from_pixels(path, bounds, bar_bounds, dpi)
        width, height = bar_bounds[2] - bar_bounds[0], bar_bounds[3] - bar_bounds[1]
        assert not win32api.GetAsyncKeyState(win32con.VK_LBUTTON) & 0x8000, (
            "The human is using the mouse; no native fixture capture will be taken"
        )
        thumb = measured["thumb_point"]
        try:
            _mouse_message(bar, win32con.WM_LBUTTONDOWN, thumb, win32con.MK_LBUTTON)
            assert capture_owner(panel) == bar
            _mouse_message(
                bar, win32con.WM_MOUSEMOVE, (width // 2, height - 2), win32con.MK_LBUTTON
            )
        finally:
            _mouse_message(bar, win32con.WM_LBUTTONUP, (width // 2, height - 2))
        assert capture_owner(panel) == 0, f"{name} scrollbar retained mouse capture"
        await asyncio.sleep(0.05)
        bottom = edit_state(edit)
        assert bottom["first_line"] > before["first_line"], f"{name} thumb drag did not scroll"
        last = win32gui.SendMessage(edit, win32con.EM_LINEINDEX, bottom["line_count"] - 1, 0)
        position = win32gui.SendMessage(edit, win32con.EM_POSFROMCHAR, last, 0)
        last_y = ctypes.c_short((position >> 16) & 0xFFFF).value
        assert 0 <= last_y < win32gui.GetClientRect(edit)[3], f"{name} thumb cannot reach the final line"
        capture_without_repaint(panel, backdrop, root / f"{name}-scrollbar-bottom.png")
        _scroll_top(edit)
        await asyncio.sleep(0.05)
        try:
            _mouse_message(
                bar, win32con.WM_LBUTTONDOWN, (width // 2, height - 3), win32con.MK_LBUTTON
            )
        finally:
            _mouse_message(bar, win32con.WM_LBUTTONUP, (width // 2, height - 3))
        await asyncio.sleep(0.05)
        assert edit_state(edit)["first_line"] > 0, f"{name} track paging did not scroll"
        _scroll_top(edit)
        await asyncio.sleep(0.05)
        try:
            _mouse_message(bar, win32con.WM_LBUTTONDOWN, thumb, win32con.MK_LBUTTON)
        finally:
            win32gui.SendMessage(bar, win32con.WM_CANCELMODE, 0, 0)
        assert capture_owner(panel) == 0, f"{name} cancellation retained mouse capture"
        evidence["scrolling"][name] = {**measured, "bottom_first_line": bottom["first_line"]}
    history = surface._editor
    _scroll_top(history)
    win32gui.SendMessage(bars["history"], win32con.WM_KEYDOWN, win32con.VK_END, 1)
    await asyncio.sleep(0.05)
    following = edit_state(history)
    assert following["first_line"] > 0
    await client.call_tool(
        "Transcript", {"title": "Owned scroll fixture", "text": "A reply follows an explicit scroll to the end."}
    )
    await asyncio.sleep(0.08)
    assert edit_state(history)["first_line"] > following["first_line"]
    assert "*" not in win32gui.GetWindowText(win32gui.GetDlgItem(panel, 209))
    _scroll_top(history)
    win32gui.SendMessage(history, win32con.EM_SETSEL, 10, 20)
    prior = edit_state(history)
    await client.call_tool(
        "Transcript", {"title": "Owned scroll fixture", "text": "A new reply must not replace your reading selection."}
    )
    await asyncio.sleep(0.08)
    assert edit_state(history)["selection"] == prior["selection"]
    assert edit_state(history)["first_line"] == prior["first_line"]
    latest = win32gui.GetDlgItem(panel, 209)
    assert "*" in win32gui.GetWindowText(latest)
    capture_without_repaint(panel, backdrop, root / "reading-with-new-reply.png")
    win32gui.SendMessage(panel, win32con.WM_COMMAND, 209, latest)
    await asyncio.sleep(0.05)
    assert "*" not in win32gui.GetWindowText(latest)
    assert edit_state(history)["first_line"] > prior["first_line"]
    capture_without_repaint(panel, backdrop, root / "following-latest.png")


async def test_native_resize_reflow_repaints_without_losing_edit_state(monkeypatch):
    artifacts = os.getenv("DESKTOP_MCP_LIVE_ARTIFACTS")
    if not artifacts:
        pytest.skip("An explicitly owned artifact directory is required.")
    root = Path(artifacts) / "resize-scroll"
    root.mkdir(parents=True, exist_ok=False)
    enable_owned_appearance_capture(monkeypatch)
    application = DesktopApplication()
    fixture = FixtureWindow()
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.SetThreadDpiAwarenessContext.argtypes = [ctypes.c_void_p]
    user32.SetThreadDpiAwarenessContext.restype = ctypes.c_void_p
    previous = user32.SetThreadDpiAwarenessContext(ctypes.c_void_p(-4))
    assert previous
    try:
        fixture.start()
        application.start()
        surface = application.teaching_surface
        panel, history, composer = surface._panel, surface._editor, surface._composer
        win32gui.ShowWindow(application.surface.window_handles()[0], win32con.SW_SHOWMINNOACTIVE)
        win32gui.SendMessage(panel, win32con.WM_COMMAND, 201, win32gui.GetDlgItem(panel, 201))
        win32gui.SetWindowPos(
            fixture.hwnd, panel, 0, 0, 0, 0,
            win32con.SWP_NOMOVE | win32con.SWP_NOSIZE | win32con.SWP_NOACTIVATE,
        )
        async with Client(create_server(application, manage_application=False)) as client:
            await client.call_tool(
                "Transcript",
                {
                    "title": "Owned resize/scroll fixture",
                    "text": "\n".join(f"History line {number:03}: known text" for number in range(200)),
                },
            )
            draft = "\r\n".join(f"Draft line {number:03}: not sent" for number in range(100))
            win32gui.SetWindowText(composer, draft)
            assert control_text(composer) == draft
            for handle, line in ((history, 30), (composer, 10)):
                offset = win32gui.SendMessage(handle, win32con.EM_LINEINDEX, line, 0)
                win32gui.SendMessage(handle, win32con.EM_SETSEL, offset + 2, offset + 12)
                state = edit_state(handle)
                win32gui.SendMessage(
                    handle, win32con.EM_LINESCROLL, 0, line - state["first_line"]
                )
            expected = {handle: _read_anchor(handle) for handle in (history, composer)}
            await asyncio.sleep(0.08)
            foreground, pointer = win32gui.GetForegroundWindow(), win32api.GetCursorPos()
            input_revision = application.controller.input_revision
            physical_dpi = surface.layout_status()["dpi"]
            work = win32gui.GetWindowRect(fixture.hwnd)
            evidence = {
                "host": application.host_info,
                "physical_monitor_dpi": physical_dpi,
                "dpi_scope": "Real native windows with controlled WM_DPICHANGED reflows; no OS DPI settings changed",
                "steps": [],
                "scrolling": {},
            }
            evidence_path = root / "evidence.json"
            evidence_path.write_text(json.dumps(evidence), encoding="utf-8")
            sizes = ((1120, 164), (900, 190), (640, 260), (380, 340), (1120, 440), (1120, 164))
            profiles = list(dict.fromkeys((physical_dpi, 96, 192)))
            if profiles[-1] != physical_dpi:
                profiles.append(physical_dpi)
            routes = [(physical_dpi, "WM_SIZE")] + [
                (dpi, "controlled-WM_DPICHANGED") for dpi in profiles
            ]
            for profile, (dpi, route) in enumerate(routes):
                for index, (width_dip, height_dip) in enumerate(sizes):
                    scale = dpi / 96
                    margin = round(16 * scale)
                    width = min(round(width_dip * scale), work[2] - work[0] - 2 * margin)
                    height = min(round(height_dip * scale), work[3] - work[1] - 2 * margin)
                    left = work[0] + (work[2] - work[0] - width) // 2
                    top = work[1] + margin if route == "WM_SIZE" else work[3] - margin - height
                    rectangle = wintypes.RECT(left, top, left + width, top + height)
                    if route == "WM_SIZE":
                        win32gui.SetWindowPos(
                            panel, 0, left, top, width, height,
                            win32con.SWP_NOACTIVATE | win32con.SWP_NOZORDER,
                        )
                    else:
                        win32gui.SendMessage(
                            panel, 0x02E0, dpi | (dpi << 16), ctypes.addressof(rectangle)
                        )
                    await asyncio.sleep(0.08)
                    assert application.controller.snapshot().interface_ready, surface._error
                    assert surface.layout_status()["dpi"] == dpi
                    step = {
                        "dpi": dpi,
                        "resize_route": route,
                        "requested_dip": [width_dip, height_dip],
                        "layout": surface.layout_status(),
                    }
                    evidence["steps"].append(step)
                    evidence_path.write_text(json.dumps(evidence), encoding="utf-8")
                    _assert_contained(surface)
                    assert control_text(composer) == draft
                    for handle, (original, anchor) in expected.items():
                        state = edit_state(handle)
                        assert state["selection"] == original["selection"]
                        assert state["first_line"] == win32gui.SendMessage(
                            handle, win32con.EM_LINEFROMCHAR, anchor, 0
                        )
                    stem = f"pass-{profile}-dpi-{dpi}-{index}-{width_dip}x{height_dip}"
                    natural, forced = root / f"{stem}-natural.png", root / f"{stem}-forced.png"
                    bounds = capture_without_repaint(panel, fixture.hwnd, natural)
                    caret = caret_bounds(panel)
                    win32gui.RedrawWindow(
                        panel, None, None,
                        win32con.RDW_INVALIDATE | win32con.RDW_ERASE
                        | win32con.RDW_ALLCHILDREN | win32con.RDW_UPDATENOW,
                    )
                    assert capture_without_repaint(panel, fixture.hwnd, forced) == bounds
                    comparison = assert_repaint_matches(
                        natural, forced, bounds, excluded=() if caret is None else (caret,)
                    )
                    step.update(comparison)
                    evidence_path.write_text(json.dumps(evidence), encoding="utf-8")
                    assert win32gui.GetForegroundWindow() == foreground, {
                        "before": foreground,
                        "after": win32gui.GetForegroundWindow(),
                        "input_revision_before": input_revision,
                        "input_revision_after": application.controller.input_revision,
                        "panel": panel,
                    }
                    assert win32api.GetCursorPos() == pointer
            await _exercise_scrollbars(client, application, fixture.hwnd, root, evidence)
            assert control_text(composer) == draft
            assert win32gui.GetForegroundWindow() == foreground
            assert win32api.GetCursorPos() == pointer
            evidence_path.write_text(json.dumps(evidence), encoding="utf-8")
            assert not application.controller.snapshot().armed
    finally:
        try:
            application.close()
        finally:
            try:
                if fixture.thread.ident is not None:
                    fixture.close()
            finally:
                assert user32.SetThreadDpiAwarenessContext(previous)
