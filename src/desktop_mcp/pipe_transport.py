"""Current-user, local-session named pipes with cancellable overlapped I/O."""

from __future__ import annotations

import asyncio
from contextlib import AbstractContextManager
import ctypes
from ctypes import wintypes
import hashlib
import time

import anyio
import pywintypes
import win32api
import win32con
import win32event
import win32file
import win32pipe
import win32security
import win32ts
import winerror

PROTOCOL = 1
MAX_PACKET = 8 * 1024 * 1024
_CHUNK = 64 * 1024
_CLOSED = {
    winerror.ERROR_BROKEN_PIPE,
    winerror.ERROR_NO_DATA,
    winerror.ERROR_PIPE_NOT_CONNECTED,
    winerror.ERROR_OPERATION_ABORTED,
    winerror.ERROR_INVALID_HANDLE,
}
_cancel_io = ctypes.WinDLL("kernel32", use_last_error=True).CancelIoEx
_cancel_io.argtypes, _cancel_io.restype = [wintypes.HANDLE, ctypes.c_void_p], wintypes.BOOL


def current_identity() -> tuple[str, int]:
    """Do not share a desktop between Windows accounts or interactive sessions."""
    token = win32security.OpenProcessToken(win32api.GetCurrentProcess(), win32con.TOKEN_QUERY)
    try:
        sid, _ = win32security.GetTokenInformation(token, win32security.TokenUser)
    finally:
        token.Close()
    return (
        win32security.ConvertSidToStringSid(sid),
        win32ts.ProcessIdToSessionId(win32api.GetCurrentProcessId()),
    )


def channel_name() -> str:
    sid, session = current_identity()
    identity = hashlib.sha256(f"{sid}:{session}".encode("ascii")).hexdigest()[:24]
    return f"Desktop-MCP-v{PROTOCOL}-{identity}"


def security_attributes():
    sid, _ = current_identity()
    descriptor = win32security.ConvertStringSecurityDescriptorToSecurityDescriptor(
        f"D:P(A;;GA;;;{sid})(A;;GA;;;SY)", win32security.SDDL_REVISION_1
    )
    attributes = pywintypes.SECURITY_ATTRIBUTES()
    attributes.SECURITY_DESCRIPTOR = descriptor
    attributes.bInheritHandle = False
    return attributes


class InstanceLock(AbstractContextManager):
    """A Windows mutex is released by the OS even after an unexpected exit."""

    def __init__(self, name: str, purpose: str, timeout: float = 0.0) -> None:
        self._handle = win32event.CreateMutex(
            security_attributes(), False, f"Local\\{name}-{purpose}"
        )
        self._owned = False
        outcome = win32event.WaitForSingleObject(self._handle, round(timeout * 1000))
        if outcome in (win32event.WAIT_OBJECT_0, win32event.WAIT_ABANDONED):
            self._owned = True
        else:
            self._handle.Close()
            self._handle = None
            raise TimeoutError("Desktop-MCP is already starting or running.")

    def __exit__(self, *args) -> None:
        if self._handle is not None:
            if self._owned:
                win32event.ReleaseMutex(self._handle)
            self._handle.Close()
            self._handle = None


async def _complete(handle, overlapped) -> int:
    """Keep overlapped buffers alive until Windows acknowledges cancellation."""
    try:
        while win32event.WaitForSingleObject(overlapped.hEvent, 0) == win32event.WAIT_TIMEOUT:
            await asyncio.sleep(0.005)
        return win32file.GetOverlappedResult(handle, overlapped, False)
    except asyncio.CancelledError:
        if not _cancel_io(int(handle), None):
            error = ctypes.get_last_error()
            if error not in _CLOSED | {winerror.ERROR_NOT_FOUND}:
                raise ctypes.WinError(error)
        # CancelIoEx is asynchronous: do not release its buffer/event before completion.
        with anyio.CancelScope(shield=True):
            while win32event.WaitForSingleObject(overlapped.hEvent, 0) == win32event.WAIT_TIMEOUT:
                await asyncio.sleep(0.005)
        raise


