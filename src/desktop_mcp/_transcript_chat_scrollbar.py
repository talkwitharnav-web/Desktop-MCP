"""Private, owner-painted inner bars; the message text remains in native EDITs."""

from __future__ import annotations

import ctypes
from typing import TYPE_CHECKING

from desktop_mcp.transcript_scroll import dragged_position, thumb_geometry

if TYPE_CHECKING:
    from desktop_mcp.transcript_chat_native import NativeChatHistory, _Bubble


class InnerScrollbars:
    """One bounded interaction state for the chat's per-message scrollbar HWNDs."""

    def __init__(self, owner: NativeChatHistory) -> None:
        self.owner = owner
        self.held = 0
        self._grab: float | None = None
        self._origin: tuple[int, int] | None = None

    def sync(self, bubble: _Bubble) -> None:
        state = self.owner._text_scroll_state(bubble)
        if state != bubble.scroll_state:
            bubble.scroll_state = state
            self.owner._gui.InvalidateRect(bubble.scrollbar, None, False)

    def cancel(
        self, handle: int | None = None, *, record: bool = False, repaint: bool = True
    ) -> None:
        if not self.held or (handle is not None and handle != self.held):
            return
        owner, held = self.owner, self.held
        self.held, self._grab, self._origin = 0, None, None
        if owner._gui.GetCapture() == held:
            owner._gui.ReleaseCapture()
        if repaint and not owner._closing and owner._gui.IsWindow(held):
            owner._gui.InvalidateRect(held, None, False)
        if record and (bubble := owner._by_scrollbar.get(held)) is not None:
            owner._record_user(bubble)

    def _command(self, bubble: _Bubble, command: int) -> bool:
        state = self.owner._text_scroll_state(bubble)
        positions = {
            0: state.position - 1,
            1: state.position + 1,
            2: state.position - state.page_step,
            3: state.position + state.page_step,
            6: 0,
            7: state.maximum,
        }
        if command not in positions:
            return False
        self.owner._scroll_text_to(bubble, positions[command])
        return True

    def _drag(self, bubble: _Bubble, y: int) -> None:
        if self._grab is None:
            return
        owner = self.owner
        state = owner._text_scroll_state(bubble)
        height = owner._gui.GetClientRect(bubble.scrollbar)[3]
        thumb = thumb_geometry(state, height, owner._scale)
        owner._scroll_text_to(
            bubble, dragged_position(state, thumb, y, self._grab, origin=self._origin)
        )

    def procedure(self, handle: int, message: int, wparam: int, lparam: int) -> int:
        owner = self.owner
        gui, con = owner._gui, owner._con
        bubble = owner._by_scrollbar[handle]
        if message == con.WM_NCDESTROY:
            self.cancel(handle, repaint=False)
            owner._by_scrollbar.pop(handle, None)
            owner._roles = {key: role for key, role in owner._roles.items() if key != handle}
            return gui.DefWindowProc(handle, message, wparam, lparam)
        if message == con.WM_PAINT:
            self._paint(bubble)
            return 0
        if message == con.WM_ERASEBKGND:
            gui.FillRect(wparam, gui.GetClientRect(handle), owner._brushes[bubble.entry.role])
            return 1
        if message == con.WM_GETDLGCODE:
            return con.DLGC_WANTARROWS
        if message in (con.WM_CANCELMODE, con.WM_KILLFOCUS, 0x0215) or (
            message == con.WM_SHOWWINDOW and not wparam
        ):
            self.cancel(handle, record=True)
            gui.InvalidateRect(handle, None, False)
            gui.InvalidateRect(bubble.window, None, False)
            return 0
        if message == con.WM_SETFOCUS:
            gui.InvalidateRect(handle, None, False)
            gui.InvalidateRect(bubble.window, None, False)
            return 0
        if message == con.WM_MOUSEWHEEL:
            owner._wheel_text(bubble, ctypes.c_short((wparam >> 16) & 0xFFFF).value)
            return 0
        if message == con.WM_KEYDOWN:
            control = owner._api.GetKeyState(con.VK_CONTROL) < 0
            if control and wparam in (ord("A"), ord("C")):
                if wparam == ord("A"):
                    gui.SendMessage(bubble.editor, con.EM_SETSEL, 0, -1)
                else:
                    gui.SendMessage(bubble.editor, con.WM_COPY, 0, 0)
                return 0
            if (
                control
                and wparam in (con.VK_PRIOR, con.VK_NEXT)
                and (owner._api.GetKeyState(con.VK_SHIFT) >= 0)
            ):
                self.cancel(handle)
                owner._focus_message(bubble.entry.sequence, -1 if wparam == con.VK_PRIOR else 1)
                return 0
            if wparam == con.VK_ESCAPE:
                self.cancel(handle, record=True)
                return 0
            command = {
                con.VK_UP: 0,
                con.VK_DOWN: 1,
                con.VK_PRIOR: 2,
                con.VK_NEXT: 3,
                con.VK_HOME: 6,
                con.VK_END: 7,
            }.get(wparam)
            if command is not None:
                self._command(bubble, command)
                return 0
        if message == con.WM_CHAR and wparam in (1, 3):
            return 0
        if message == con.WM_VSCROLL and self._command(bubble, wparam & 0xFFFF):
            return 0
        y = ctypes.c_short((lparam >> 16) & 0xFFFF).value
        if message in (con.WM_LBUTTONDOWN, con.WM_LBUTTONDBLCLK):
            captured = gui.GetCapture()
            if captured and captured != handle:
                return 0
            if self.held:
                return 0
            state = owner._text_scroll_state(bubble)
            if not state.maximum:
                return 0
            owner.cancel_animation()
            gui.SetFocus(handle)
            if gui.GetCapture() not in (0, handle):
                return 0
            gui.SetCapture(handle)
            if gui.GetCapture() != handle:
                return 0
            self.held = handle
            thumb = thumb_geometry(state, gui.GetClientRect(handle)[3], owner._scale)
            if thumb.top <= y < thumb.bottom:
                self._grab = thumb.grab_fraction(y)
                self._origin = y, state.position
                owner._notify()
            else:
                self._grab = None
                self._origin = None
                self._command(bubble, 2 if y < thumb.top else 3)
            gui.InvalidateRect(handle, None, False)
            return 0
        if message in (con.WM_MOUSEMOVE, con.WM_LBUTTONUP) and self.held == handle:
            if gui.GetCapture() != handle:
                self.cancel(handle, record=True)
                return 0
            if message == con.WM_LBUTTONUP:
                self._drag(bubble, y)
                self.cancel(handle, record=True)
            elif wparam & con.MK_LBUTTON:
                self._drag(bubble, y)
            else:
                self.cancel(handle, record=True)
            return 0
        return gui.DefWindowProc(handle, message, wparam, lparam)

    def _paint(self, bubble: _Bubble) -> None:
        owner = self.owner
        gui, con, handle = owner._gui, owner._con, bubble.scrollbar
        dc, paint = gui.BeginPaint(handle)
        try:
            rect = gui.GetClientRect(handle)
            gui.FillRect(dc, rect, owner._brushes[bubble.entry.role])
            width, height = rect[2], rect[3]
            state = owner._text_scroll_state(bubble)
            thumb = thumb_geometry(state, height, owner._scale)
            pill_width = min(width, max(1, round(6 * owner._scale)))
            left = (width - pill_width) // 2
            pen = gui.SelectObject(dc, gui.GetStockObject(con.NULL_PEN))
            try:
                for top, bottom, name in (
                    (thumb.track_top, thumb.track_top + thumb.track_length, "scroll-track"),
                    (
                        thumb.top,
                        thumb.bottom,
                        "scroll-active"
                        if self.held == handle or gui.GetFocus() == handle
                        else "scroll-thumb",
                    ),
                ):
                    brush = gui.SelectObject(dc, owner._brushes[name])
                    try:
                        gui.RoundRect(
                            dc, left, top, left + pill_width, bottom, pill_width, pill_width
                        )
                    finally:
                        gui.SelectObject(dc, brush)
            finally:
                gui.SelectObject(dc, pen)
        finally:
            gui.EndPaint(handle, paint)
