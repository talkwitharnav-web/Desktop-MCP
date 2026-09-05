import ctypes
from types import SimpleNamespace

import pytest

from desktop_mcp.contracts import INJECTED_INPUT_TAG
from desktop_mcp.native import WindowsInput, normalize_absolute


@pytest.mark.parametrize(
    "bounds,point",
    [
        ((0, 0, 1920, 1080), (0, 0)),
        ((0, 0, 1920, 1080), (1919, 1079)),
        ((-1920, -200, 2560, 1440), (-1900, -100)),
        ((-1920, -200, 2560, 1440), (2000, 1300)),
    ],
)
def test_absolute_normalization_hits_pixel_centers(bounds, point):
    x, y = normalize_absolute(point, bounds)
    width, height = bounds[2] - bounds[0], bounds[3] - bounds[1]
    assert bounds[0] + x * width // 65536 == point[0]
    assert bounds[1] + y * height // 65536 == point[1]


def test_absolute_normalization_rejects_outside_coordinates():
    with pytest.raises(ValueError):
        normalize_absolute((1920, 0), (0, 0, 1920, 1080))


@pytest.fixture
def packets():
    import windows_mcp.uia as uia
    from windows_mcp.uia.enums import INPUT

    backend = WindowsInput.__new__(WindowsInput)
    backend._uia = uia
    backend._input_type = INPUT
    backend._pending_releases = []
    captured = []
    backend._send = lambda events: captured.extend(events)
    return backend, captured


def test_unicode_packets_preserve_surrogate_pairs_and_mark_injected_input(packets):
    backend, events = packets
    backend.text("\u03bb\U0001f369")
    down = [event for event in events if not event.union.ki.dwFlags & 2]
    raw = b"".join(event.union.ki.wScan.to_bytes(2, "little") for event in down)
    assert raw.decode("utf-16-le") == "\u03bb\U0001f369"
    assert len(events) == 6
    for event in events:
        assert ctypes.cast(event.union.ki.dwExtraInfo, ctypes.c_void_p).value == INJECTED_INPUT_TAG


def test_native_scroll_has_real_horizontal_and_vertical_flags(packets):
    backend, events = packets
    backend.wheel(120, -240)
    assert [event.union.mi.dwFlags for event in events] == [0x0800, 0x1000]
    assert [event.union.mi.mouseData for event in events] == [(-240 & 0xFFFFFFFF), 120]


def test_partial_text_send_releases_the_unmatched_key_down(packets):
    backend, captured = packets
    backend.text("a")
    calls = []

    def send(count, events, size):
        calls.append([events[index] for index in range(count)])
        return 1

    backend._user32 = SimpleNamespace(SendInput=send)
    with pytest.raises(OSError, match="accepted 1 of 2"):
        WindowsInput._send(backend, captured)
    assert len(calls) == 2
    assert calls[1][0].union.ki.dwFlags & 0x0002
    assert backend._pending_releases == []
