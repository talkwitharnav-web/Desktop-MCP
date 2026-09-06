"""Opt-in native resize/scroll QA on owned UI and an opaque owned backdrop."""

import asyncio
import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import time

from fastmcp import Client
import pytest
import win32api
import win32con
import win32gui

from desktop_mcp.app import DesktopApplication, create_server
from desktop_mcp.transcript_layout import COMPACT_SIZE
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
from tests.test_desktop_launch_live import chat_message_controls, control_text

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
        if win32gui.GetParent(handle) != panel:
            # Message rows may be partly clipped; outer controls must be fully contained.
            assert win32gui.IsChild(surface._history_window, handle)
            assert (
                win32gui.GetWindowLong(surface._history_window, win32con.GWL_STYLE)
                & win32con.WS_CLIPCHILDREN
            )
            assert not (
                win32gui.GetWindowLong(handle, win32con.GWL_EXSTYLE) & win32con.WS_EX_LAYERED
            )
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
    win32gui.SendMessage(handle, win32con.EM_LINESCROLL, 0, -edit_state(handle)["first_line"])


def _message_editor(history, prefix):
    return next(
        handle
        for handle, text in chat_message_controls(history, expected_pid=os.getpid())
        if text.startswith(prefix)
    )


def _history_geometry(history):
    return {
        win32gui.GetParent(handle): win32gui.GetWindowRect(win32gui.GetParent(handle))
        for handle, _ in chat_message_controls(history, expected_pid=os.getpid())
        if win32gui.IsWindowVisible(handle)
    }


def _history_edge(panel, *, bottom=False):
    bar = win32gui.GetDlgItem(panel, 306)
    key = win32con.VK_END if bottom else win32con.VK_HOME
    win32gui.SendMessage(bar, win32con.WM_KEYDOWN, key, 1)
    win32gui.SendMessage(bar, win32con.WM_KEYUP, key, 1 | (3 << 30))


