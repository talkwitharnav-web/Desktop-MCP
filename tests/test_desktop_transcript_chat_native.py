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
from desktop_mcp.transcript_scroll import WHEEL_PAGESCROLL


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
        return handle

    def CreatePen(self, style, width, color):
        return self.CreateSolidBrush(color)

    def DeleteObject(self, handle):
        assert handle in self.objects
        self.objects.remove(handle)
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
        if self.fail_create == kind:
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
        if handle in self.subclasses:
            return self.subclasses[handle](handle, message, wparam, lparam, 0, 0)
        kind = self.windows[handle]["kind"]
        if kind in self.classes:
            return self.classes[kind](handle, message, wparam, lparam)
        return self.native_message(handle, message, wparam, lparam)

    def native_message(self, handle, message, wparam, lparam):
        item = self.windows[handle]
        if message == con.WM_SETTEXT:
            item["text"] = lparam
            item["first"], item["selection"], item["line_cache"] = 0, (0, 0), None
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
            ctypes.cast(wparam, ctypes.POINTER(wintypes.DWORD)).contents.value = start
            ctypes.cast(lparam, ctypes.POINTER(wintypes.DWORD)).contents.value = end
            return (start & 0xFFFF) | ((end & 0xFFFF) << 16)
        elif message == con.EM_SETSEL:
            end = utf16_length(item["text"]) if lparam == -1 else lparam
            item["selection"] = wparam, end
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

    def SetWindowTheme(self, handle, theme, part):
        return 0

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

    def ReleaseCapture(self):
        self.events.append(("release-capture", self.capture))
        self.capture = 0

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

    def RoundRect(self, dc, *rectangle):
        self.events.append(("rounded", rectangle))

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
        history._api = history._comctl = history._user32 = history._theme = native
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
    assert len(history.window_handles()) == 8
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
        assert native.windows[bubble.editor]["scrollbar"]
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
    assert len(history.window_handles()) == 98
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


def test_partial_native_creation_failure_leaves_only_owned_resources_for_close(chat):
    history, native, _, _ = chat
    history.set_entries(entries(1), animate=False)
    first = history._bubbles[1]
    native.fail_create = "EDIT"
    with pytest.raises(OSError, match="Synthetic"):
        history.set_entries(entries(2), now=2)
    assert native.GetWindowText(first.editor) == entries(1)[0][2]
    assert not any(
        item["parent"] != 0 and item["parent"] not in native.windows
        for item in native.windows.values()
    )
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
