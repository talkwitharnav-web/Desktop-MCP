"""Read-only pixel/state probes for explicitly owned native repaint fixtures."""

from __future__ import annotations

import ctypes
from ctypes import wintypes
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw

from tests.desktop_live_fixture import owned_window_pid


def edit_state(handle: int) -> dict[str, object]:
    import win32con
    import win32gui

    owned_window_pid(handle)
    start, end = wintypes.DWORD(), wintypes.DWORD()
    win32gui.SendMessage(handle, win32con.EM_GETSEL, ctypes.addressof(start), ctypes.addressof(end))
    return {
        "first_line": win32gui.SendMessage(handle, win32con.EM_GETFIRSTVISIBLELINE, 0, 0),
        "line_count": win32gui.SendMessage(handle, win32con.EM_GETLINECOUNT, 0, 0),
        "selection": (start.value, end.value),
    }


def capture_without_repaint(panel: int, backdrop: int, path: Path) -> tuple[int, int, int, int]:
    """Capture the actual dirty/clean result, never repairing it before inspection."""
    import mss
    import win32gui

    from tests.test_desktop_live import assert_owned_region, own_capture_bounds

    owned_window_pid(panel)
    owned_window_pid(backdrop)
    assert win32gui.IsWindowVisible(panel) and not win32gui.IsIconic(panel)
    assert win32gui.IsWindowVisible(backdrop) and not win32gui.IsIconic(backdrop)
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.GetWindowDisplayAffinity.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user32.GetWindowDisplayAffinity.restype = wintypes.BOOL
    for handle in (panel, backdrop):
        affinity = wintypes.DWORD()
        assert user32.GetWindowDisplayAffinity(handle, ctypes.byref(affinity))
        assert affinity.value == 0, "Only the explicit appearance fixture may expose its pixels"
    bounds, safe = own_capture_bounds(panel), own_capture_bounds(backdrop)
    left, top, right, bottom = bounds
    assert safe[0] <= left < right <= safe[2] and safe[1] <= top < bottom <= safe[3]
    assert_owned_region(panel, bounds)
    assert_owned_region(backdrop, bounds, allowed_above=(panel,))
    dwm = ctypes.WinDLL("dwmapi", use_last_error=True)
    dwm.DwmFlush.restype = ctypes.c_long
    assert dwm.DwmFlush() >= 0
    with mss.MSS() as capture:
        pixels = capture.grab(
            {"left": left, "top": top, "width": right - left, "height": bottom - top}
        )
        with Image.frombytes("RGB", pixels.size, pixels.rgb) as image:
            assert sum(image.convert("L").histogram()[:80]) > image.width * image.height / 4
            image.save(path, format="PNG")
    return bounds


def _gui_state(panel: int):
    import win32process

    from desktop_mcp.window_targets import GUIThreadInfo

    owned_window_pid(panel)
    thread = win32process.GetWindowThreadProcessId(panel)[0]
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.GetGUIThreadInfo.argtypes = [wintypes.DWORD, ctypes.POINTER(GUIThreadInfo)]
    user32.GetGUIThreadInfo.restype = wintypes.BOOL
    state = GUIThreadInfo(cbSize=ctypes.sizeof(GUIThreadInfo))
    assert user32.GetGUIThreadInfo(thread, ctypes.byref(state))
    return state


def capture_owner(panel: int) -> int:
    return int(_gui_state(panel).hwndCapture or 0)


def caret_bounds(panel: int) -> tuple[int, int, int, int] | None:
    import win32gui

    state = _gui_state(panel)
    handle = int(state.hwndCaret or 0)
    if not handle or not win32gui.IsChild(panel, handle):
        return None
    owned_window_pid(handle)
    left, top = win32gui.ClientToScreen(handle, (state.rcCaret.left, state.rcCaret.top))
    right, bottom = win32gui.ClientToScreen(handle, (state.rcCaret.right, state.rcCaret.bottom))
    return left - 2, top - 2, right + 2, bottom + 2


def assert_repaint_matches(
    before: Path,
    after: Path,
    bounds: tuple[int, int, int, int],
    *,
    excluded: tuple[tuple[int, int, int, int], ...] = (),
) -> dict[str, int]:
    with Image.open(before) as natural, Image.open(after) as forced:
        assert natural.size == forced.size
        difference = ImageChops.difference(natural.convert("RGB"), forced.convert("RGB"))
        red, green, blue = difference.split()
        maximum = ImageChops.lighter(ImageChops.lighter(red, green), blue)
        changed = maximum.point(lambda value: 255 if value > 4 else 0)
        mask = Image.new("L", natural.size, 255)
        drawing = ImageDraw.Draw(mask)
        for left, top, right, bottom in excluded:
            drawing.rectangle(
                (left - bounds[0], top - bounds[1], right - bounds[0], bottom - bounds[1]),
                fill=0,
            )
        count = ImageChops.multiply(changed, mask).histogram()[255]
        checked = mask.histogram()[255]
        if count > max(20, checked // 4000):
            changed.save(before.with_name(before.stem + "-repaint-difference.png"))
        assert count <= max(20, checked // 4000), (
            f"{count}/{checked} native pixels changed only after a forced erase/repaint; "
            "normal resize/reflow did not produce a clean frame"
        )
        return {"changed_pixels": count, "checked_pixels": checked}


def scrollbar_from_pixels(
    path: Path,
    capture_bounds: tuple[int, int, int, int],
    bar_bounds: tuple[int, int, int, int],
    dpi: int,
) -> dict[str, object]:
    """Verify visible dark pixels and locate the actual thumb, not a style flag."""
    left, top, right, bottom = bar_bounds
    width, height = right - left, bottom - top
    assert 0 < width <= round(10 * dpi / 96), "The replacement scrollbar is not slim"
    assert height > round(12 * dpi / 96)
    with Image.open(path) as image:
        crop = image.crop(
            (left - capture_bounds[0], top - capture_bounds[1],
             right - capture_bounds[0], bottom - capture_bounds[1])
        ).convert("L")
        histogram = crop.histogram()
        mean = sum(value * count for value, count in enumerate(histogram)) / (width * height)
        assert mean < 140, "The visible scrollbar still looks like the light stock control"
        assert sum(histogram[225:]) < width * height / 20
        column = [crop.getpixel((width // 2, y)) for y in range(height)]
        threshold = (min(column) + max(column)) / 2
        assert max(column) - min(column) >= 20, "The scrollbar thumb is not distinguishable"
        runs = []
        start = None
        for index, value in enumerate((*column, -1)):
            if value > threshold and start is None:
                start = index
            elif value <= threshold and start is not None:
                runs.append((start, index))
                start = None
        first, last = max(runs, key=lambda run: run[1] - run[0])
        assert last - first >= round(8 * dpi / 96), "The visible thumb is too small to grab"
        return {
            "width": width,
            "mean_luminance": mean,
            "thumb_point": (width // 2, (first + last) // 2),
        }
