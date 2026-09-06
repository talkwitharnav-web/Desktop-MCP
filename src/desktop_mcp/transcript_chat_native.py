"""Native chat bubbles with real read-only EDIT text and bounded child windows.

All methods run on the owning UI thread. The caller owns the top-level window,
external history scrollbar, borrowed font and timer. ScrollState uses pixels for
the outer history, not EDIT line numbers. Each long message has an independent
native text scrollbar; Ctrl+PageUp/PageDown visits adjacent messages. Native
selection/copy is per message, never a painted imitation of selectable text.
"""

from __future__ import annotations

from collections.abc import Callable
import ctypes
from ctypes import wintypes
from dataclasses import dataclass
import math
import time

from desktop_mcp.transcript_chat import (
    ARRIVAL_SECONDS,
    ASSISTANT_BACKGROUND,
    BACKGROUND,
    BORDER_COLOR,
    FOCUS_COLOR,
    LABEL_COLOR,
    TEXT_COLOR,
    USER_BACKGROUND,
    BubbleBox,
    ChatEntry,
    ChatView,
    EntryTuple,
    MessageView,
    ReadingAnchor,
    anchor_at,
    anchor_position,
    arrival_offset,
    bubble_size,
    content_height,
    layout_bubbles,
    remap_anchor,
    utf16_length,
    validate_entries,
)
from desktop_mcp.transcript_scroll import ScrollState, WHEEL_PAGESCROLL, wheel_movement

_SUBCLASS_ID = 0x43484154
_EMPTY_TEXT = "Your messages and replies appear here.\r\nAsk Copilot to listen with TranscriptRead."


@dataclass
class _Bubble:
    entry: ChatEntry
    window: int
    label: int
    editor: int
    body_height: int = 1
    measured: tuple | None = None
    desired: MessageView | None = None
    observed: MessageView | None = None
    position: tuple[int, int, int, int] | None = None
    visible: bool = False
    wheel_remainder: int = 0


