"""A separately owned GUI process for native input tests, never a user's app."""

from __future__ import annotations

import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import threading
import psutil

_external_windows: dict[int, int] = {}


def owned_window_pid(handle: int) -> int:
    import win32process

    process = win32process.GetWindowThreadProcessId(handle)[1]
    assert process == os.getpid() or _external_windows.get(handle) == process, (
        "The window is not owned by this test or its explicitly created GUI process"
    )
    return process


def serve_fixture() -> None:
    from tests.test_desktop_live import FixtureWindow

    fixture = FixtureWindow()
    try:
        fixture.start()
        print(
            json.dumps({"pid": os.getpid(), "hwnd": fixture.hwnd, "editor": fixture.editor}),
            flush=True,
        )
        for line in sys.stdin:
            command = json.loads(line)
            if command == "close":
                break
            if command != "events":
                raise ValueError("Unsupported owned-fixture command")
            print(json.dumps(fixture.events), flush=True)
    finally:
        fixture.close()


class IsolatedFixtureWindow:
    def __init__(self) -> None:
        self.process: subprocess.Popen | None = None
        self.pid: int | None = None
        self._gui_process: psutil.Process | None = None
        self.hwnd: int | None = None
        self.editor: int | None = None
        self._reader: threading.Thread | None = None
        self._lines: queue.Queue[str | None] = queue.Queue()

    def _read_lines(self) -> None:
        try:
            for line in self.process.stdout:
                self._lines.put(line)
        finally:
            self._lines.put(None)

    def _receive(self):
        try:
            line = self._lines.get(timeout=5)
        except queue.Empty as error:
            raise RuntimeError("The owned GUI fixture did not respond in five seconds") from error
        if line is None:
            raise RuntimeError("The owned GUI fixture closed before replying")
        return json.loads(line)

    def start(self) -> None:
        import win32process

        self.process = subprocess.Popen(
            [
                sys.executable,
                "-c",
                "from tests.desktop_live_fixture import serve_fixture; serve_fixture()",
            ],
            cwd=Path(__file__).resolve().parent.parent,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
        )
        self._reader = threading.Thread(target=self._read_lines, name="Owned fixture protocol")
        self._reader.start()
        ready = self._receive()
        candidate = psutil.Process(ready["pid"])
        assert candidate.pid == self.process.pid or candidate.ppid() == self.process.pid, (
            "The GUI is not this test's launched interpreter or its direct venv-launcher child"
        )
        self._gui_process, self.pid = candidate, candidate.pid
        self.hwnd, self.editor = ready["hwnd"], ready["editor"]
        assert self.hwnd and self.editor and self.hwnd != self.editor
        for handle in (self.hwnd, self.editor):
            assert win32process.GetWindowThreadProcessId(handle)[1] == self.pid
            _external_windows[handle] = self.pid

    @property
    def events(self):
        self.process.stdin.write(json.dumps("events") + "\n")
        self.process.stdin.flush()
        return self._receive()

    def text(self) -> str:
        from tests.test_desktop_launch_live import control_text

        owned_window_pid(self.editor)
        return control_text(self.editor)

    def _terminate_owned(self) -> None:
        if self._gui_process is not None:
            candidates = [self._gui_process]
        elif self.process.poll() is None:
            candidates = psutil.Process(self.process.pid).children(recursive=True)
        else:
            candidates = []
        for candidate in candidates:
            try:
                if candidate.is_running():
                    candidate.terminate()
                    candidate.wait(timeout=5)
            except psutil.NoSuchProcess:
                pass
        if self.process.poll() is None:
            self.process.kill()
            self.process.wait(timeout=5)

    def close(self) -> None:
        if self.process is None:
            return
        process = self.process
        graceful = True
        try:
            if process.poll() is None or (
                self._gui_process is not None and self._gui_process.is_running()
            ):
                try:
                    process.stdin.write(json.dumps("close") + "\n")
                    process.stdin.flush()
                except (OSError, ValueError):
                    graceful = False
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    graceful = False
                    self._terminate_owned()
            if self._reader is not None:
                self._reader.join(3)
                assert not self._reader.is_alive(), "The owned fixture reader did not exit"
            stderr = process.stderr.read()
            assert graceful, "The owned GUI fixture required termination instead of closing"
            assert process.returncode == 0, stderr
        finally:
            try:
                if process.poll() is None:
                    self._terminate_owned()
                if self._reader is not None:
                    self._reader.join(3)
            finally:
                for handle in (self.hwnd, self.editor):
                    if (
                        handle is not None
                        and self.pid is not None
                        and _external_windows.get(handle) == self.pid
                    ):
                        _external_windows.pop(handle)
                for stream in (process.stdin, process.stdout, process.stderr):
                    stream.close()
                self.process = None
