import subprocess
from types import SimpleNamespace

import pytest

from desktop_mcp.app import DesktopApplication
from desktop_mcp.contracts import CaptureContext
from desktop_mcp.runtime import DesktopStopped


def test_accessibility_stop_kills_only_its_owned_fake_worker(monkeypatch):
    worker = SimpleNamespace(returncode=None, killed=False, calls=0)

    def communicate(timeout):
        worker.calls += 1
        if worker.killed:
            return "", ""
        raise subprocess.TimeoutExpired("owned UIA worker", timeout)

    def kill():
        worker.killed = True
        worker.returncode = -1

    worker.communicate = communicate
    worker.kill = kill
    worker.poll = lambda: worker.returncode
    monkeypatch.setattr(subprocess, "Popen", lambda *args, **kwargs: worker)
    context = CaptureContext(123, (0, 0, 100, 100), (0, 0, 100, 100))
    app = DesktopApplication.__new__(DesktopApplication)
    app.capture = SimpleNamespace(context=lambda: context)

    def checkpoint():
        if worker.calls:
            raise DesktopStopped("stopped")

    app.controller = SimpleNamespace(checkpoint=checkpoint)
    with pytest.raises(DesktopStopped):
        app.accessibility_tree()
    assert worker.killed
    assert worker.calls == 2


def test_accessibility_inspects_only_the_requested_window(monkeypatch):
    calls = []
    worker = SimpleNamespace(
        returncode=0,
        communicate=lambda timeout: ('{"tree":"button"}', ""),
        poll=lambda: 0,
    )

    def start(args, **kwargs):
        calls.append(args)
        return worker

    monkeypatch.setattr(subprocess, "Popen", start)
    app = DesktopApplication.__new__(DesktopApplication)
    app.capture = SimpleNamespace(
        context=lambda: CaptureContext(123, (0, 0, 100, 100), (0, 0, 100, 100))
    )
    app.controller = SimpleNamespace(checkpoint=lambda: None)
    assert app.accessibility_tree(use_dom=True) == "button"
    assert calls[0][-3:] == ["--window", "123", "--dom"]
