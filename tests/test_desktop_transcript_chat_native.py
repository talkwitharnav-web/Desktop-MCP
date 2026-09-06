"""In-memory Win32 message fixtures only; this module never creates native windows."""

from bisect import bisect_right
import ctypes
from ctypes import wintypes
from types import SimpleNamespace

import pytest
import win32con as con
import win32gui

from desktop_mcp.transcript_chat import ChatView, MessageView, ReadingAnchor, utf16_length
from desktop_mcp.transcript_chat_native import NativeChatHistory
from desktop_mcp.transcript_scroll import (
    MIN_THUMB_DIP,
    SCROLLBAR_DIP,
    WHEEL_PAGESCROLL,
    thumb_geometry,
)


class FakeWin32:
    """An owned object graph, including native wrapping/selection/scroll state."""

    def __init__(self):
        self.windows = {
            1: dict(
                kind="parent",
                parent=0,
                text="",
                position=(-1920, -480, 900, 600),
                style=con.WS_VISIBLE,
                extra=0,
                visible=True,
            )
        }
        self.classes = {}
        self.subclasses = {}
        self.next_handle = 100
        self.objects = set()
        self.object_colors = {}
        self.deleted = []
        self.events = []
        self.focus = self.capture = 0
        self.keys = {}
        self.pitches = {700: 22, 701: 35, 702: 14}
        self.wheel_lines = 3
        self.notifications = 0
        self.clipboard_text = ""
        self.fail_create = None
        self.selected_objects = {}
        self.WNDCLASS = SimpleNamespace

    def _handle(self):
        self.next_handle += 1
        return self.next_handle

    def RGB(self, red, green, blue):
        return red | green << 8 | blue << 16

    def CreateSolidBrush(self, color):
        handle = self._handle()
        self.objects.add(handle)
        self.object_colors[handle] = color
        return handle

    def CreatePen(self, style, width, color):
        return self.CreateSolidBrush(color)

    def DeleteObject(self, handle):
        assert handle in self.objects
        self.objects.remove(handle)
        self.object_colors.pop(handle)
        self.deleted.append(handle)

    def LoadCursor(self, instance, cursor):
        return 9

    def RegisterClass(self, description):
        self.classes[description.lpszClassName] = description.lpfnWndProc

    def UnregisterClass(self, name, instance):
        assert not any(item["kind"] == name for item in self.windows.values())
        del self.classes[name]

    def CreateWindowEx(
        self, extra, kind, text, style, x, y, width, height, parent, identifier, instance, data
    ):
        if self.fail_create in (kind, (kind, identifier)):
            raise OSError("Synthetic control allocation failure")
        handle = self._handle()
        self.windows[handle] = dict(
            kind=kind,
            text=text,
            style=style,
            extra=extra,
            parent=parent,
            identifier=identifier,
            position=(x, y, width, height),
            visible=bool(style & con.WS_VISIBLE),
            first=0,
            selection=(0, 0),
            selection_anchor=0,
            selection_active=0,
            font=700,
            scrollbar=False,
            line_cache=None,
        )
        self.events.append(("create", handle, kind))
        return handle

    def DestroyWindow(self, handle):
        for child, item in tuple(self.windows.items()):
            if item["parent"] == handle:
                self.DestroyWindow(child)
        self.SendMessage(handle, con.WM_NCDESTROY, 0, 0)
        self.events.append(("destroy", handle))
        self.windows.pop(handle)
        if self.focus == handle:
            self.focus = 0
        if self.capture == handle:
            self.capture = 0

    def IsWindow(self, handle):
        return handle in self.windows

    def IsWindowVisible(self, handle):
        item = self.windows.get(handle)
        if item is None or not item["visible"]:
            return False
        return not item["parent"] or self.IsWindowVisible(item["parent"])

    def GetClassName(self, handle):
        return self.windows[handle]["kind"]

    def ShowWindow(self, handle, command):
        self.windows[handle]["visible"] = command != con.SW_HIDE
        self.events.append(("show", handle, command))

    def GetClientRect(self, handle):
        item = self.windows[handle]
        _, _, width, height = item["position"]
        if item.get("scrollbar"):
            width = max(0, width - 17)
        return 0, 0, width, height

    def _position(self, handle, x, y, width, height):
        assert -32768 < x < 32768 and -32768 < y < 32768
        assert 0 < width < 32768 and 0 < height < 32768
        self.windows[handle]["position"] = x, y, width, height
        self._clamp(handle)
        self.events.append(("position", handle, x, y, width, height))

    def MoveWindow(self, handle, x, y, width, height, repaint):
        self._position(handle, x, y, width, height)

    def SetWindowPos(self, handle, after, x, y, width, height, flags):
        assert flags & con.SWP_NOACTIVATE
        self._position(handle, x, y, width, height)

    def SetWindowText(self, handle, text):
        self.SendMessage(handle, con.WM_SETTEXT, 0, text)

    def GetWindowText(self, handle):
        return self.windows[handle]["text"]

    def ShowScrollBar(self, handle, bar, shown):
        self.events.append(("stock-scrollbar", handle, bool(shown)))
        self.windows[handle]["scrollbar"] = bool(shown)
        self._clamp(handle)

    def _lines(self, handle):
        item = self.windows[handle]
        pitch = self.pitches.get(item["font"], 22)
        columns = max(1, self.GetClientRect(handle)[2] // max(1, pitch // 2))
        key = item["text"], columns
        if item["line_cache"] is not None and item["line_cache"][0] == key:
            return item["line_cache"][1]
        lines = []
        offset = 0
        for paragraph in item["text"].split("\r\n"):
            lines.append(offset)
            for index, char in enumerate(paragraph):
                if index and index % columns == 0:
                    lines.append(offset)
                offset += utf16_length(char)
            offset += 2
        item["line_cache"] = key, lines
        return lines

    def _maximum(self, handle):
        pitch = self.pitches.get(self.windows[handle]["font"], 22)
        return max(0, len(self._lines(handle)) - max(1, self.GetClientRect(handle)[3] // pitch))

    def _clamp(self, handle):
        item = self.windows[handle]
        if item["kind"] == "EDIT":
            item["first"] = min(max(0, item["first"]), self._maximum(handle))

    def SendMessage(self, handle, message, wparam, lparam):
        if message == con.EM_GETSEL:
            assert wparam and lparam, "Packed EM_GETSEL must never be used"
        if message == con.WM_SETTEXT:
            self.events.append(("text", handle))
        if message == con.EM_SETSEL:
            self.events.append(("selection", handle, wparam, lparam))
        if handle in self.subclasses:
            return self.subclasses[handle](handle, message, wparam, lparam, 0, 0)
        kind = self.windows[handle]["kind"]
        if kind in self.classes:
            return self.classes[kind](handle, message, wparam, lparam)
        return self.native_message(handle, message, wparam, lparam)

    def set_selection(self, handle, anchor, active):
        item = self.windows[handle]
        item["selection_anchor"], item["selection_active"] = anchor, active
        item["selection"] = min(anchor, active), max(anchor, active)

    def native_message(self, handle, message, wparam, lparam):
        item = self.windows[handle]
        if message == con.WM_SETTEXT:
            item["text"] = lparam
            item["first"], item["line_cache"] = 0, None
            self.set_selection(handle, 0, 0)
            return 1
        if message == con.WM_SETFONT:
            item["font"] = wparam
            self._clamp(handle)
        elif message == con.WM_GETTEXTLENGTH:
            return utf16_length(item["text"])
        elif message == con.EM_GETLINECOUNT:
            return len(self._lines(handle))
        elif message == con.EM_GETFIRSTVISIBLELINE:
            return item["first"]
        elif message == con.EM_LINEINDEX:
            lines = self._lines(handle)
            return lines[wparam] if 0 <= wparam < len(lines) else -1
        elif message == con.EM_LINEFROMCHAR:
            return max(0, bisect_right(self._lines(handle), wparam) - 1)
        elif message == con.EM_GETRECT:
            rect = ctypes.cast(lparam, ctypes.POINTER(wintypes.RECT)).contents
            rect.left, rect.top, rect.right, rect.bottom = self.GetClientRect(handle)
        elif message == con.EM_GETSEL:
            start, end = item["selection"]
            assert start <= end, "Native EM_GETSEL returns ordered bounds, not active endpoints"
            ctypes.cast(wparam, ctypes.POINTER(wintypes.DWORD)).contents.value = start
            ctypes.cast(lparam, ctypes.POINTER(wintypes.DWORD)).contents.value = end
            return (start & 0xFFFF) | ((end & 0xFFFF) << 16)
        elif message == con.EM_SETSEL:
            limit = utf16_length(item["text"])
            anchor, active = ctypes.c_int(wparam).value, ctypes.c_int(lparam).value
            if anchor < 0:
                anchor = active = item["selection_active"]
            else:
                anchor = min(anchor, limit)
                active = limit if active < 0 else min(active, limit)
            self.set_selection(handle, anchor, active)
        elif message == con.EM_LINESCROLL:
            item["first"] += lparam
            self._clamp(handle)
        elif message == con.WM_COPY:
            start, end = item["selection"]
            self.clipboard_text = (
                item["text"].encode("utf-16-le")[2 * start : 2 * end].decode("utf-16-le")
            )
        elif message == con.WM_LBUTTONDOWN:
            self.capture = handle
            self.SetFocus(handle)
        elif message == con.WM_KEYDOWN and wparam in (con.VK_LEFT, con.VK_RIGHT):
            if self.GetKeyState(con.VK_SHIFT) < 0:
                active = item["selection_active"] + (-1 if wparam == con.VK_LEFT else 1)
                self.set_selection(
                    handle,
                    item["selection_anchor"],
                    min(max(0, active), utf16_length(item["text"])),
                )
        elif message in (con.WM_LBUTTONUP, con.WM_CANCELMODE):
            if self.capture == handle:
                self.capture = 0
        return 0

    def DefWindowProc(self, handle, message, wparam, lparam):
        return self.native_message(handle, message, wparam, lparam)

    def SetWindowSubclass(self, handle, callback, identifier, data):
        self.subclasses[handle] = callback
        return True

    def RemoveWindowSubclass(self, handle, callback, identifier):
        self.subclasses.pop(handle, None)
        return True

    def DefSubclassProc(self, handle, message, wparam, lparam):
        return self.native_message(handle, message, wparam, lparam)

    def SystemParametersInfoW(self, command, unused, pointer, flags):
        ctypes.cast(pointer, ctypes.POINTER(wintypes.UINT)).contents.value = self.wheel_lines
        return True

    def GetKeyState(self, key):
        return self.keys.get(key, 0)

    def GetFocus(self):
        return self.focus

    def SetFocus(self, handle):
        old, self.focus = self.focus, handle
        self.events.append(("focus", handle))
        if old in self.windows:
            self.SendMessage(old, con.WM_KILLFOCUS, handle, 0)
        self.SendMessage(handle, con.WM_SETFOCUS, old, 0)

    def GetCapture(self):
        return self.capture

    def SetCapture(self, handle):
        self.events.append(("capture", handle))
        self.capture = handle

    def ReleaseCapture(self):
        self.events.append(("release-capture", self.capture))
        previous = self.capture
        self.capture = 0
        if previous in self.windows:
            self.SendMessage(previous, 0x0215, 0, 0)

    def InvalidateRect(self, handle, rect, erase):
        self.events.append(("invalidate", handle))

    def BeginPaint(self, handle):
        return 55, handle

    def EndPaint(self, handle, paint):
        self.events.append(("end-paint", handle))

    def FillRect(self, dc, rect, brush):
        self.events.append(("fill", rect, brush))

    def SelectObject(self, dc, handle):
        previous = self.selected_objects.get(dc, 0)
        self.selected_objects[dc] = handle
        return previous

    def GetStockObject(self, kind):
        return -kind

    def RoundRect(self, dc, *rectangle):
        self.events.append(
            ("rounded", rectangle, self.object_colors.get(self.selected_objects.get(dc)))
        )

    def SetTextColor(self, dc, color):
        self.events.append(("text-color", color))

    def SetBkColor(self, dc, color):
        self.events.append(("background-color", color))


@pytest.fixture
def chat():
    native = FakeWin32()
    changed = []
    errors = []
    history = NativeChatHistory(on_change=lambda: changed.append(True), on_error=errors.append)

    def load():
        history._gui = SimpleNamespace(
            **{name: getattr(native, name) for name in dir(native) if hasattr(win32gui, name)}
        )
        history._api = history._comctl = history._user32 = native
        history._con = con
        history._text_callback = history._text_procedure

    history._load_native = load
    history.create(1, 7, 301)
    history.set_font(700, line_height=22, scale=1)
    history.reflow(480, 260)
    yield history, native, changed, errors
    history.close()
    assert not native.objects
    assert not native.classes
    assert not native.subclasses
    assert not errors


def entries(count, *, start=1, text="A short readable message."):
    return tuple(
        (i, "Assistant", text, "user" if i % 2 == 0 else "assistant")
        for i in range(start, start + count)
    )


def test_genuine_native_role_boxes_text_selection_and_read_only_controls(chat):
    history, native, _, _ = chat
    history.set_entries(
        (
            (1, "Unicode ✓", "Message 😀\n第二行 & <literal>", "assistant"),
            (2, "You", "Yes, thanks!", "user"),
        ),
        animate=False,
    )
    first, second = history._bubbles.values()
    assert native.windows[first.label]["text"] == "Assistant · Unicode ✓"
    assert native.windows[second.label]["text"] == "You"
    assert native.GetWindowText(first.editor) == "Message 😀\r\n第二行 & <literal>"
    for bubble in (first, second):
        control = native.windows[bubble.editor]
        assert control["kind"] == "EDIT"
        assert control["style"] & con.ES_READONLY
        assert control["style"] & con.ES_MULTILINE
        assert control["style"] & con.WS_TABSTOP
        assert not control["style"] & con.ES_AUTOHSCROLL
        assert native.windows[bubble.window]["extra"] & con.WS_EX_CONTROLPARENT
        history._paint(bubble.window)
    assert len([event for event in native.events if event[0] == "rounded"]) == 2
    assert history._boxes[0].x < history._boxes[1].x
    assert set(history.window_handles()) == set(history.window_roles())
    assert len(history.window_handles()) == 10
    native.SendMessage(first.editor, con.EM_SETSEL, 2, 10)
    assert history.capture_view().messages[0].selection == (2, 10)
    assert not history.following


def test_append_does_not_replace_previous_text_or_steal_selection_focus_or_anchor(chat):
    history, native, _, _ = chat
    original = entries(9, text="One two three four\n" * 20)
    history.set_entries(original, animate=False)
    history.scroll_to(300)
    bubble = history._bubbles[3]
    native.SetFocus(bubble.editor)
    native.SendMessage(bubble.editor, con.EM_SETSEL, 4, 58)
    view = history.capture_view()
    handles = history.window_handles()
    native.events.clear()
    history.set_entries((*original, (10, "Assistant", "A new reply", "assistant")), now=10)
    after = history.capture_view()
    assert after.anchor == view.anchor
    assert after.messages[:9] == view.messages
    assert not history.animation_active
    assert history.unread and not history.following
    assert native.GetFocus() == bubble.editor
    assert all(event[1] not in handles for event in native.events if event[0] == "text")
    assert not any(event[0] == "focus" for event in native.events)


def test_long_unicode_history_is_complete_bounded_and_survives_pruning(chat):
    history, native, _, _ = chat
    original = entries(32, text="😀" * 16_000)
    history.set_entries(original, animate=False)
    assert (
        sum(utf16_length(native.GetWindowText(b.editor)) for b in history._bubbles.values())
        == 1_024_000
    )
    for bubble in history._bubbles.values():
        assert not native.windows[bubble.editor]["scrollbar"]
        assert bubble.scroll_visible
        assert native.windows[bubble.editor]["position"][3] <= 300
    retained = history._bubbles[20]
    history.scroll_to(history._boxes[19].y)
    native.SendMessage(retained.editor, con.EM_SETSEL, 29_990, 30_000)
    native.SendMessage(retained.editor, con.EM_LINESCROLL, 0, 700)
    saved = next(item for item in history.capture_view().messages if item.sequence == 20)
    history.set_entries(entries(32, start=2, text="😀" * 16_000), now=20)
    assert history._bubbles[20] is retained
    restored = next(item for item in history.capture_view().messages if item.sequence == 20)
    assert restored == saved
    assert 1 not in history._bubbles and len(history._bubbles) == 32
    assert len(history.window_handles()) == 130
    assert not history.animation_active


def test_internal_text_scroll_reaches_both_ends_without_giant_children(chat):
    history, native, _, _ = chat
    history.set_entries(entries(3, text="line\n" * 3000), animate=False)
    bubble = history._bubbles[3]
    assert history._text_scroll_state(bubble).at_end
    native.SendMessage(bubble.editor, con.EM_LINESCROLL, 0, -100_000)
    assert not history.following
    history._wheel_text(bubble, -120)
    assert history._text_scroll_state(bubble).position == 3
    assert history._text_scroll_state(bubble).maximum > 2900
    history.latest()
    assert history._text_scroll_state(bubble).at_end
    assert history.following


def test_reflow_keeps_desired_reading_position_when_a_large_viewport_clamps_it(chat):
    history, native, _, _ = chat
    history.set_entries(entries(10), animate=False)
    history.scroll_to(170)
    bubble = history._bubbles[4]
    native.SendMessage(bubble.editor, con.EM_SETSEL, 1, 7)
    before = history.capture_view()
    history.reflow(900, 2000)
    assert history.scroll_state().maximum == 0
    assert not history.following
    history.reflow(480, 260)
    after = history.capture_view()
    assert after.anchor == before.anchor
    assert after.messages == before.messages
    assert not history.following
    assert history.scroll_state().position == 170


def test_font_and_width_reflow_preserve_native_utf16_anchor_and_selection(chat):
    history, native, _, _ = chat
    history.set_entries(entries(5, text="漢字 😀 more text\n" * 400), animate=False)
    history.scroll_to(500)
    bubble = history._bubbles[3]
    native.SendMessage(bubble.editor, con.EM_LINESCROLL, 0, 80)
    native.SendMessage(bubble.editor, con.EM_SETSEL, 1024, 1099)
    view = history.capture_view()
    history.set_font(701, line_height=35, scale=1.5)
    history.reflow(190, 175)
    history.set_font(700, line_height=22, scale=1)
    history.reflow(480, 260)
    after = history.capture_view()
    assert after.messages == view.messages
    assert after.anchor == view.anchor
    assert native.windows[bubble.editor]["font"] == 700
    assert native.windows[bubble.editor]["position"][2] <= 480
    assert 700 not in native.deleted and 701 not in native.deleted


def test_arrival_is_follow_only_immediate_native_text_and_timer_bounded(chat):
    history, native, _, _ = chat
    history.set_entries((), now=1)
    history.set_entries(entries(1), now=10)
    assert history.animation_active
    bubble = history._bubbles[1]
    assert native.GetWindowText(bubble.editor) == "A short readable message."
    initial = native.windows[bubble.window]["position"]
    history.tick(10.06)
    middle = native.windows[bubble.window]["position"]
    assert initial[0] <= middle[0]
    history.tick(10.20)
    final = native.windows[bubble.window]["position"]
    assert middle[0] <= final[0]
    assert not history.animation_active
    native.events.clear()
    assert not history.tick(11)
    assert native.events == []
    history.set_entries(entries(1), now=12)
    assert not history.animation_active
    history.set_entries(entries(2), now=13, animate=False)
    assert not history.animation_active
    history.reflow(490, 280)
    assert not history.animation_active


def test_initial_population_show_prune_and_non_following_do_not_replay_arrivals(chat):
    history, native, _, _ = chat
    history.set_entries(entries(12), now=1)
    assert not history.animation_active
    history.set_entries(entries(11, start=2), now=2)
    assert not history.animation_active
    history.scroll_to(0)
    history.set_entries(entries(12, start=2), now=3)
    assert not history.animation_active and history.unread
    history.latest()
    native.ShowWindow(1, con.SW_HIDE)
    history.set_entries(entries(13, start=2), now=4)
    assert not history.animation_active
    native.ShowWindow(1, con.SW_SHOWNA)
    history.set_entries(entries(13, start=2), now=5)
    assert not history.animation_active
    assert history.following and not history.unread


def test_external_drag_and_native_selection_block_arrival_and_follow(chat):
    history, native, _, _ = chat
    history.set_entries(entries(3), animate=False)
    history.set_interacting(True)
    before = history.scroll_state().position
    history.set_entries(entries(4), now=10)
    assert history.scroll_state().position == before
    assert not history.animation_active and history.unread
    history.set_interacting(False)
    history.latest()
    bubble = history._bubbles[4]
    native.SendMessage(bubble.editor, con.WM_LBUTTONDOWN, 0, 0)
    history.set_entries(entries(5), now=11)
    assert not history.animation_active and not history.following
    history.cancel_interaction()
    assert native.GetCapture() == 0


def test_scroll_commands_wheel_accumulation_page_preference_and_native_spillover(chat):
    history, native, _, _ = chat
    history.set_entries(entries(12, text="long message " * 100), animate=False)
    history.scroll_to(1000)
    history.wheel(60)
    assert history.scroll_state().position == 1000
    history.wheel(60)
    assert history.scroll_state().position == 934
    history.wheel(120, 0)
    assert history.scroll_state().position == 934
    history.wheel(-120, WHEEL_PAGESCROLL)
    assert history.scroll_state().position == 1172
    assert history.scroll_command(con.SB_TOP)
    assert history.scroll_state().position == 0
    assert not history.scroll_command(con.SB_THUMBTRACK)
    history.scroll_command(con.SB_BOTTOM)
    assert history.scroll_state().at_end
    history.scroll_to(500)
    bubble = history._bubbles[1]
    native.SendMessage(bubble.editor, con.EM_LINESCROLL, 0, -100_000)
    history._wheel_text(bubble, 120)
    assert history.scroll_state().position == 434


def test_keyboard_visits_offscreen_messages_and_select_all_uses_native_buffer(chat):
    history, native, _, _ = chat
    history.set_entries(entries(20), animate=False)
    last = history._bubbles[20]
    native.SetFocus(last.editor)
    native.keys[con.VK_CONTROL] = -1
    native.SendMessage(last.editor, con.WM_KEYDOWN, con.VK_PRIOR, 0)
    previous = history._bubbles[19]
    assert native.GetFocus() == previous.editor
    assert native.IsWindowVisible(previous.editor)
    native.SendMessage(previous.editor, con.WM_KEYDOWN, ord("A"), 0)
    assert native.windows[previous.editor]["selection"] == (
        0,
        utf16_length(native.GetWindowText(previous.editor)),
    )
    assert not history.following
    native.SendMessage(previous.editor, con.WM_COPY, 0, 0)
    assert native.clipboard_text == native.GetWindowText(previous.editor)
    assert native.SendMessage(history.hwnd, con.WM_GETDLGCODE, 0, 0) & con.DLGC_WANTARROWS


def test_cancel_never_releases_unowned_capture_and_close_is_idempotent(chat):
    history, native, _, _ = chat
    history.set_entries(entries(4), animate=False)
    native.capture = 999
    history.cancel_interaction()
    assert native.capture == 999
    native.capture = history.hwnd
    history.cancel_interaction()
    assert native.capture == 0
    history.close()
    history.close()
    assert history.window_roles() == {}
    assert native.windows.keys() == {1}


def test_invalid_input_is_rejected_before_changing_any_native_text(chat):
    history, native, _, _ = chat
    history.set_entries(entries(2), animate=False)
    before = {handle: value["text"] for handle, value in native.windows.items()}
    with pytest.raises(ValueError):
        history.set_entries(((1, "Assistant", "x" * 16001, "assistant"),), now=3)
    assert {handle: value["text"] for handle, value in native.windows.items()} == before


def test_restore_pruned_message_uses_surviving_anchor_and_never_focuses_composer(chat):
    history, native, _, _ = chat
    history.set_entries(entries(5), animate=False)
    view = ChatView(ReadingAnchor(1, 10), (MessageView(1, 2, (2, 5)),), False)
    history.set_entries(entries(5, start=2), animate=False)
    native.events.clear()
    history.restore_view(view)
    assert history.capture_view().anchor == ReadingAnchor(2, 0)
    assert not history.following
    assert not any(event[0] == "focus" for event in native.events)


def test_borrowed_font_change_invalidates_real_children_even_when_pitch_is_unchanged(chat):
    history, native, _, _ = chat
    history.set_entries(entries(2), animate=False)
    native.pitches[703] = 22
    native.events.clear()
    history.set_font(703, line_height=22)
    invalidated = {event[1] for event in native.events if event[0] == "invalidate"}
    for bubble in history._bubbles.values():
        assert {bubble.window, bubble.label, bubble.editor} <= invalidated
    assert 703 not in native.deleted


def test_close_and_recreate_keeps_the_same_api_without_stale_text_cache(chat):
    history, native, _, _ = chat
    original = entries(3)
    history.set_entries(original, animate=False)
    history.close()
    history.create(1, 7, 301)
    history.set_font(700, line_height=22)
    history.reflow(480, 260)
    history.set_entries(original, now=10)
    assert len(history._bubbles) == 3
    assert all(
        native.GetWindowText(bubble.editor) == item[2]
        for item, bubble in zip(original, history._bubbles.values())
    )
    assert not history.animation_active


def test_partial_native_creation_failure_terminally_closes_owned_resources(chat):
    history, native, _, _ = chat
    history.set_entries(entries(1), animate=False)
    native.fail_create = "EDIT"
    with pytest.raises(OSError, match="Synthetic"):
        history.set_entries(entries(2), now=2)
    assert not history.hwnd
    assert history.window_roles() == {}
    assert history.capture_view() == ChatView()
    history._render()
    history.close()
    assert native.windows.keys() == {1}
    assert not native.objects


def test_pruned_pointer_owner_cannot_leave_following_permanently_blocked(chat):
    history, native, _, _ = chat
    history.set_entries(entries(3), animate=False)
    first = history._bubbles[1]
    native.SendMessage(first.editor, con.WM_LBUTTONDOWN, 0, 0)
    history.set_entries(entries(3, start=2), now=2)
    assert not history._pointer_down
    assert history._pointer_window == 0
    assert native.IsWindowVisible(native.GetFocus())
    history.latest()
    assert history.following


def test_reading_gap_position_is_preserved_and_empty_history_clears_unread(chat):
    history, _, _, _ = chat
    history.set_entries(entries(7), animate=False)
    position = history._boxes[2].bottom + 3
    history.scroll_to(position)
    history.set_entries(entries(8), now=2)
    assert history.scroll_state().position == position
    assert history.unread
    history.set_entries((), animate=False)
    assert not history.unread and history.following


def test_old_ids_never_replay_and_input_order_defines_geometry(chat):
    history, _, _, _ = chat
    history.set_entries(entries(3, start=2), now=1)
    retained = history._bubbles[2]
    history.set_entries(entries(4), now=2)
    assert [box.sequence for box in history._boxes] == [1, 2, 3, 4]
    assert history._bubbles[2] is retained
    assert not history.animation_active
    history.set_entries((), now=3)
    history.set_entries(entries(4), now=4)
    assert not history.animation_active


def point(x, y):
    return (x & 0xFFFF) | ((y & 0xFFFF) << 16)


def grab_thumb(history, native, bubble, fraction=0.7):
    state = history._text_scroll_state(bubble)
    thumb = thumb_geometry(state, native.GetClientRect(bubble.scrollbar)[3], history._scale)
    y = thumb.top + min(thumb.length - 1, round(thumb.length * fraction))
    native.SendMessage(bubble.scrollbar, con.WM_LBUTTONDOWN, con.MK_LBUTTON, point(3, y))
    return y


@pytest.mark.parametrize("scale", [1, 1.5, 2, 3])
@pytest.mark.parametrize("height_dip", [75, 260])
def test_long_message_bars_are_owned_dark_slim_and_never_stock_chrome(chat, scale, height_dip):
    history, native, _, _ = chat
    native.pitches[900] = round(22 * scale)
    history.set_font(900, line_height=native.pitches[900], scale=scale)
    history.reflow(round(480 * scale), round(height_dip * scale))
    text = "Native Unicode 😀\n" * 500
    history.set_entries(entries(2, text=text), animate=False)
    objects = set(native.objects)
    for bubble in history._bubbles.values():
        assert bubble.scroll_visible
        assert not native.windows[bubble.editor]["scrollbar"]
        assert not native.windows[bubble.editor]["style"] & con.WS_VSCROLL
        assert native.GetWindowText(bubble.editor) == text.replace("\n", "\r\n")
        assert history.window_roles()[bubble.scrollbar] == "transcript-history-message-scrollbar"
        assert native.windows[bubble.scrollbar]["text"] == "Message scroll"
        assert native.windows[bubble.scrollbar]["style"] & con.WS_TABSTOP
        editor_x, editor_y, editor_width, editor_height = native.windows[bubble.editor]["position"]
        bar_x, bar_y, bar_width, bar_height = native.windows[bubble.scrollbar]["position"]
        assert bar_width == round(SCROLLBAR_DIP * scale)
        assert editor_x + editor_width == bar_x
        assert editor_y == bar_y and editor_height == bar_height
        state = history._text_scroll_state(bubble)
        thumb = thumb_geometry(state, bar_height, scale)
        minimum = min(round(MIN_THUMB_DIP * scale), max(1, thumb.track_length - 1))
        assert thumb.length >= minimum
        assert thumb.bottom <= bar_height
        before = len(native.events)
        native.SendMessage(bubble.scrollbar, con.WM_PAINT, 0, 0)
        rounded = [event for event in native.events[before:] if event[0] == "rounded"]
        assert len(rounded) == 2
        for _, (left, top, right, bottom, _, _), color in rounded:
            assert right - left == round(6 * scale)
            assert 0 <= left < right <= bar_width and 0 <= top < bottom <= bar_height
            assert color in {
                native.object_colors[history._brushes["scroll-track"]],
                native.object_colors[history._brushes["scroll-thumb"]],
            }
            assert all((color >> shift) & 255 < 160 for shift in (0, 8, 16))
    assert native.objects == objects
    assert not any(event[2] for event in native.events if event[0] == "stock-scrollbar")


def test_inner_line_page_home_end_and_keyboard_leave_native_selection_intact(chat):
    history, native, _, _ = chat
    history.set_entries(entries(1, text="line 😀\n" * 500), animate=False)
    bubble = history._bubbles[1]
    native.SendMessage(bubble.editor, con.EM_SETSEL, 40, 80)
    maximum = history._text_scroll_state(bubble).maximum
    bar = bubble.scrollbar
    native.SendMessage(bar, con.WM_KEYDOWN, con.VK_HOME, 0)
    assert history._text_scroll_state(bubble).position == 0
    native.SendMessage(bar, con.WM_KEYDOWN, con.VK_DOWN, 0)
    assert history._text_scroll_state(bubble).position == 1
    native.SendMessage(bar, con.WM_VSCROLL, con.SB_LINEDOWN, 0)
    assert history._text_scroll_state(bubble).position == 2
    page = history._text_scroll_state(bubble).page_step
    native.SendMessage(bar, con.WM_KEYDOWN, con.VK_NEXT, 0)
    assert history._text_scroll_state(bubble).position == 2 + page
    native.SendMessage(bar, con.WM_VSCROLL, con.SB_PAGEUP, 0)
    assert history._text_scroll_state(bubble).position == 2
    native.SendMessage(bar, con.WM_KEYDOWN, con.VK_UP, 0)
    assert history._text_scroll_state(bubble).position == 1
    native.SendMessage(bar, con.WM_KEYDOWN, con.VK_END, 0)
    assert history._text_scroll_state(bubble).position == maximum
    assert native.windows[bubble.editor]["selection"] == (40, 80)
    assert not history.following
    assert native.SendMessage(bar, con.WM_GETDLGCODE, 0, 0) & con.DLGC_WANTARROWS


def test_inner_thumb_grab_does_not_jump_and_reaches_both_ends_with_signed_client_points(chat):
    history, native, _, _ = chat
    history.set_entries(entries(1, text="line\n" * 3000), animate=False)
    bubble = history._bubbles[1]
    history._scroll_text_to(bubble, 1000)
    native.SendMessage(bubble.editor, con.EM_SETSEL, 100, 130)
    position = history._text_scroll_state(bubble).position
    y = grab_thumb(history, native, bubble)
    assert native.GetCapture() == bubble.scrollbar
    assert history._inner_scroll.held == bubble.scrollbar
    assert history._text_scroll_state(bubble).position == position
    assert not history.following
    native.SendMessage(bubble.scrollbar, con.WM_MOUSEMOVE, con.MK_LBUTTON, point(-20, -100))
    assert history._text_scroll_state(bubble).position == 0
    height = native.GetClientRect(bubble.scrollbar)[3]
    native.SendMessage(bubble.scrollbar, con.WM_MOUSEMOVE, con.MK_LBUTTON, point(30, height + 100))
    assert history._text_scroll_state(bubble).at_end
    native.SendMessage(bubble.scrollbar, con.WM_LBUTTONUP, 0, point(30, height + 100))
    assert native.GetCapture() == 0 and history._inner_scroll.held == 0
    assert native.windows[bubble.editor]["selection"] == (100, 130)
    assert y >= 0


@pytest.mark.parametrize("fraction", [0.0, 0.5, 0.95])
@pytest.mark.parametrize("gesture", ["release", "stationary-move", "away-and-back"])
def test_inner_thumb_preserves_unrounded_position_at_its_grab_origin(chat, fraction, gesture):
    history, native, _, _ = chat
    history.set_entries(entries(1, text="line\n" * 3000), animate=False)
    bubble = history._bubbles[1]
    history._scroll_text_to(bubble, 1000)
    y = grab_thumb(history, native, bubble, fraction)
    if gesture == "away-and-back":
        native.SendMessage(bubble.scrollbar, con.WM_MOUSEMOVE, con.MK_LBUTTON, point(3, y + 8))
        assert history._text_scroll_state(bubble).position > 1000
    if gesture != "release":
        native.SendMessage(bubble.scrollbar, con.WM_MOUSEMOVE, con.MK_LBUTTON, point(3, y))
    native.SendMessage(bubble.scrollbar, con.WM_LBUTTONUP, 0, point(3, y))
    assert history._text_scroll_state(bubble).position == 1000
    assert native.GetCapture() == 0


def test_inner_track_pages_once_without_turning_the_click_into_a_thumb_jump(chat):
    history, native, _, _ = chat
    history.set_entries(entries(1, text="line\n" * 500), animate=False)
    bubble = history._bubbles[1]
    history._scroll_text_to(bubble, 150)
    state = history._text_scroll_state(bubble)
    thumb = thumb_geometry(state, native.GetClientRect(bubble.scrollbar)[3], history._scale)
    y = thumb.bottom + 5
    native.SendMessage(bubble.scrollbar, con.WM_LBUTTONDOWN, con.MK_LBUTTON, point(3, y))
    assert history._text_scroll_state(bubble).position == 150 + state.page_step
    assert history._inner_scroll.held == bubble.scrollbar and not history.following
    native.SendMessage(bubble.scrollbar, con.WM_LBUTTONUP, 0, point(3, y))
    assert history._text_scroll_state(bubble).position == 150 + state.page_step
    thumb = thumb_geometry(
        history._text_scroll_state(bubble), native.GetClientRect(bubble.scrollbar)[3], 1
    )
    y = thumb.top - 3
    native.SendMessage(bubble.scrollbar, con.WM_LBUTTONDOWN, con.MK_LBUTTON, point(3, y))
    native.SendMessage(bubble.scrollbar, con.WM_LBUTTONUP, 0, point(3, y))
    assert history._text_scroll_state(bubble).position == 150


def test_inner_bar_wheel_reuses_native_text_and_outer_spillover_preferences(chat):
    history, native, _, _ = chat
    history.set_entries(entries(3, text="line\n" * 1000), animate=False)
    bubble = history._bubbles[2]
    history.scroll_to(history._boxes[1].y)
    history._scroll_text_to(bubble, 0)
    outer = history.scroll_state().position
    native.SendMessage(bubble.scrollbar, con.WM_MOUSEWHEEL, (-120 & 0xFFFF) << 16, 0)
    assert history._text_scroll_state(bubble).position == 3
    assert history.scroll_state().position == outer
    native.wheel_lines = 0
    native.SendMessage(bubble.scrollbar, con.WM_MOUSEWHEEL, (-120 & 0xFFFF) << 16, 0)
    assert history._text_scroll_state(bubble).position == 3
    native.wheel_lines = WHEEL_PAGESCROLL
    native.SendMessage(bubble.scrollbar, con.WM_MOUSEWHEEL, (-120 & 0xFFFF) << 16, 0)
    assert (
        history._text_scroll_state(bubble).position
        == 3 + history._text_scroll_state(bubble).page_step
    )
    history._scroll_text_to(bubble, 0)
    native.wheel_lines = 3
    native.SendMessage(bubble.scrollbar, con.WM_MOUSEWHEEL, 120 << 16, 0)
    assert history.scroll_state().position == outer - 3 * history._line_height


@pytest.mark.parametrize(
    "cancellation",
    [
        "cancel",
        "reflow",
        "font",
        "cancelmode",
        "escape",
        "capture-loss",
        "hide",
        "killfocus",
        "prune",
    ],
)
def test_inner_drag_cancellation_never_releases_foreign_capture_or_leaves_follow_frozen(
    chat, cancellation
):
    history, native, _, _ = chat
    history.set_entries(entries(3, text="line\n" * 500), animate=False)
    bubble = history._bubbles[1]
    history.scroll_to(0)
    grab_thumb(history, native, bubble)
    assert native.GetCapture() == bubble.scrollbar
    if cancellation == "cancel":
        history.cancel_interaction()
    elif cancellation == "reflow":
        history.reflow(470, 250)
    elif cancellation == "font":
        history.set_font(701, line_height=35, scale=1.5)
    elif cancellation == "cancelmode":
        native.SendMessage(bubble.scrollbar, con.WM_CANCELMODE, 0, 0)
    elif cancellation == "escape":
        native.SendMessage(bubble.scrollbar, con.WM_KEYDOWN, con.VK_ESCAPE, 0)
    elif cancellation == "capture-loss":
        native.capture = 999
        native.SendMessage(bubble.scrollbar, 0x0215, 0, 999)
    elif cancellation == "hide":
        native.SendMessage(bubble.scrollbar, con.WM_SHOWWINDOW, 0, 0)
    elif cancellation == "killfocus":
        native.SendMessage(bubble.scrollbar, con.WM_KILLFOCUS, 0, 0)
    else:
        history.set_entries(entries(3, start=2, text="line\n" * 500), animate=False)
        assert bubble.scrollbar not in history.window_roles()
        assert bubble.scrollbar not in history._by_scrollbar
    assert history._inner_scroll.held == 0
    assert native.GetCapture() == (999 if cancellation == "capture-loss" else 0)
    assert ("release-capture", 999) not in native.events
    history.latest()
    assert history.following


def test_inner_bar_refuses_a_foreign_capture_before_focus_or_drag(chat):
    history, native, _, _ = chat
    history.set_entries(entries(1, text="line\n" * 500), animate=False)
    bubble = history._bubbles[1]
    native.SetFocus(bubble.editor)
    native.capture = 999
    native.events.clear()
    grab_thumb(history, native, bubble)
    assert native.GetCapture() == 999 and native.GetFocus() == bubble.editor
    assert history._inner_scroll.held == 0
    assert not any(event[0] in ("capture", "release-capture", "focus") for event in native.events)


def test_appending_while_an_inner_thumb_is_held_preserves_the_reader_and_does_not_animate(chat):
    history, native, _, _ = chat
    original = entries(3, text="Native full text 😀\n" * 500)
    history.set_entries(original, animate=False)
    bubble = history._bubbles[3]
    grab_thumb(history, native, bubble)
    before = history.capture_view()
    outer = history.scroll_state().position
    history.set_entries((*original, (4, "Assistant", "new arrival", "assistant")), now=10)
    after = history.capture_view()
    assert history.scroll_state().position == outer
    assert after.messages[:3] == before.messages
    assert after.anchor == before.anchor
    assert not history.following and not history.animation_active and history.unread
    assert native.GetCapture() == bubble.scrollbar
    history.cancel_interaction()


def test_hidden_inner_handles_are_registered_pruning_destroys_them_and_no_gdi_cache_grows(chat):
    history, native, _, _ = chat
    history.set_entries(entries(32, text="line\n" * 500), animate=False)
    first = history._bubbles[1]
    assert not native.IsWindowVisible(first.scrollbar)
    assert history.window_roles()[first.scrollbar] == "transcript-history-message-scrollbar"
    assert len(history.window_handles()) == 130
    objects = set(native.objects)
    history.set_entries(entries(32, start=2, text="line\n" * 500), animate=False)
    assert first.scrollbar not in history.window_roles()
    assert not native.IsWindow(first.scrollbar)
    assert len(history._by_scrollbar) == 32
    assert all(handle in native.windows for handle in history.window_handles())
    assert native.objects == objects
    history.close()
    assert history.window_roles() == {} and not history._by_scrollbar
    assert not native.objects


def test_latest_and_native_selection_scroll_repaint_the_inner_thumb_not_the_whole_ui(chat):
    history, native, changed, _ = chat
    history.set_entries(entries(1, text="line\n" * 500), animate=False)
    bubble = history._bubbles[1]
    history._scroll_text_to(bubble, 40)
    assert bubble.scroll_state.position == 40
    native.events.clear()
    history.latest()
    assert bubble.scroll_state.at_end
    assert ("invalidate", bubble.scrollbar) in native.events
    history.set_entries(entries(2, text="line\n" * 500), now=10)
    assert history.animation_active
    changed.clear()
    history.tick(10.04)
    history.tick(10.20)
    assert changed == []


def test_copy_selection_and_message_navigation_work_while_the_inner_bar_has_focus(chat):
    history, native, _, _ = chat
    history.set_entries(entries(3, text="Unicode 😀 text\n" * 100), animate=False)
    bubble = history._bubbles[3]
    native.SetFocus(bubble.scrollbar)
    native.keys[con.VK_CONTROL] = -1
    native.SendMessage(bubble.scrollbar, con.WM_KEYDOWN, ord("A"), 0)
    native.SendMessage(bubble.scrollbar, con.WM_KEYDOWN, ord("C"), 0)
    assert native.windows[bubble.editor]["selection"] == (
        0,
        utf16_length(native.GetWindowText(bubble.editor)),
    )
    assert native.clipboard_text == native.GetWindowText(bubble.editor)
    native.SendMessage(bubble.scrollbar, con.WM_KEYDOWN, con.VK_PRIOR, 0)
    assert native.GetFocus() == history._bubbles[2].editor
    assert native.IsWindowVisible(native.GetFocus())


@pytest.mark.parametrize("width", [1, 17, 96, 320])
def test_inner_gutter_never_eliminates_or_overlaps_the_native_text_viewport(chat, width):
    history, native, _, _ = chat
    text = "😀" * 16_000
    history.reflow(width, 75)
    history.set_entries(entries(1, text=text), animate=False)
    bubble = history._bubbles[1]
    x, y, text_width, text_height = native.windows[bubble.editor]["position"]
    assert text_width >= 1 and text_height >= 1
    assert native.GetWindowText(bubble.editor) == text
    assert bubble.scrollbar in history.window_roles()
    if bubble.scroll_visible:
        bar_x, bar_y, bar_width, bar_height = native.windows[bubble.scrollbar]["position"]
        assert x + text_width == bar_x and y == bar_y
        assert bar_width <= SCROLLBAR_DIP and bar_height == text_height
        assert bar_x + bar_width <= history._boxes[0].width
    else:
        assert not native.IsWindowVisible(bubble.scrollbar)


def test_inner_control_allocation_failure_does_not_leave_an_owned_window_or_role(chat):
    history, native, _, _ = chat
    history.set_entries(entries(1), animate=False)
    native.fail_create = history._class_name, 3
    with pytest.raises(OSError, match="Synthetic"):
        history.set_entries(entries(2), now=2)
    assert set(native.windows) == {1}
    assert history.window_handles() == ()
    assert not history._by_scrollbar
    assert history.capture_view() == ChatView()


@pytest.mark.parametrize("failed_control", ["EDIT", "inner"])
def test_update_failure_after_pruning_is_terminal_and_view_render_and_close_remain_safe(
    chat, failed_control
):
    history, native, _, _ = chat
    history.set_entries(entries(2, text="line\n" * 500), animate=False)
    old_handles = history.window_handles()
    old_labels = {bubble.label for bubble in history._bubbles.values()}
    native.fail_create = "EDIT" if failed_control == "EDIT" else (history._class_name, 3)
    with pytest.raises(OSError, match="Synthetic"):
        history.set_entries(entries(2, start=2, text="line\n" * 500), now=2)
    assert native.windows.keys() == {1}
    assert not any(native.IsWindow(handle) for handle in old_handles)
    assert not any(handle in history.window_roles() for handle in old_labels)
    assert history.window_roles() == {}
    assert history._boxes == () and history._bubbles == {}
    assert not history._by_editor and not history._by_window and not history._by_scrollbar
    assert history.capture_view() == ChatView()
    assert not history.scroll_state().maximum
    history._render()
    history.close()
    history.close()
    assert history.capture_view() == ChatView()
    with pytest.raises(RuntimeError, match="native chat history failed"):
        history.set_entries(entries(2, start=2), now=3)
    with pytest.raises(RuntimeError, match="native chat history failed"):
        history.create(1, 7, 301)
    assert not native.objects and not native.classes


@pytest.mark.parametrize("change", ["append", "font", "reflow"])
def test_backward_selection_keeps_its_active_end_and_shift_navigation_without_native_reset(
    chat, change
):
    history, native, _, _ = chat
    original = entries(2, text="abcdefghijklmnopqrstuvwxyz\n" * 100)
    history.set_entries(original, animate=False)
    bubble = history._bubbles[1]
    native.SendMessage(bubble.editor, con.EM_SETSEL, 20, 5)
    assert native.windows[bubble.editor]["selection"] == (5, 20)
    assert history.capture_view().messages[0].selection == (20, 5)
    native.events.clear()
    if change == "append":
        history.set_entries((*original, (3, "Assistant", "reply", "assistant")), now=3)
    elif change == "font":
        history.set_font(701, line_height=35, scale=1.5)
    else:
        history.reflow(320, 190)
    assert not any(event[0] == "selection" and event[1] == bubble.editor for event in native.events)
    assert native.windows[bubble.editor]["selection_anchor"] == 20
    assert native.windows[bubble.editor]["selection_active"] == 5
    native.keys[con.VK_SHIFT] = -1
    native.SendMessage(bubble.editor, con.WM_KEYDOWN, con.VK_LEFT, 0)
    assert native.windows[bubble.editor]["selection"] == (4, 20)
    assert native.windows[bubble.editor]["selection_anchor"] == 20
    assert native.windows[bubble.editor]["selection_active"] == 4
    assert history.capture_view().messages[0].selection == (20, 4)


@pytest.mark.parametrize("restoration", ["snapshot", "replacement-text"])
def test_required_restoration_uses_anchor_then_active_end_not_ordered_getsel_bounds(
    chat, restoration
):
    history, native, _, _ = chat
    original = entries(2, text="abcdefghijklmnopqrstuvwxyz\n" * 100)
    history.set_entries(original, animate=False)
    bubble = history._bubbles[1]
    native.SendMessage(bubble.editor, con.EM_SETSEL, 20, 5)
    view = history.capture_view()
    native.events.clear()
    if restoration == "snapshot":
        native.SendMessage(bubble.editor, con.EM_SETSEL, 0, 0)
        history.restore_view(view)
    else:
        updated = (1, "Assistant", original[0][2].upper(), "assistant")
        history.set_entries((updated, original[1]), animate=False)
    assert ("selection", bubble.editor, 20, 5) in native.events
    assert native.windows[bubble.editor]["selection_anchor"] == 20
    assert native.windows[bubble.editor]["selection_active"] == 5
    native.keys[con.VK_SHIFT] = -1
    native.SendMessage(bubble.editor, con.WM_KEYDOWN, con.VK_RIGHT, 0)
    assert native.windows[bubble.editor]["selection"] == (6, 20)
    assert native.windows[bubble.editor]["selection_active"] == 6


def test_native_shift_and_mouse_selection_direction_is_tracked_without_an_em_setsel_request(chat):
    history, native, _, _ = chat
    history.set_entries(entries(2, text="abcdefghijklmnopqrstuvwxyz\n" * 100), animate=False)
    bubble = history._bubbles[1]
    native.set_selection(bubble.editor, 20, 20)
    history._record_user(bubble)
    native.keys[con.VK_SHIFT] = -1
    native.SendMessage(bubble.editor, con.WM_KEYDOWN, con.VK_LEFT, 0)
    assert history.capture_view().messages[0].selection == (20, 19)
    native.set_selection(bubble.editor, 20, 8)
    native.SendMessage(bubble.editor, con.WM_MOUSEMOVE, con.MK_LBUTTON, point(2, 2))
    view = history.capture_view()
    assert view.messages[0].selection == (20, 8)
    native.SendMessage(bubble.editor, con.EM_SETSEL, 0, 0)
    history.restore_view(view)
    native.SendMessage(bubble.editor, con.WM_KEYDOWN, con.VK_LEFT, 0)
    assert native.windows[bubble.editor]["selection"] == (7, 20)
    assert native.windows[bubble.editor]["selection_active"] == 7


def test_backward_selection_same_bounds_can_be_restored_after_its_direction_changes(chat):
    history, native, _, _ = chat
    history.set_entries(entries(2, text="abcdefghijklmnopqrstuvwxyz\n" * 100), animate=False)
    bubble = history._bubbles[1]
    native.SendMessage(bubble.editor, con.EM_SETSEL, 20, 5)
    view = history.capture_view()
    native.SendMessage(bubble.editor, con.EM_SETSEL, 5, 20)
    assert native.windows[bubble.editor]["selection"] == (5, 20)
    history.restore_view(view)
    assert native.windows[bubble.editor]["selection_anchor"] == 20
    assert native.windows[bubble.editor]["selection_active"] == 5


def test_custom_thumb_motion_at_end_keeps_following_frozen_until_release(chat):
    history, native, _, _ = chat
    original = entries(3, text="line\n" * 500)
    history.set_entries(original, animate=False)
    bubble = history._bubbles[3]
    grab_thumb(history, native, bubble)
    height = native.GetClientRect(bubble.scrollbar)[3]
    native.SendMessage(bubble.scrollbar, con.WM_MOUSEMOVE, con.MK_LBUTTON, point(3, height + 50))
    assert history._text_scroll_state(bubble).at_end
    assert history._following and not history.following
    before = history.capture_view()
    history.set_entries((*original, (4, "Assistant", "new reply", "assistant")), now=10)
    assert history.capture_view().anchor == before.anchor
    assert history.capture_view().messages[:3] == before.messages
    assert not history.following and not history.animation_active
    assert native.GetCapture() == bubble.scrollbar
    native.SendMessage(bubble.scrollbar, con.WM_LBUTTONUP, 0, point(3, height + 50))
    assert history._inner_scroll.held == 0 and native.GetCapture() == 0


@pytest.mark.parametrize("delta", [-120, 120])
def test_page_wheel_over_one_line_message_moves_one_outer_page_not_one_text_line(chat, delta):
    history, native, _, _ = chat
    history.set_entries(entries(18, text="one line"), animate=False)
    history.scroll_to(500)
    bubble = history._bubbles[7]
    assert history._text_scroll_state(bubble).page == 1
    assert not history._text_scroll_state(bubble).maximum
    native.wheel_lines = WHEEL_PAGESCROLL
    native.SendMessage(bubble.editor, con.WM_MOUSEWHEEL, (delta & 0xFFFF) << 16, 0)
    assert history.scroll_state().position == 500 + (-238 if delta > 0 else 238)


@pytest.mark.parametrize("delta", [-120, 120])
@pytest.mark.parametrize("available_lines", [0, 1, 3, 4, 7])
def test_page_wheel_spills_the_unconsumed_fraction_using_outer_page_pixels(
    chat, delta, available_lines
):
    history, native, _, _ = chat
    history.set_entries(entries(5, text="line\n" * 500), animate=False)
    history.scroll_to(500)
    bubble = history._bubbles[3]
    state = history._text_scroll_state(bubble)
    assert state.page_step == 8
    start = available_lines if delta > 0 else state.maximum - available_lines
    history._scroll_text_to(bubble, start)
    native.SendMessage(bubble.editor, con.EM_SETSEL, 20, 5)
    native.wheel_lines = WHEEL_PAGESCROLL
    native.SendMessage(bubble.editor, con.WM_MOUSEWHEEL, (delta & 0xFFFF) << 16, 0)
    unconsumed = state.page_step - available_lines
    pixels = round(unconsumed / state.page_step * 238)
    assert history.scroll_state().position == 500 + (-pixels if delta > 0 else pixels)
    actual = history._text_scroll_state(bubble)
    assert actual.position == (0 if delta > 0 else actual.maximum)
    assert native.windows[bubble.editor]["selection_anchor"] == 20
    assert native.windows[bubble.editor]["selection_active"] == 5
