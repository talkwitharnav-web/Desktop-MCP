"""Content-free, read-only Win32 ownership and input-receiver checks."""

from __future__ import annotations

import ctypes
from copy import deepcopy
from ctypes import wintypes
from dataclasses import dataclass
from datetime import datetime, timezone
from threading import get_native_id
import time

from desktop_mcp.contracts import Point, Rect

_MAX_WINDOWS = 256
_MAX_HIT_WINDOWS = 128
_MAX_CHILDREN = 32
_LAYERED = 0x00080000
_TRANSPARENT = 0x00000020
_MENU_MODES = 0x0004 | 0x0008 | 0x0010


class TargetDenied(RuntimeError):
    """A rejected target, without window titles, control text, or input contents."""

    def __init__(self, message: str, details: dict[str, object]) -> None:
        super().__init__(message)
        self.details = deepcopy(details)


class _QueryFailed(RuntimeError):
    def __init__(self, reason: str, window_id: int | None = None) -> None:
        super().__init__(reason)
        self.window_id = window_id


class GUIThreadInfo(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("flags", wintypes.DWORD),
        ("hwndActive", wintypes.HWND),
        ("hwndFocus", wintypes.HWND),
        ("hwndCapture", wintypes.HWND),
        ("hwndMenuOwner", wintypes.HWND),
        ("hwndMoveSize", wintypes.HWND),
        ("hwndCaret", wintypes.HWND),
        ("rcCaret", wintypes.RECT),
    ]


def configure_window_queries(user32: ctypes.WinDLL, gdi32: ctypes.WinDLL) -> None:
    """Bind only geometry, identity, and routing queries (never text messages)."""
    signatures = {
        "GetAncestor": ([wintypes.HWND, wintypes.UINT], wintypes.HWND),
        "GetWindow": ([wintypes.HWND, wintypes.UINT], wintypes.HWND),
        "GetWindowThreadProcessId": (
            [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)],
            wintypes.DWORD,
        ),
        "GetGUIThreadInfo": (
            [wintypes.DWORD, ctypes.POINTER(GUIThreadInfo)],
            wintypes.BOOL,
        ),
        "WindowFromPoint": ([wintypes.POINT], wintypes.HWND),
        "IsWindowEnabled": ([wintypes.HWND], wintypes.BOOL),
        "GetWindowDisplayAffinity": (
            [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)],
            wintypes.BOOL,
        ),
        "GetLayeredWindowAttributes": (
            [
                wintypes.HWND,
                ctypes.POINTER(wintypes.DWORD),
                ctypes.POINTER(wintypes.BYTE),
                ctypes.POINTER(wintypes.DWORD),
            ],
            wintypes.BOOL,
        ),
        "GetWindowRgn": ([wintypes.HWND, wintypes.HRGN], ctypes.c_int),
        "ScreenToClient": (
            [wintypes.HWND, ctypes.POINTER(wintypes.POINT)],
            wintypes.BOOL,
        ),
        "ChildWindowFromPointEx": (
            [wintypes.HWND, wintypes.POINT, wintypes.UINT],
            wintypes.HWND,
        ),
        "SendMessageTimeoutW": (
            [
                wintypes.HWND,
                wintypes.UINT,
                wintypes.WPARAM,
                wintypes.LPARAM,
                wintypes.UINT,
                wintypes.UINT,
                ctypes.POINTER(ctypes.c_size_t),
            ],
            wintypes.LPARAM,
        ),
    }
    for name, (arguments, result) in signatures.items():
        function = getattr(user32, name)
        function.argtypes, function.restype = arguments, result
    gdi32.CreateRectRgn.argtypes = [ctypes.c_int] * 4
    gdi32.CreateRectRgn.restype = wintypes.HRGN
    gdi32.PtInRegion.argtypes = [wintypes.HRGN, ctypes.c_int, ctypes.c_int]
    gdi32.PtInRegion.restype = wintypes.BOOL
    gdi32.DeleteObject.argtypes = [wintypes.HANDLE]
    gdi32.DeleteObject.restype = wintypes.BOOL