async def _exercise_scrollbars(client, application, backdrop, root, evidence):
    surface = application.teaching_surface
    panel = surface._panel
    roles = surface.window_roles()
    bars = {
        "history": next(
            handle for handle, role in roles.items() if role == "transcript-history-scrollbar"
        ),
        "composer": next(
            handle for handle, role in roles.items() if role == "transcript-composer-scrollbar"
        ),
    }
    _history_edge(panel)
    message = _message_editor(surface._history_window, "History line 000")
    bars["message"] = next(
        handle
        for handle, role in roles.items()
        if role == "transcript-history-message-scrollbar"
        and win32gui.GetParent(handle) == win32gui.GetParent(message)
    )
    dpi = surface.layout_status()["dpi"]
    for name, edit in (("message", message), ("composer", surface._composer)):
        _scroll_top(edit)
        await asyncio.sleep(0.08)
        before = edit_state(edit)
        assert before["first_line"] == 0
        point = win32gui.ClientToScreen(edit, (12, 12))
        _mouse_message(edit, win32con.WM_MOUSEWHEEL, point, (-120 & 0xFFFF) << 16)
        await asyncio.sleep(0.05)
        assert edit_state(edit)["first_line"] > 0, (
            f"{name} wheel scrolling did not move the viewport"
        )
        _scroll_top(edit)
        win32gui.SendMessage(edit, win32con.WM_KEYDOWN, win32con.VK_NEXT, 1)
        win32gui.SendMessage(edit, win32con.WM_KEYUP, win32con.VK_NEXT, 1 | (3 << 30))
        await asyncio.sleep(0.05)
        assert edit_state(edit)["first_line"] > 0, (
            f"{name} Page Down did not scroll the native EDIT"
        )
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
        assert 0 <= last_y < win32gui.GetClientRect(edit)[3], (
            f"{name} thumb cannot reach the final line"
        )
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
    history = surface._history_window
    _history_edge(panel)
    await asyncio.sleep(0.05)
    top_geometry = _history_geometry(history)
    point = win32gui.ClientToScreen(history, (2, 2))
    _mouse_message(history, win32con.WM_MOUSEWHEEL, point, (-120 & 0xFFFF) << 16)
    await asyncio.sleep(0.05)
    assert _history_geometry(history) != top_geometry, "Outer history wheel did not scroll"
    _history_edge(panel)
    win32gui.SendMessage(history, win32con.WM_KEYDOWN, win32con.VK_NEXT, 1)
    win32gui.SendMessage(history, win32con.WM_KEYUP, win32con.VK_NEXT, 1 | (3 << 30))
    await asyncio.sleep(0.05)
    assert _history_geometry(history) != top_geometry, "Outer history Page Down did not scroll"
    _history_edge(panel)
    await asyncio.sleep(0.05)
    bar = bars["history"]
    bar_bounds = win32gui.GetWindowRect(bar)
    path = root / "history-scrollbar-top.png"
    bounds = capture_without_repaint(panel, backdrop, path)
    measured = scrollbar_from_pixels(path, bounds, bar_bounds, dpi)
    width, height = bar_bounds[2] - bar_bounds[0], bar_bounds[3] - bar_bounds[1]
    thumb = measured["thumb_point"]
    assert not win32api.GetAsyncKeyState(win32con.VK_LBUTTON) & 0x8000
    try:
        _mouse_message(bar, win32con.WM_LBUTTONDOWN, thumb, win32con.MK_LBUTTON)
        assert capture_owner(panel) == bar
        _mouse_message(bar, win32con.WM_MOUSEMOVE, (width // 2, height - 2), win32con.MK_LBUTTON)
    finally:
        _mouse_message(bar, win32con.WM_LBUTTONUP, (width // 2, height - 2))
    assert capture_owner(panel) == 0
    await asyncio.sleep(0.05)
    last_message = _message_editor(history, "Context message 7")
    assert win32gui.IsWindowVisible(last_message), "Outer thumb cannot reach the last message"
    capture_without_repaint(panel, backdrop, root / "history-scrollbar-bottom.png")
    _history_edge(panel)
    await asyncio.sleep(0.05)
    try:
        _mouse_message(bar, win32con.WM_LBUTTONDOWN, (width // 2, height - 3), win32con.MK_LBUTTON)
    finally:
        _mouse_message(bar, win32con.WM_LBUTTONUP, (width // 2, height - 3))
    await asyncio.sleep(0.05)
    assert _history_geometry(history) != top_geometry, "Outer track paging did not scroll"
    _history_edge(panel)
    await asyncio.sleep(0.05)
    try:
        _mouse_message(bar, win32con.WM_LBUTTONDOWN, thumb, win32con.MK_LBUTTON)
    finally:
        win32gui.SendMessage(bar, win32con.WM_CANCELMODE, 0, 0)
    assert capture_owner(panel) == 0
    evidence["scrolling"]["history"] = measured
    _history_edge(panel, bottom=True)
    foreground = win32gui.GetForegroundWindow()
    await client.call_tool(
        "Transcript",
        {"title": "Owned scroll fixture", "text": "A reply follows an explicit scroll to the end."},
    )
    await asyncio.sleep(0.25)
    following = _message_editor(history, "A reply follows an explicit scroll")
    assert win32gui.IsWindowVisible(following)
    assert "*" not in win32gui.GetWindowText(win32gui.GetDlgItem(panel, 209))
    assert win32gui.GetForegroundWindow() == foreground
    _history_edge(panel)
    _scroll_top(message)
    win32gui.SendMessage(message, win32con.EM_SETSEL, 10, 20)
    prior = edit_state(message)
    prior_geometry = _history_geometry(history)
    await client.call_tool(
        "Transcript",
        {
            "title": "Owned scroll fixture",
            "text": "A new reply must not replace your reading selection.",
        },
    )
    await asyncio.sleep(0.08)
    assert edit_state(message)["selection"] == prior["selection"]
    assert edit_state(message)["first_line"] == prior["first_line"]
    assert _history_geometry(history) == prior_geometry
    latest = win32gui.GetDlgItem(panel, 209)
    assert "*" in win32gui.GetWindowText(latest)
    capture_without_repaint(panel, backdrop, root / "reading-with-new-reply.png")
    win32gui.SendMessage(panel, win32con.WM_COMMAND, 209, latest)
    await asyncio.sleep(0.05)
    assert "*" not in win32gui.GetWindowText(latest)
    newest = _message_editor(history, "A new reply must not replace")
    assert win32gui.IsWindowVisible(newest)
    capture_without_repaint(panel, backdrop, root / "following-latest.png")
    assert win32gui.GetForegroundWindow() == foreground


async def _exercise_arrival(client, application, evidence):
    surface = application.teaching_surface
    history = surface._history_window
    win32gui.SendMessage(
        surface._panel,
        win32con.WM_COMMAND,
        209,
        win32gui.GetDlgItem(surface._panel, 209),
    )
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.SystemParametersInfoW.argtypes = [
        wintypes.UINT,
        wintypes.UINT,
        ctypes.c_void_p,
        wintypes.UINT,
    ]
    user32.SystemParametersInfoW.restype = wintypes.BOOL
    enabled = wintypes.BOOL()
    assert user32.SystemParametersInfoW(0x1042, 0, ctypes.byref(enabled), 0)
    text = "A new message arrives without replacing your reading history."
    samples = []
    started = time.monotonic()

    async def sample_positions():
        bubble = None
        while time.monotonic() - started < 0.6:
            if bubble is None:
                matches = [
                    handle
                    for handle, content in chat_message_controls(history, expected_pid=os.getpid())
                    if content == text
                ]
                if matches:
                    bubble = win32gui.GetParent(matches[0])
            if bubble is not None and win32gui.IsWindowVisible(bubble):
                samples.append(
                    {
                        "elapsed": time.monotonic() - started,
                        "bounds": list(win32gui.GetWindowRect(bubble)),
                    }
                )
            await asyncio.sleep(0.006)

    sampling = asyncio.create_task(sample_positions())
    try:
        await client.call_tool("Transcript", {"title": "Owned arrival animation", "text": text})
    finally:
        await sampling
    assert samples, "The new native message never became visible"
    positions = {sample["bounds"][0] for sample in samples}
    if enabled.value:
        assert len(positions) > 1, "The native message appeared without an arrival transition"
    else:
        assert len(positions) == 1, "The Windows reduced-motion preference was ignored"
    settled = samples[-5:]
    assert len({tuple(sample["bounds"]) for sample in settled}) == 1
    evidence["arrival"] = {
        "client_animations_enabled": bool(enabled.value),
        "distinct_horizontal_positions": len(positions),
        "samples": samples,
        "scope": "Native message-window movement, not a compositor FPS measurement",
    }


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
        panel, history, composer = surface._panel, surface._history_window, surface._composer
        win32gui.ShowWindow(application.surface.window_handles()[0], win32con.SW_SHOWMINNOACTIVE)
        win32gui.SendMessage(panel, win32con.WM_COMMAND, 201, win32gui.GetDlgItem(panel, 201))
        win32gui.SetWindowPos(
            fixture.hwnd,
            panel,
            0,
            0,
            0,
            0,
            win32con.SWP_NOMOVE | win32con.SWP_NOSIZE | win32con.SWP_NOACTIVATE,
        )
        async with Client(create_server(application, manage_application=False)) as client:
            await client.call_tool(
                "Transcript",
                {
                    "title": "Owned resize/scroll fixture",
                    "text": "\n".join(
                        f"History line {number:03}: known text" for number in range(200)
                    ),
                },
            )
            for number in range(1, 8):
                await client.call_tool(
                    "Transcript",
                    {
                        "title": "Owned history context",
                        "text": f"Context message {number}: known fixture content.",
                    },
                )
            await asyncio.sleep(0.25)
            _history_edge(panel)
            history_text = _message_editor(history, "History line 000")
            draft = "\r\n".join(f"Draft line {number:03}: not sent" for number in range(100))
            win32gui.SetWindowText(composer, draft)
            assert control_text(composer) == draft
            for handle, line in ((history_text, 30), (composer, 10)):
                offset = win32gui.SendMessage(handle, win32con.EM_LINEINDEX, line, 0)
                win32gui.SendMessage(handle, win32con.EM_SETSEL, offset + 2, offset + 12)
                state = edit_state(handle)
                win32gui.SendMessage(handle, win32con.EM_LINESCROLL, 0, line - state["first_line"])
            expected = {handle: _read_anchor(handle) for handle in (history_text, composer)}
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
            sizes = (COMPACT_SIZE, (900, 190), (640, 260), (380, 340), (1120, 440), COMPACT_SIZE)
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
                            panel,
                            0,
                            left,
                            top,
                            width,
                            height,
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
                        panel,
                        None,
                        None,
                        win32con.RDW_INVALIDATE
                        | win32con.RDW_ERASE
                        | win32con.RDW_ALLCHILDREN
                        | win32con.RDW_UPDATENOW,
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
            # Local scrollbar presses may activate this owned transcript, unlike agent replies.
            after_local_scroll = win32gui.GetForegroundWindow()
            assert after_local_scroll in (foreground, panel)
            await _exercise_arrival(client, application, evidence)
            assert control_text(composer) == draft
            assert win32gui.GetForegroundWindow() == after_local_scroll
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