class PipeChannel:
    def __init__(self, handle) -> None:
        self.handle = handle
        self._send_lock = asyncio.Lock()

    def close(self) -> None:
        if self.handle is not None:
            self.handle.Close()
            self.handle = None

    async def send(self, packet: bytes) -> None:
        if not 0 < len(packet) <= MAX_PACKET:
            raise ValueError("Desktop-MCP messages must be nonempty and at most 8 MiB.")
        async with self._send_lock:
            overlapped = pywintypes.OVERLAPPED()
            overlapped.hEvent = win32event.CreateEvent(None, True, False, None)
            try:
                win32file.WriteFile(self.handle, packet, overlapped)
                written = await _complete(self.handle, overlapped)
                if written != len(packet):
                    raise OSError("Windows did not write the complete MCP message.")
            except pywintypes.error as error:
                if error.winerror in _CLOSED:
                    raise EOFError("Desktop-MCP closed its connection.") from error
                raise
            finally:
                overlapped.hEvent.Close()

    async def receive(self) -> bytes:
        parts: list[bytes] = []
        size = 0
        while True:
            overlapped = pywintypes.OVERLAPPED()
            overlapped.hEvent = win32event.CreateEvent(None, True, False, None)
            buffer = win32file.AllocateReadBuffer(_CHUNK)
            try:
                win32file.ReadFile(self.handle, buffer, overlapped)
                try:
                    count = await _complete(self.handle, overlapped)
                    more = False
                except pywintypes.error as error:
                    if error.winerror != winerror.ERROR_MORE_DATA:
                        raise
                    count, more = _CHUNK, True
                size += count
                if size > MAX_PACKET:
                    raise ValueError("Desktop-MCP received a message larger than 8 MiB.")
                parts.append(bytes(buffer[:count]))
                if not more:
                    if not size:
                        raise EOFError("Desktop-MCP closed its connection.")
                    return b"".join(parts)
            except pywintypes.error as error:
                if error.winerror in _CLOSED:
                    raise EOFError("Desktop-MCP closed its connection.") from error
                raise
            finally:
                overlapped.hEvent.Close()


class PipeListener:
    def __init__(self, name: str) -> None:
        self.address = f"\\\\.\\pipe\\{name}"
        self._pending = self._new_handle(first=True)

    def _new_handle(self, *, first: bool = False):
        flags = win32pipe.PIPE_ACCESS_DUPLEX | win32file.FILE_FLAG_OVERLAPPED
        if first:
            flags |= 0x00080000  # FILE_FLAG_FIRST_PIPE_INSTANCE
        return win32pipe.CreateNamedPipe(
            self.address,
            flags,
            win32pipe.PIPE_TYPE_MESSAGE
            | win32pipe.PIPE_READMODE_MESSAGE
            | win32pipe.PIPE_WAIT
            | 0x08,  # PIPE_REJECT_REMOTE_CLIENTS
            32,
            _CHUNK,
            _CHUNK,
            1000,
            security_attributes(),
        )

    async def accept(self) -> PipeChannel:
        while True:
            handle = self._pending
            overlapped = pywintypes.OVERLAPPED()
            overlapped.hEvent = win32event.CreateEvent(None, True, False, None)
            try:
                try:
                    status = win32pipe.ConnectNamedPipe(handle, overlapped)
                except pywintypes.error as error:
                    if error.winerror == winerror.ERROR_NO_DATA:
                        # A short-lived client can vanish before accept starts.
                        # Reserve another handle first, keeping the name bound.
                        self._pending = self._new_handle()
                        handle.Close()
                        continue
                    if error.winerror != winerror.ERROR_PIPE_CONNECTED:
                        raise
                else:
                    if status == winerror.ERROR_NO_DATA:
                        self._pending = self._new_handle()
                        handle.Close()
                        continue
                    if status != winerror.ERROR_PIPE_CONNECTED:
                        await _complete(handle, overlapped)
                # Keep another instance bound before handing off the connected handle.
                self._pending = self._new_handle()
                return PipeChannel(handle)
            finally:
                overlapped.hEvent.Close()

    def close(self) -> None:
        if self._pending is not None:
            self._pending.Close()
            self._pending = None


async def connect(name: str, *, timeout: float = 0.0) -> PipeChannel:
    deadline = time.monotonic() + timeout
    while True:
        try:
            handle = win32file.CreateFile(
                f"\\\\.\\pipe\\{name}",
                win32con.GENERIC_READ | win32con.GENERIC_WRITE,
                0,
                None,
                win32con.OPEN_EXISTING,
                win32file.FILE_FLAG_OVERLAPPED,
                None,
            )
            try:
                win32pipe.SetNamedPipeHandleState(
                    handle, win32pipe.PIPE_READMODE_MESSAGE, None, None
                )
            except BaseException:
                handle.Close()
                raise
            return PipeChannel(handle)
        except pywintypes.error as error:
            if error.winerror not in {winerror.ERROR_FILE_NOT_FOUND, winerror.ERROR_PIPE_BUSY}:
                raise
            if time.monotonic() >= deadline:
                raise FileNotFoundError("Desktop-MCP is not running.") from error
            await asyncio.sleep(0.05)