@dataclass(frozen=True)
class _Identity:
    window_id: int
    root_id: int
    owner_root_id: int
    thread_id: int
    process_id: int
    root_process_id: int
    owner_process_id: int


class WindowTargets:
    """One bounded query snapshot; caches roots, never scans child-window text.

    ``capture_excluded`` reports native display affinity, or None if unavailable.
    The application may additionally exclude these surfaces with its capture guard.
    ``effective_visible`` includes root minimization, not occlusion; receiver
    resolution, rather than this visibility flag, determines pointer protection.
    """

    def __init__(
        self,
        user32: ctypes.WinDLL,
        gdi32: ctypes.WinDLL,
        handles: tuple[int, ...],
        roles: dict[int, str],
        *,
        process_id: int,
    ) -> None:
        if len(handles) > _MAX_WINDOWS or len(roles) > _MAX_WINDOWS:
            raise ValueError("The registered-window query limit was exceeded.")
        self.user32, self.gdi32 = user32, gdi32
        self.process_id = process_id
        self.handles = tuple(dict.fromkeys((*handles, *roles)))
        if len(self.handles) > _MAX_WINDOWS:
            raise ValueError("The registered-window query limit was exceeded.")
        if any(isinstance(handle, bool) or not isinstance(handle, int) for handle in self.handles):
            raise ValueError("Registered window IDs must be integers.")
        if any(
            not isinstance(role, str)
            or not 1 <= len(role) <= 64
            or not all(character in "abcdefghijklmnopqrstuvwxyz0123456789-" for character in role)
            for role in roles.values()
        ):
            raise ValueError("Window roles must be stable lowercase ASCII labels.")
        self.roles = dict(roles)
        self._basics: dict[int, tuple[int, int] | None] = {}
        self._identities: dict[int, _Identity] = {}
        self._styles: dict[int, int] = {}
        self._root_facts: dict[int, tuple[bool, bool, int]] = {}
        self._affinities: dict[int, bool | None] = {}
        self._gui: dict[int, GUIThreadInfo] = {}

    def _basic(self, handle: int) -> tuple[int, int] | None:
        if handle in self._basics:
            return self._basics[handle]
        if not handle or not self.user32.IsWindow(handle):
            self._basics[handle] = None
            return None
        process = wintypes.DWORD()
        thread = int(self.user32.GetWindowThreadProcessId(handle, ctypes.byref(process)))
        if not thread or not process.value:
            if not self.user32.IsWindow(handle):
                self._basics[handle] = None
                return None
            raise _QueryFailed("GetWindowThreadProcessId failed.", handle)
        result = thread, int(process.value)
        self._basics[handle] = result
        return result

    def _identity(self, handle: int) -> _Identity:
        if handle in self._identities:
            return self._identities[handle]
        basic = self._basic(handle)
        if basic is None:
            raise _QueryFailed("The window no longer exists.", handle)
        root = int(self.user32.GetAncestor(handle, 2) or 0)  # GA_ROOT, not the child.
        owner_root = int(self.user32.GetAncestor(handle, 3) or 0)
        root_basic, owner_basic = self._basic(root), self._basic(owner_root)
        if root_basic is None or owner_basic is None:
            raise _QueryFailed("The window's root or owner changed during the query.", handle)
        identity = _Identity(handle, root, owner_root, *basic, root_basic[1], owner_basic[1])
        self._identities[handle] = identity
        return identity

    def _verify_identity(self, identity: _Identity) -> None:
        processes = {
            identity.window_id: identity.process_id,
            identity.root_id: identity.root_process_id,
            identity.owner_root_id: identity.owner_process_id,
        }
        for handle, expected_process in processes.items():
            process = wintypes.DWORD()
            if not self.user32.IsWindow(handle):
                raise _QueryFailed("The target window disappeared during the query.", handle)
            thread = int(self.user32.GetWindowThreadProcessId(handle, ctypes.byref(process)))
            if (
                not thread
                or process.value != expected_process
                or (handle == identity.window_id and thread != identity.thread_id)
            ):
                raise _QueryFailed("The target window identity changed during the query.", handle)
        if (
            int(self.user32.GetAncestor(identity.window_id, 2) or 0) != identity.root_id
            or int(self.user32.GetAncestor(identity.window_id, 3) or 0) != identity.owner_root_id
        ):
            raise _QueryFailed(
                "The target window's root changed during the query.", identity.window_id
            )

    def _owned(self, identity: _Identity) -> bool:
        return self.process_id in (
            identity.process_id,
            identity.root_process_id,
            identity.owner_process_id,
        )

    def _role(self, identity: _Identity) -> str:
        if not self._owned(identity):
            return "external-window"
        if identity.process_id == self.process_id and identity.window_id in self.roles:
            return self.roles[identity.window_id]
        if identity.root_process_id == self.process_id and identity.root_id in self.roles:
            return self.roles[identity.root_id]
        if identity.window_id in self.handles and identity.process_id == self.process_id:
            return "owned-window"
        return "owned-popup"

    def _style(self, handle: int) -> int:
        if handle not in self._styles:
            self._identity(handle)
            ctypes.set_last_error(0)
            style = int(self.user32.GetWindowLongW(handle, -20))
            if not style and ctypes.get_last_error():
                raise _QueryFailed("GetWindowLongW failed.", handle)
            self._styles[handle] = style
        return self._styles[handle]

    def _facts(self, handle: int) -> tuple[bool, bool, int]:
        if handle not in self._root_facts:
            self._root_facts[handle] = (
                bool(self.user32.IsWindowVisible(handle)),
                bool(self.user32.IsIconic(handle)),
                self._style(handle),
            )
        return self._root_facts[handle]

    def _rect(self, handle: int) -> Rect:
        rect = wintypes.RECT()
        if not self.user32.GetWindowRect(handle, ctypes.byref(rect)):
            raise _QueryFailed("GetWindowRect failed or the window disappeared.", handle)
        return rect.left, rect.top, rect.right, rect.bottom

    def _effective(self, identity: _Identity) -> tuple[bool, bool, bool, bool]:
        root_visible, minimized, style = self._facts(identity.root_id)
        if identity.owner_root_id != identity.root_id:
            minimized = minimized or self._facts(identity.owner_root_id)[1]
        visible = bool(self.user32.IsWindowVisible(identity.window_id))
        own_style = self._style(identity.window_id)
        transparent_style = _LAYERED | _TRANSPARENT
        return (
            visible,
            minimized,
            visible and root_visible and not minimized,
            style & transparent_style == transparent_style
            or own_style & transparent_style == transparent_style,
        )

    def _metadata(self, handle: int) -> dict[str, object]:
        result: dict[str, object] = {
            "window_id": handle,
            "root_id": None,
            "role": self.roles.get(
                handle, "owned-window" if handle in self.handles else "unresolved-window"
            ),
            "bounds": None,
            "visible": None,
            "minimized": None,
            "effective_visible": None,
            "click_through": None,
            "capture_excluded": None,
            "status": "unavailable",
        }
        try:
            basic = self._basic(handle)
            if basic is None:
                result.update(status="missing", visible=False, effective_visible=False)
                return result
            identity = self._identity(handle)
            # A recycled foreign HWND is not the old panel, and is not inspected as one.
            if not self._owned(identity) and handle in self.handles:
                result["status"] = "stale"
                return result
            result.update(
                root_id=identity.root_id,
                owner_root_id=identity.owner_root_id,
                role=self._role(identity),
            )
            visible, minimized, effective, click_through = self._effective(identity)
            result.update(
                visible=visible,
                minimized=minimized,
                effective_visible=effective,
                click_through=click_through,
                bounds=self._rect(handle),
            )
            root = identity.root_id
            if root not in self._affinities:
                affinity = wintypes.DWORD()
                available = self.user32.GetWindowDisplayAffinity(root, ctypes.byref(affinity))
                self._affinities[root] = affinity.value == 0x11 if available else None
            self._verify_identity(identity)
            result.update(capture_excluded=self._affinities[root], status="ok")
        except _QueryFailed as error:
            result["reason"] = str(error)
        return result

    def _thread_info(self, thread: int) -> GUIThreadInfo:
        if thread not in self._gui:
            info = GUIThreadInfo(cbSize=ctypes.sizeof(GUIThreadInfo))
            if not self.user32.GetGUIThreadInfo(thread, ctypes.byref(info)):
                raise _QueryFailed("GetGUIThreadInfo could not resolve input routing.")
            self._gui[thread] = info
        return self._gui[thread]

    def _owned_threads(self) -> set[int]:
        threads = set()
        for handle in self.handles:
            basic = self._basic(handle)
            if basic is not None and basic[1] == self.process_id:
                threads.add(basic[0])
        return threads

    def protected_windows(self) -> list[dict[str, object]]:
        """Describe registered surfaces and active owned menus/capture, without text."""
        handles = dict.fromkeys(self.handles)
        foreground = int(self.user32.GetForegroundWindow() or 0)
        foreground_basic = self._basic(foreground)
        if foreground_basic is not None and foreground_basic[1] == self.process_id:
            handles[foreground] = None
        for thread in self._owned_threads():
            info = self._thread_info(thread)
            for handle in (info.hwndActive, info.hwndFocus, info.hwndCapture, info.hwndMenuOwner):
                handle = int(handle or 0)
                basic = self._basic(handle)
                if basic is not None and basic[1] == self.process_id:
                    handles[handle] = None
        if len(handles) > _MAX_WINDOWS:
            raise _QueryFailed("The protected-window metadata limit was exceeded.")
        return [self._metadata(handle) for handle in handles]

    def _deny(
        self,
        code: str,
        operation: str,
        point: Point | None,
        expected: int | None,
        foreground: int,
        *,
        handle: int | None = None,
        routing: str | None = None,
        reason: str | None = None,
    ) -> None:
        matched = self._metadata(handle) if handle else None
        details: dict[str, object] = {
            "code": code,
            "operation": operation,
            "target_point": point,
            "expected_window": expected,
            "actual_foreground": foreground,
            "matched": matched,
            "routing": routing,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        if reason:
            details["reason"] = reason
        if code == "foreground_mismatch":
            message = (
                f"The foreground window changed (expected HWND {expected}, actual HWND "
                f"{foreground}, physical point {point}). Obtain a fresh screenshot."
            )
        elif code == "protected_target":
            message = (
                f"Desktop-MCP cannot target its own {matched['role']} window "
                f"(HWND {handle}, root {matched['root_id']}, physical point {point}, "
                f"bounds {matched['bounds']}, routing {routing})."
            )
        else:
            message = (
                f"Desktop-MCP could not safely resolve the target at physical point {point}: "
                f"{reason} Obtain a fresh observation; do not replay input."
            )
        raise TargetDenied(message, details)

    def _check_owned(
        self,
        handle: int,
        operation: str,
        point: Point | None,
        expected: int | None,
        foreground: int,
        routing: str,
    ) -> None:
        if handle and self._owned(self._identity(handle)):
            self._verify_identity(self._identity(handle))
            self._deny(
                "protected_target",
                operation,
                point,
                expected,
                foreground,
                handle=handle,
                routing=routing,
            )

    def _foreground_checks(
        self, foreground: int, operation: str, point: Point | None, expected: int | None
    ) -> GUIThreadInfo:
        self._check_owned(foreground, operation, point, expected, foreground, "foreground")
        identity = self._identity(foreground)
        info = self._thread_info(identity.thread_id)
        self._check_owned(
            int(info.hwndActive or 0), operation, point, expected, foreground, "keyboard_active"
        )
        self._check_owned(
            int(info.hwndFocus or 0), operation, point, expected, foreground, "keyboard_focus"
        )
        return info

    def ensure_observable_foreground(self) -> int:
        """Reject owned active surfaces without arming or reading text.

        Return zero for no foreground; the capture caller retains its existing
        foreground-unavailable handling and its independent capture permission gate.
        """
        foreground = int(self.user32.GetForegroundWindow() or 0)
        if not foreground:
            return 0
        try:
            self._foreground_checks(foreground, "observe_foreground", None, None)
            if foreground != int(self.user32.GetForegroundWindow() or 0):
                raise _QueryFailed("The foreground changed during the ownership query.")
            self._verify_identity(self._identity(foreground))
        except _QueryFailed as error:
            self._deny(
                "target_indeterminate",
                "observe_foreground",
                None,
                None,
                foreground,
                handle=error.window_id,
                reason=str(error),
            )
        return foreground

    def ensure_focus(self, window_id: int) -> None:
        foreground = int(self.user32.GetForegroundWindow() or 0)
        try:
            self._check_owned(window_id, "focus", None, None, foreground, "requested_window")
            self._verify_identity(self._identity(window_id))
        except _QueryFailed as error:
            self._deny(
                "target_indeterminate",
                "focus",
                None,
                None,
                foreground,
                handle=error.window_id,
                reason=str(error),
            )

    def ensure_target(self, point: Point | None = None, window_id: int | None = None) -> None:
        foreground = int(self.user32.GetForegroundWindow() or 0)
        if window_id is not None and foreground != window_id:
            self._deny("foreground_mismatch", "input", point, window_id, foreground)
        try:
            if not foreground:
                raise _QueryFailed("No foreground window is available.")
            info = self._foreground_checks(foreground, "input", point, window_id)
            foreground_thread = self._identity(foreground).thread_id
            infos = [info]
            infos.extend(
                self._thread_info(thread)
                for thread in self._owned_threads()
                if thread != foreground_thread
            )
            for info in infos:
                for handle, routing in (
                    (info.hwndCapture, "mouse_capture"),
                    (info.hwndMenuOwner if info.flags & _MENU_MODES else 0, "menu_owner"),
                    (info.hwndMoveSize if info.flags & 0x0002 else 0, "move_size"),
                ):
                    self._check_owned(
                        int(handle or 0), "input", point, window_id, foreground, routing
                    )
            if point is not None:
                initial = int(self.user32.WindowFromPoint(wintypes.POINT(*point)) or 0)
                receiver = self._receiver(initial, point)
                self._check_owned(receiver, "input", point, window_id, foreground, "pointer_hit")
                if initial != int(self.user32.WindowFromPoint(wintypes.POINT(*point)) or 0):
                    raise _QueryFailed("The hit window changed during the ownership query.")
                self._verify_identity(self._identity(receiver))
            actual = int(self.user32.GetForegroundWindow() or 0)
            if window_id is not None and actual != window_id:
                self._deny("foreground_mismatch", "input", point, window_id, actual)
            if actual != foreground:
                raise _QueryFailed("The foreground changed during the ownership query.")
            self._verify_identity(self._identity(foreground))
        except _QueryFailed as error:
            self._deny(
                "target_indeterminate",
                "input",
                point,
                window_id,
                foreground,
                handle=error.window_id,
                reason=str(error),
            )

    def _hit_test(self, handle: int, point: Point, deadline: float) -> int:
        if self._identity(handle).thread_id == get_native_id():
            # SendMessageTimeout cannot bound a direct call to this thread's WndProc.
            raise _QueryFailed("The receiver cannot be probed on its own GUI thread.", handle)
        if any(not -32768 <= coordinate <= 32767 for coordinate in point):
            raise _QueryFailed("WM_NCHITTEST cannot represent this physical point.", handle)
        remaining = int((deadline - time.monotonic()) * 1000)
        if remaining <= 0:
            raise _QueryFailed("The overlay receiver query timed out.", handle)
        result = ctypes.c_size_t()
        packed = (point[0] & 0xFFFF) | ((point[1] & 0xFFFF) << 16)
        if not self.user32.SendMessageTimeoutW(
            handle,
            0x0084,
            0,
            packed,
            0x0001 | 0x0002 | 0x0020,
            min(25, remaining),
            ctypes.byref(result),
        ):
            raise _QueryFailed("WM_NCHITTEST did not return a reliable receiver.", handle)
        return ctypes.c_ssize_t(result.value).value

    def _in_region(self, handle: int, point: Point, bounds: Rect) -> bool:
        region = self.gdi32.CreateRectRgn(0, 0, 0, 0)
        if not region:
            raise _QueryFailed("CreateRectRgn failed.", handle)
        try:
            kind = self.user32.GetWindowRgn(handle, region)
            if not kind:  # A valid window without a region uses its rectangular bounds.
                if not self.user32.IsWindow(handle):
                    raise _QueryFailed("The hit window disappeared.", handle)
                return True
            return bool(self.gdi32.PtInRegion(region, point[0] - bounds[0], point[1] - bounds[1]))
        finally:
            if not self.gdi32.DeleteObject(region):
                raise _QueryFailed("The hit-test region could not be released.", handle)

    def _check_layered_shape(self, handle: int, style: int) -> None:
        if not style & _LAYERED:
            return
        color, alpha, flags = wintypes.DWORD(), wintypes.BYTE(), wintypes.DWORD()
        if (
            not self.user32.GetLayeredWindowAttributes(
                handle, ctypes.byref(color), ctypes.byref(alpha), ctypes.byref(flags)
            )
            or flags.value & 1
            or not flags.value & 2
            or alpha.value != 255
        ):
            raise _QueryFailed("A layered window's pixel hit region is indeterminate.", handle)

    def _child_receiver(self, handle: int, point: Point, deadline: float) -> int:
        seen = set()
        for _ in range(_MAX_CHILDREN):
            if handle in seen:
                raise _QueryFailed("The child hit-test traversal cycled.", handle)
            seen.add(handle)
            client = wintypes.POINT(*point)
            if not self.user32.ScreenToClient(handle, ctypes.byref(client)):
                raise _QueryFailed("ScreenToClient failed.", handle)
            child = int(self.user32.ChildWindowFromPointEx(handle, client, 0x0003) or 0)
            if not child or child == handle:
                return handle
            identity = self._identity(child)
            if not self._effective(identity)[2]:
                raise _QueryFailed("The child hit receiver is no longer visible.", child)
            style = self._style(child)
            self._check_layered_shape(child, style)
            if self._hit_test(child, point, deadline) <= 0:
                raise _QueryFailed("A child has ambiguous hit-test routing.", child)
            handle = child
        raise _QueryFailed("The child hit-test traversal limit was exceeded.", handle)

    def _receiver(self, initial: int, point: Point) -> int:
        identity = self._identity(initial)
        _, _, effective, click_through = self._effective(identity)
        if not effective:
            raise _QueryFailed("The reported hit window is hidden or minimized.", initial)
        if not click_through:
            return initial
        style = self._style(identity.root_id)
        if style & (_LAYERED | _TRANSPARENT) != (_LAYERED | _TRANSPARENT):
            raise _QueryFailed("A click-through child has indeterminate sibling routing.", initial)
        # WS_EX_NOACTIVATE is not transparency. Only layered WS_EX_TRANSPARENT
        # passes the whole root through; no protected window is hidden to probe it.
        handle = identity.root_id
        seen = {handle}
        deadline = time.monotonic() + 0.25
        for _ in range(_MAX_HIT_WINDOWS):
            handle = int(self.user32.GetWindow(handle, 2) or 0)  # GW_HWNDNEXT, Z order.
            if not handle:
                raise _QueryFailed("No receiver was found beneath the click-through overlay.")
            if handle in seen:
                raise _QueryFailed("The overlay Z-order traversal cycled.", handle)
            seen.add(handle)
            candidate = self._identity(handle)
            if candidate.root_id != handle:
                raise _QueryFailed("The top-level Z order changed during the query.", handle)
            _, _, effective, click_through = self._effective(candidate)
            if not effective or click_through or not self.user32.IsWindowEnabled(handle):
                continue
            bounds = self._rect(handle)
            if not (bounds[0] <= point[0] < bounds[2] and bounds[1] <= point[1] < bounds[3]):
                continue
            if not self._in_region(handle, point, bounds):
                continue
            self._check_layered_shape(handle, self._facts(handle)[2])
            hit = self._hit_test(handle, point, deadline)
            if hit == 0:
                continue
            if hit < 0:
                raise _QueryFailed("A window has ambiguous transparent hit routing.", handle)
            return self._child_receiver(handle, point, deadline) if hit == 1 else handle
        raise _QueryFailed("The overlay Z-order traversal limit was exceeded.", handle)