class NativeChatHistory:
    """A presentation-only history control; it has no conversation queue authority."""

    def __init__(
        self,
        *,
        on_change: Callable[[], None] | None = None,
        on_error: Callable[[Exception], None] | None = None,
    ) -> None:
        self.hwnd = 0
        self._empty = 0
        self._on_change = on_change
        self._on_error = on_error
        self._gui = None
        self._instance = 0
        self._class_name = ""
        self._registered = False
        self._brushes: dict[str, int] = {}
        self._pens: dict[str, int] = {}
        self._bubbles: dict[int, _Bubble] = {}
        self._by_window: dict[int, _Bubble] = {}
        self._by_editor: dict[int, _Bubble] = {}
        self._roles: dict[int, str] = {}
        self._entries: tuple[EntryTuple, ...] | None = None
        self._boxes: tuple[BubbleBox, ...] = ()
        self._font = 0
        self._line_height = 22
        self._scale = 1.0
        self._width = self._height = 1
        self._position = 0
        self._desired_anchor: ReadingAnchor | None = None
        self._following = True
        self._unread = False
        self._pointer_down = False
        self._pointer_window = 0
        self._interacting = False
        self._updating = 0
        self._wheel_remainder = 0
        self._highest_sequence = -1
        self._arrivals: dict[int, float] = {}
        self._animation_now = 0.0
        self._error: Exception | None = None
        self._closing = False

    @property
    def animation_active(self) -> bool:
        return bool(self._arrivals)

    @property
    def following(self) -> bool:
        return self._following and not self._pointer_down and not self._interacting

    @property
    def unread(self) -> bool:
        return self._unread

    def window_handles(self) -> tuple[int, ...]:
        return tuple(self._roles)

    def window_roles(self) -> dict[int, str]:
        """Content-free HWND ownership, including offscreen native message controls."""
        return dict(self._roles)

    def _load_native(self) -> None:
        import win32api
        import win32con
        import win32gui

        self._api, self._con, self._gui = win32api, win32con, win32gui
        self._user32 = ctypes.WinDLL("user32", use_last_error=True)
        self._user32.SystemParametersInfoW.argtypes = [
            wintypes.UINT,
            wintypes.UINT,
            ctypes.c_void_p,
            wintypes.UINT,
        ]
        self._user32.SystemParametersInfoW.restype = wintypes.BOOL
        self._user32.ShowScrollBar.argtypes = [wintypes.HWND, ctypes.c_int, wintypes.BOOL]
        self._user32.ShowScrollBar.restype = wintypes.BOOL
        self._comctl = ctypes.WinDLL("comctl32", use_last_error=True)
        callback_type = ctypes.WINFUNCTYPE(
            ctypes.c_ssize_t,
            wintypes.HWND,
            wintypes.UINT,
            wintypes.WPARAM,
            wintypes.LPARAM,
            ctypes.c_size_t,
            ctypes.c_size_t,
        )
        self._text_callback = callback_type(self._text_procedure)
        self._comctl.SetWindowSubclass.argtypes = [
            wintypes.HWND,
            callback_type,
            ctypes.c_size_t,
            ctypes.c_size_t,
        ]
        self._comctl.SetWindowSubclass.restype = wintypes.BOOL
        self._comctl.RemoveWindowSubclass.argtypes = [wintypes.HWND, callback_type, ctypes.c_size_t]
        self._comctl.RemoveWindowSubclass.restype = wintypes.BOOL
        self._comctl.DefSubclassProc.argtypes = [
            wintypes.HWND,
            wintypes.UINT,
            wintypes.WPARAM,
            wintypes.LPARAM,
        ]
        self._comctl.DefSubclassProc.restype = ctypes.c_ssize_t
        self._theme = ctypes.WinDLL("uxtheme", use_last_error=True)
        self._theme.SetWindowTheme.argtypes = [wintypes.HWND, wintypes.LPCWSTR, wintypes.LPCWSTR]
        self._theme.SetWindowTheme.restype = ctypes.c_long

    def create(self, parent: int, instance: int, control_id: int) -> int:
        """Create one child host. Supply its readable borrowed font with set_font."""
        if self.hwnd:
            raise RuntimeError("The chat control has already been created.")
        self._load_native()
        gui, con = self._gui, self._con
        self._instance = instance
        self._class_name = f"DesktopMCPChat-{id(self):x}"
        try:
            for name, color in (
                ("host", BACKGROUND),
                ("assistant", ASSISTANT_BACKGROUND),
                ("user", USER_BACKGROUND),
            ):
                self._brushes[name] = gui.CreateSolidBrush(self._api.RGB(*color))
            for name, color in (("border", BORDER_COLOR), ("focus", FOCUS_COLOR)):
                self._pens[name] = gui.CreatePen(con.PS_SOLID, 1, self._api.RGB(*color))
            window_class = gui.WNDCLASS()
            window_class.hInstance = instance
            window_class.lpszClassName = self._class_name
            window_class.lpfnWndProc = self._procedure
            window_class.hCursor = gui.LoadCursor(0, con.IDC_ARROW)
            gui.RegisterClass(window_class)
            self._registered = True
            self.hwnd = gui.CreateWindowEx(
                con.WS_EX_CONTROLPARENT,
                self._class_name,
                "Conversation",
                con.WS_CHILD | con.WS_VISIBLE | con.WS_CLIPCHILDREN | con.WS_TABSTOP,
                0,
                0,
                1,
                1,
                parent,
                control_id,
                instance,
                None,
            )
            self._empty = gui.CreateWindowEx(
                0,
                "STATIC",
                _EMPTY_TEXT,
                con.WS_CHILD | con.WS_VISIBLE | con.SS_NOPREFIX,
                0,
                0,
                1,
                1,
                self.hwnd,
                1,
                instance,
                None,
            )
            self._publish_roles()
            return self.hwnd
        except Exception:
            self.close()
            raise

    def close(self) -> None:
        """Destroy only owned children and GDI objects, never the borrowed font."""
        if self._gui is None or self._closing:
            return
        self._closing = True
        self._arrivals.clear()
        try:
            self.cancel_interaction()
            if self.hwnd and self._gui.IsWindow(self.hwnd):
                self._gui.DestroyWindow(self.hwnd)
            self.hwnd = self._empty = 0
            self._bubbles.clear()
            self._by_window.clear()
            self._by_editor.clear()
            self._roles = {}
            self._boxes = ()
            self._entries = None
            self._desired_anchor = None
            self._position = 0
            self._font = 0
            self._width = self._height = 1
            self._line_height, self._scale = 22, 1.0
            self._wheel_remainder = 0
            self._following, self._unread = True, False
            self._highest_sequence = -1
            if self._registered:
                self._gui.UnregisterClass(self._class_name, self._instance)
                self._registered = False
            for objects in (self._pens, self._brushes):
                for name, handle in tuple(objects.items()):
                    self._gui.DeleteObject(handle)
                    del objects[name]
        finally:
            self._closing = False

    def _publish_roles(self) -> None:
        roles = {}
        if self.hwnd:
            roles[self.hwnd] = "transcript-history"
        if self._empty:
            roles[self._empty] = "transcript-history-label"
        for bubble in self._bubbles.values():
            roles[bubble.window] = "transcript-history-bubble"
            roles[bubble.label] = "transcript-history-label"
            roles[bubble.editor] = "transcript-history-text"
        self._roles = roles

    def _raise_error(self) -> None:
        if self._error is not None:
            raise RuntimeError("The native chat history failed.") from self._error

    def _notify(self) -> None:
        if not self._updating and not self._closing and self._on_change is not None:
            self._on_change()

    def _failure(self, error: Exception) -> None:
        self._error = error
        self._arrivals.clear()
        if self._on_error is not None:
            self._on_error(error)

    def _create_bubble(self, entry: ChatEntry) -> _Bubble:
        gui, con = self._gui, self._con
        size = bubble_size(self._width, self._height, self._scale, self._line_height, entry.role)
        window = gui.CreateWindowEx(
            con.WS_EX_CONTROLPARENT,
            self._class_name,
            entry.label,
            con.WS_CHILD | con.WS_CLIPCHILDREN | con.WS_CLIPSIBLINGS,
            0,
            0,
            size.width,
            size.label_height + 2 * size.padding + size.body_cap,
            self.hwnd,
            0,
            self._instance,
            None,
        )
        try:
            label = gui.CreateWindowEx(
                0,
                "STATIC",
                entry.label,
                con.WS_CHILD | con.WS_VISIBLE | con.SS_NOPREFIX | con.SS_ENDELLIPSIS,
                size.padding,
                size.padding,
                size.body_width,
                size.label_height,
                window,
                1,
                self._instance,
                None,
            )
            editor = gui.CreateWindowEx(
                0,
                "EDIT",
                "",
                con.WS_CHILD
                | con.WS_VISIBLE
                | con.WS_TABSTOP
                | con.ES_MULTILINE
                | con.ES_READONLY
                | con.ES_AUTOVSCROLL
                | con.ES_NOHIDESEL,
                size.padding,
                size.padding + size.label_height,
                size.body_width,
                size.body_cap,
                window,
                2,
                self._instance,
                None,
            )
            if not self._comctl.SetWindowSubclass(editor, self._text_callback, _SUBCLASS_ID, 0):
                raise ctypes.WinError(ctypes.get_last_error())
            bubble = _Bubble(entry, window, label, editor)
            self._by_window[window] = bubble
            self._by_editor[editor] = bubble
            gui.SendMessage(editor, con.EM_SETLIMITTEXT, 0x7FFFFFFE, 0)
            gui.SendMessage(editor, con.EM_SETMARGINS, 3, 0)
            for handle in (label, editor):
                if self._font:
                    gui.SendMessage(handle, con.WM_SETFONT, self._font, False)
            self._theme.SetWindowTheme(editor, "DarkMode_Explorer", None)
            if not gui.SendMessage(editor, con.WM_SETTEXT, 0, entry.text):
                raise RuntimeError("The native message control could not retain its text.")
            return bubble
        except Exception:
            gui.DestroyWindow(window)
            raise

    def set_entries(
        self,
        entries: tuple[EntryTuple, ...],
        *,
        now: float | None = None,
        animate: bool = True,
    ) -> bool:
        """Diff by sequence; never replace unchanged native text or replay old arrivals."""
        self._raise_error()
        if not animate:
            self.cancel_animation()
        if entries == self._entries:
            return False
        validated = validate_entries(entries)
        now = time.monotonic() if now is None else now
        if not math.isfinite(now):
            raise ValueError("Chat animation requires finite monotonic time.")
        view = self.capture_view()
        previous = self._entries
        new_ids = {entry.sequence for entry in validated if entry.sequence > self._highest_sequence}
        was_visible = self._gui.IsWindowVisible(self.hwnd)
        focused = self._by_editor.get(self._gui.GetFocus())
        self._updating += 1
        try:
            keep = {entry.sequence for entry in validated}
            for sequence in tuple(self._bubbles):
                if sequence not in keep:
                    bubble = self._bubbles.pop(sequence)
                    self._gui.DestroyWindow(bubble.window)
                    self._by_window.pop(bubble.window, None)
                    self._by_editor.pop(bubble.editor, None)
            changed = False
            for entry in validated:
                bubble = self._bubbles.get(entry.sequence)
                if bubble is None:
                    self._bubbles[entry.sequence] = self._create_bubble(entry)
                    changed = True
                elif bubble.entry != entry:
                    self._gui.SetWindowText(bubble.window, entry.label)
                    self._gui.SetWindowText(bubble.label, entry.label)
                    if bubble.entry.text != entry.text:
                        if not self._gui.SendMessage(
                            bubble.editor, self._con.WM_SETTEXT, 0, entry.text
                        ):
                            raise RuntimeError("The native message update failed.")
                    bubble.entry = entry
                    bubble.measured = None
                    changed = True
            self._bubbles = {entry.sequence: self._bubbles[entry.sequence] for entry in validated}
            self._entries = entries
            self._highest_sequence = max(self._highest_sequence, max(new_ids, default=-1))
            eligible = animate and previous is not None and view.following and was_visible
            self._arrivals = {sequence: now for sequence in new_ids} if eligible else {}
            self._animation_now = now
            self._measure()
            self.restore_view(view)
            if changed and not view.following:
                self._unread = True
            if not self._boxes:
                self._following, self._unread = True, False
            if eligible:
                visible = {
                    box.sequence
                    for box in self._boxes
                    if box.bottom > self._position and box.y < self._position + self._height
                }
                self._arrivals = {sequence: now for sequence in new_ids & visible}
            self._publish_roles()
            self._render()
            if was_visible and focused is not None and focused.entry.sequence not in self._bubbles:
                visible_bubble = next(
                    (bubble for bubble in self._bubbles.values() if bubble.visible), None
                )
                self._gui.SetFocus(visible_bubble.editor if visible_bubble else self.hwnd)
        finally:
            self._updating -= 1
        self._notify()
        return True

    def set_font(self, font: int, *, line_height: int, scale: float = 1.0) -> None:
        """Borrow a readable native font; preserve native selections and reading anchors."""
        bubble_size(self._width, self._height, scale, line_height, "assistant")
        if (font, line_height, scale) == (self._font, self._line_height, self._scale):
            return
        view = self.capture_view()
        self._updating += 1
        try:
            self.cancel_animation()
            self._font, self._line_height, self._scale = font, line_height, scale
            handles = [self._empty]
            for bubble in self._bubbles.values():
                handles.extend((bubble.label, bubble.editor))
            for handle in handles:
                if handle:
                    self._gui.SendMessage(handle, self._con.WM_SETFONT, font, False)
            self._measure()
            self.restore_view(view)
        finally:
            self._updating -= 1
        self._notify()

    def reflow(self, width: int, height: int) -> None:
        """Reflow client-local geometry. Offscreen rows never get huge/negative HWND positions."""
        width, height = max(1, width), max(1, height)
        bubble_size(width, height, self._scale, self._line_height, "assistant")
        if (width, height) == (self._width, self._height):
            return
        view = self.capture_view()
        self._updating += 1
        try:
            self.cancel_animation()
            self._width, self._height = width, height
            self._measure()
            self.restore_view(view)
        finally:
            self._updating -= 1
        self._notify()

    def _measure(self) -> None:
        gui, con = self._gui, self._con
        for bubble in self._bubbles.values():
            size = bubble_size(
                self._width, self._height, self._scale, self._line_height, bubble.entry.role
            )
            key = size, self._font, self._line_height, bubble.entry.text
            if bubble.measured == key:
                continue
            self._user32.ShowScrollBar(bubble.editor, con.SB_VERT, False)
            gui.MoveWindow(
                bubble.editor,
                size.padding,
                size.padding + size.label_height,
                size.body_width,
                size.body_cap,
                False,
            )
            lines = max(1, gui.SendMessage(bubble.editor, con.EM_GETLINECOUNT, 0, 0))
            body_height = min(size.body_cap, lines * self._line_height + 2)
            gui.MoveWindow(
                bubble.editor,
                size.padding,
                size.padding + size.label_height,
                size.body_width,
                body_height,
                False,
            )
            state = self._text_scroll_state(bubble)
            self._user32.ShowScrollBar(bubble.editor, con.SB_VERT, bool(state.maximum))
            if state.maximum:
                body_height = size.body_cap
                gui.MoveWindow(
                    bubble.editor,
                    size.padding,
                    size.padding + size.label_height,
                    size.body_width,
                    body_height,
                    False,
                )
            gui.MoveWindow(
                bubble.label,
                size.padding,
                size.padding,
                size.body_width,
                size.label_height,
                False,
            )
            bubble.body_height = body_height
            bubble.measured = key
            for handle in (bubble.window, bubble.label, bubble.editor):
                gui.InvalidateRect(handle, None, False)
        entries = tuple(bubble.entry for bubble in self._bubbles.values())
        self._boxes = layout_bubbles(
            entries,
            {sequence: bubble.body_height for sequence, bubble in self._bubbles.items()},
            self._width,
            self._height,
            self._scale,
            self._line_height,
        )
        if self._empty:
            inset = min(round(12 * self._scale), max(0, (self._width - 1) // 4))
            gui.MoveWindow(
                self._empty,
                inset,
                inset,
                max(1, self._width - 2 * inset),
                max(1, self._height - 2 * inset),
                False,
            )
            gui.ShowWindow(self._empty, con.SW_HIDE if self._boxes else con.SW_SHOWNA)

    def _text_scroll_state(self, bubble: _Bubble) -> ScrollState:
        gui, con = self._gui, self._con
        rect = wintypes.RECT()
        gui.SendMessage(bubble.editor, con.EM_GETRECT, 0, ctypes.addressof(rect))
        return ScrollState.from_edit(
            gui.SendMessage(bubble.editor, con.EM_GETLINECOUNT, 0, 0),
            gui.SendMessage(bubble.editor, con.EM_GETFIRSTVISIBLELINE, 0, 0),
            rect.bottom - rect.top,
            self._line_height,
        )

    def _sample_message(self, bubble: _Bubble) -> MessageView:
        gui, con = self._gui, self._con
        start, end = wintypes.DWORD(), wintypes.DWORD()
        gui.SendMessage(
            bubble.editor, con.EM_GETSEL, ctypes.addressof(start), ctypes.addressof(end)
        )
        line = gui.SendMessage(bubble.editor, con.EM_GETFIRSTVISIBLELINE, 0, 0)
        anchor = max(0, gui.SendMessage(bubble.editor, con.EM_LINEINDEX, line, 0))
        return MessageView(bubble.entry.sequence, anchor, (start.value, end.value))

    def capture_view(self) -> ChatView:
        """Capture semantic message positions, not packed 16-bit document offsets."""
        messages = []
        for bubble in self._bubbles.values():
            actual = self._sample_message(bubble)
            if bubble.observed == actual and bubble.desired is not None:
                actual = bubble.desired
            messages.append(actual)
        following = self.following and all(
            item.selection[0] == item.selection[1] for item in messages
        )
        if following and self._boxes:
            following = self._text_scroll_state(self._bubbles[self._boxes[-1].sequence]).at_end
        return ChatView(
            self._desired_anchor or anchor_at(self._boxes, self._position),
            tuple(messages),
            following,
        )

    def restore_view(self, view: ChatView) -> None:
        self._updating += 1
        try:
            for item in view.messages:
                bubble = self._bubbles.get(item.sequence)
                if bubble is None:
                    continue
                limit = utf16_length(bubble.entry.text)
                desired = MessageView(
                    item.sequence,
                    min(max(0, item.anchor), limit),
                    tuple(min(max(0, offset), limit) for offset in item.selection),
                )
                self._gui.SendMessage(bubble.editor, self._con.EM_SETSEL, *desired.selection)
                target = self._gui.SendMessage(
                    bubble.editor, self._con.EM_LINEFROMCHAR, desired.anchor, 0
                )
                current = self._gui.SendMessage(
                    bubble.editor, self._con.EM_GETFIRSTVISIBLELINE, 0, 0
                )
                self._gui.SendMessage(bubble.editor, self._con.EM_LINESCROLL, 0, target - current)
                bubble.desired, bubble.observed = desired, self._sample_message(bubble)
            self._following = view.following
            if view.following:
                self._latest(clear_selection=False)
            else:
                self._desired_anchor = remap_anchor(self._boxes, view.anchor)
                self._position = self.scroll_state().clamp(
                    anchor_position(self._boxes, self._desired_anchor)
                )
                self._render()
        finally:
            self._updating -= 1
        self._notify()

    def scroll_state(self) -> ScrollState:
        return ScrollState(
            content_height(self._boxes, self._scale), max(1, self._height), self._position
        )

    def _record_user(self, bubble: _Bubble | None = None) -> None:
        if self._updating or self._closing:
            return
        if bubble is not None:
            bubble.desired = bubble.observed = self._sample_message(bubble)
        self._desired_anchor = anchor_at(self._boxes, self._position)
        selected = any(
            item.desired is not None and item.desired.selection[0] != item.desired.selection[1]
            for item in self._bubbles.values()
        )
        last_at_end = (
            not self._boxes
            or self._text_scroll_state(self._bubbles[self._boxes[-1].sequence]).at_end
        )
        self._following = self.scroll_state().at_end and last_at_end and not selected
        if self.following:
            self._unread = False
        self._notify()

    def scroll_to(self, position: int) -> None:
        self.cancel_animation()
        self._position = self.scroll_state().clamp(position)
        self._render()
        self._record_user()

    def scroll_command(self, command: int) -> bool:
        state = self.scroll_state()
        targets = {
            0: state.position - self._line_height,
            1: state.position + self._line_height,
            2: state.position - max(1, state.page - self._line_height),
            3: state.position + max(1, state.page - self._line_height),
            6: 0,
            7: state.maximum,
        }
        if command not in targets:
            return False
        self.scroll_to(targets[command])
        return True

    def wheel(self, delta: int, lines_per_notch: int = 3) -> None:
        state = self.scroll_state()
        movement, self._wheel_remainder = wheel_movement(
            self._wheel_remainder,
            delta,
            lines_per_notch,
            max(1, state.page // self._line_height),
        )
        if movement:
            step = self._line_height
            if lines_per_notch == WHEEL_PAGESCROLL:
                step = max(1, state.page - self._line_height) / max(
                    1, state.page // self._line_height - 1
                )
            self.scroll_to(state.position + round(movement * step))

    def _wheel_text(self, bubble: _Bubble, delta: int) -> None:
        state = self._text_scroll_state(bubble)
        movement, bubble.wheel_remainder = wheel_movement(
            bubble.wheel_remainder, delta, self._wheel_lines(), state.page
        )
        if not movement:
            return
        self.cancel_animation()
        target = state.clamp(state.position + movement)
        self._updating += 1
        try:
            self._gui.SendMessage(
                bubble.editor, self._con.EM_LINESCROLL, 0, target - state.position
            )
        finally:
            self._updating -= 1
        remaining = movement - (target - state.position)
        if remaining:
            self._position = self.scroll_state().clamp(
                self._position + remaining * self._line_height
            )
            self._render()
        self._record_user(bubble)

    def _latest(self, *, clear_selection: bool) -> None:
        gui, con = self._gui, self._con
        self._updating += 1
        try:
            if clear_selection:
                for bubble in self._bubbles.values():
                    actual = self._sample_message(bubble)
                    if actual.selection[0] != actual.selection[1]:
                        gui.SendMessage(
                            bubble.editor, con.EM_SETSEL, actual.selection[1], actual.selection[1]
                        )
                    bubble.desired = bubble.observed = self._sample_message(bubble)
            if self._boxes:
                bubble = self._bubbles[self._boxes[-1].sequence]
                end = utf16_length(bubble.entry.text)
                gui.SendMessage(bubble.editor, con.EM_SETSEL, end, end)
                state = self._text_scroll_state(bubble)
                gui.SendMessage(bubble.editor, con.EM_LINESCROLL, 0, state.maximum - state.position)
                bubble.desired = bubble.observed = self._sample_message(bubble)
            self._position = self.scroll_state().maximum
            self._desired_anchor = anchor_at(self._boxes, self._position)
            self._following, self._unread = True, False
            self._render()
        finally:
            self._updating -= 1

    def latest(self) -> None:
        """Explicitly resume following; this user action alone may clear selection."""
        self.cancel_animation()
        self._latest(clear_selection=True)
        self._notify()

    def set_interacting(self, active: bool) -> None:
        """Mark an externally owned history-scrollbar interaction."""
        self._interacting = bool(active)
        if active:
            self.cancel_animation()
        else:
            self._record_user()

    def cancel_animation(self) -> None:
        if self._arrivals:
            self._arrivals.clear()
            self._render()

    def cancel_interaction(self) -> None:
        self._pointer_down = self._interacting = False
        self._pointer_window = 0
        self.cancel_animation()
        if self._gui is not None:
            captured = self._gui.GetCapture()
            if captured and captured in self._roles:
                self._gui.ReleaseCapture()
                self._gui.SendMessage(captured, self._con.WM_CANCELMODE, 0, 0)

    def tick(self, now: float) -> bool:
        """Advance only active arrivals from the caller's existing UI timer."""
        self._raise_error()
        if not self._arrivals:
            return False
        if not math.isfinite(now):
            raise ValueError("Chat animation requires finite monotonic time.")
        if not self._gui.IsWindowVisible(self.hwnd):
            self.cancel_animation()
            return True
        self._animation_now = now
        self._arrivals = {
            sequence: started
            for sequence, started in self._arrivals.items()
            if now - started < ARRIVAL_SECONDS
        }
        self._render()
        return True

    def _render(self) -> None:
        if not self.hwnd or self._closing:
            return
        gui, con = self._gui, self._con
        for box in self._boxes:
            bubble = self._bubbles[box.sequence]
            y = box.y - self._position
            visible = y < self._height and y + box.height > 0
            if visible:
                shift = 0
                if (started := self._arrivals.get(box.sequence)) is not None:
                    shift = arrival_offset(self._animation_now, started, self._scale)
                    if box.role == "assistant":
                        shift = -shift
                x = max(0, min(self._width - box.width, box.x + shift))
                position = x, y, box.width, box.height
                if position != bubble.position:
                    gui.SetWindowPos(
                        bubble.window,
                        0,
                        *position,
                        con.SWP_NOACTIVATE | con.SWP_NOZORDER | con.SWP_NOCOPYBITS,
                    )
                    bubble.position = position
            if visible != bubble.visible:
                gui.ShowWindow(bubble.window, con.SW_SHOWNA if visible else con.SW_HIDE)
                bubble.visible = visible
        gui.InvalidateRect(self.hwnd, None, False)

    def _focus_message(self, sequence: int, offset: int) -> None:
        sequences = [box.sequence for box in self._boxes]
        index = sequences.index(sequence)
        target = min(max(0, index + offset), len(sequences) - 1)
        box = self._boxes[target]
        self.scroll_to(min(box.y, max(0, box.bottom - self._height)))
        self._gui.SetFocus(self._bubbles[box.sequence].editor)

    def _wheel_lines(self) -> int:
        lines = wintypes.UINT(3)
        if not self._user32.SystemParametersInfoW(0x0068, 0, ctypes.byref(lines), 0):
            return 3
        return lines.value

    def _text_procedure(self, handle, message, wparam, lparam, subclass_id=0, data=0):
        con = self._con
        try:
            if message == con.WM_NCDESTROY:
                self._comctl.RemoveWindowSubclass(handle, self._text_callback, _SUBCLASS_ID)
                if handle == self._pointer_window:
                    self._pointer_down = False
                    self._pointer_window = 0
                self._by_editor.pop(handle, None)
                self._roles = {key: role for key, role in self._roles.items() if key != handle}
                return self._comctl.DefSubclassProc(handle, message, wparam, lparam)
            bubble = self._by_editor.get(handle)
            if bubble is None or self._closing:
                return self._comctl.DefSubclassProc(handle, message, wparam, lparam)
            if message == con.WM_MOUSEWHEEL:
                self._wheel_text(bubble, ctypes.c_short((wparam >> 16) & 0xFFFF).value)
                return 0
            if (
                message == con.WM_KEYDOWN
                and wparam in (con.VK_PRIOR, con.VK_NEXT)
                and self._api.GetKeyState(con.VK_CONTROL) < 0
                and self._api.GetKeyState(con.VK_SHIFT) >= 0
            ):
                self._focus_message(bubble.entry.sequence, -1 if wparam == con.VK_PRIOR else 1)
                return 0
            if (
                message == con.WM_KEYDOWN
                and wparam == ord("A")
                and self._api.GetKeyState(con.VK_CONTROL) < 0
            ):
                self._gui.SendMessage(handle, con.EM_SETSEL, 0, -1)
                return 0
            if message == con.WM_CHAR and wparam == 1:
                return 0
            if message in (con.WM_LBUTTONDOWN, con.WM_LBUTTONDBLCLK):
                self._pointer_down = True
                self._pointer_window = handle
            elif message in (con.WM_LBUTTONUP, con.WM_CANCELMODE, 0x0215) and (
                handle == self._pointer_window
            ):
                self._pointer_down = False
                self._pointer_window = 0
            result = self._comctl.DefSubclassProc(handle, message, wparam, lparam)
            navigation = message in (
                con.WM_LBUTTONDOWN,
                con.WM_LBUTTONDBLCLK,
                con.WM_LBUTTONUP,
                con.WM_KEYDOWN,
                con.WM_VSCROLL,
                con.EM_SETSEL,
                con.EM_LINESCROLL,
                con.EM_SCROLLCARET,
                con.WM_CANCELMODE,
                0x0215,
            ) or (message == con.WM_MOUSEMOVE and wparam & con.MK_LBUTTON)
            if navigation and not self._updating:
                self.cancel_animation()
                self._record_user(bubble)
            if message in (con.WM_SETFOCUS, con.WM_KILLFOCUS):
                self._gui.InvalidateRect(bubble.window, None, False)
            return result
        except Exception as error:
            self._failure(error)
            return 0

    def _paint(self, handle: int) -> None:
        gui = self._gui
        dc, paint = gui.BeginPaint(handle)
        try:
            rect = gui.GetClientRect(handle)
            gui.FillRect(dc, rect, self._brushes["host"])
            bubble = self._by_window.get(handle)
            if bubble is not None:
                focus = gui.GetFocus() == bubble.editor
                brush = gui.SelectObject(dc, self._brushes[bubble.entry.role])
                pen = gui.SelectObject(dc, self._pens["focus" if focus else "border"])
                try:
                    diameter = max(2, round(16 * self._scale))
                    gui.RoundRect(dc, *rect, diameter, diameter)
                finally:
                    gui.SelectObject(dc, pen)
                    gui.SelectObject(dc, brush)
        finally:
            gui.EndPaint(handle, paint)

    def _procedure(self, handle, message, wparam, lparam):
        gui, con = self._gui, self._con
        try:
            if message == con.WM_NCDESTROY:
                self._by_window.pop(handle, None)
                self._roles = {key: role for key, role in self._roles.items() if key != handle}
                if handle == self.hwnd:
                    self.hwnd = 0
                    self._roles = {}
                    self._arrivals.clear()
                return gui.DefWindowProc(handle, message, wparam, lparam)
            if message == con.WM_PAINT:
                self._paint(handle)
                return 0
            if message == con.WM_ERASEBKGND:
                gui.FillRect(wparam, gui.GetClientRect(handle), self._brushes["host"])
                return 1
            if message in (con.WM_CTLCOLORSTATIC, con.WM_CTLCOLOREDIT):
                bubble = self._by_window.get(handle)
                color = (
                    USER_BACKGROUND
                    if bubble and bubble.entry.role == "user"
                    else (ASSISTANT_BACKGROUND if bubble else BACKGROUND)
                )
                is_label = bubble is None or lparam == bubble.label
                gui.SetTextColor(wparam, self._api.RGB(*(LABEL_COLOR if is_label else TEXT_COLOR)))
                gui.SetBkColor(wparam, self._api.RGB(*color))
                return self._brushes[bubble.entry.role if bubble else "host"]
            if message == con.WM_SIZE and handle == self.hwnd and not self._updating:
                _, _, width, height = gui.GetClientRect(handle)
                self.reflow(width, height)
                return 0
            if message == con.WM_GETDLGCODE and handle == self.hwnd:
                return con.DLGC_WANTARROWS
            if message == con.WM_MOUSEWHEEL:
                self.wheel(ctypes.c_short((wparam >> 16) & 0xFFFF).value, self._wheel_lines())
                return 0
            if message == con.WM_KEYDOWN and handle == self.hwnd:
                command = {
                    con.VK_UP: 0,
                    con.VK_DOWN: 1,
                    con.VK_PRIOR: 2,
                    con.VK_NEXT: 3,
                    con.VK_HOME: 6,
                    con.VK_END: 7,
                }.get(wparam)
                if command is not None:
                    self.scroll_command(command)
                    return 0
            if message == con.WM_CANCELMODE:
                self.cancel_interaction()
                return 0
        except Exception as error:
            self._failure(error)
            return 0
        return gui.DefWindowProc(handle, message, wparam, lparam)
