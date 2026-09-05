import subprocess
import json
import sys
import weakref
from dataclasses import replace
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

    app.controller = SimpleNamespace(checkpoint=checkpoint, input_revision=0)
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
    app.controller = SimpleNamespace(checkpoint=lambda: None, input_revision=0)
    assert app.accessibility_tree(use_dom=True) == "button"
    assert calls[0][-3:] == ["--window", "123", "--dom"]


@pytest.mark.parametrize("change", ["window", "input"])
def test_pinned_accessibility_ticket_is_checked_before_worker_creation(monkeypatch, change):
    context = CaptureContext(123, (0, 0, 100, 100), (0, 0, 100, 100))
    current = replace(context, window_id=456) if change == "window" else context
    app = DesktopApplication.__new__(DesktopApplication)
    app.capture = SimpleNamespace(context=lambda: current)
    app.controller = SimpleNamespace(checkpoint=lambda: None, input_revision=int(change == "input"))
    monkeypatch.setattr(
        subprocess, "Popen", lambda *args, **kwargs: pytest.fail("A stale ticket launched UIA")
    )
    with pytest.raises(RuntimeError, match="changed"):
        app.accessibility_tree(expected_context=context, expected_input_revision=0)


def test_input_revision_cancels_and_reaps_only_the_owned_accessibility_worker(monkeypatch):
    context = CaptureContext(123, (0, 0, 100, 100), (0, 0, 100, 100))
    app = DesktopApplication.__new__(DesktopApplication)
    app.capture = SimpleNamespace(context=lambda: context)
    app.controller = SimpleNamespace(checkpoint=lambda: None, input_revision=0)
    worker = SimpleNamespace(returncode=None, killed=False)

    def communicate(timeout):
        if worker.killed:
            return "", ""
        app.controller.input_revision += 1
        raise subprocess.TimeoutExpired("owned UIA worker", timeout)

    def kill():
        worker.killed = True
        worker.returncode = -1

    worker.communicate, worker.kill = communicate, kill
    worker.poll = lambda: worker.returncode
    monkeypatch.setattr(subprocess, "Popen", lambda *args, **kwargs: worker)
    with pytest.raises(RuntimeError, match="Input changed"):
        app.accessibility_tree(expected_context=context, expected_input_revision=0)
    assert worker.killed


def test_pinned_accessibility_keeps_the_five_second_worker_bound(monkeypatch):
    from desktop_mcp import app as app_module

    context = CaptureContext(123, (0, 0, 100, 100), (0, 0, 100, 100))
    application = DesktopApplication.__new__(DesktopApplication)
    application.capture = SimpleNamespace(context=lambda: context)
    application.controller = SimpleNamespace(checkpoint=lambda: None, input_revision=0)
    clock = [0.0]
    worker = SimpleNamespace(returncode=None, killed=False)

    def communicate(timeout):
        if worker.killed:
            return "", ""
        clock[0] += 6
        raise subprocess.TimeoutExpired("owned UIA worker", timeout)

    def kill():
        worker.killed = True
        worker.returncode = -1

    worker.communicate, worker.kill = communicate, kill
    worker.poll = lambda: worker.returncode
    monkeypatch.setattr(subprocess, "Popen", lambda *args, **kwargs: worker)
    monkeypatch.setattr(app_module.time, "monotonic", lambda: clock[0])
    with pytest.raises(TimeoutError, match="timed out"):
        application.accessibility_tree(expected_context=context, expected_input_revision=0)
    assert worker.killed


def test_worker_retains_the_desktop_behind_the_trees_weak_reference(monkeypatch, capsys):
    from desktop_mcp.accessibility import main
    import comtypes
    import windows_mcp.desktop.service as desktop_module

    class Tree:
        def __init__(self, desktop):
            self.desktop = weakref.proxy(desktop)

        def get_state(self, window, others, use_dom):
            assert self.desktop.is_browser
            assert (window, others, use_dom) == (123, [], True)
            return SimpleNamespace(status=True, semantic_tree_to_string=lambda: "browser DOM")

    class Desktop:
        def __init__(self):
            self.is_browser = True
            self.tree = Tree(self)

    monkeypatch.setattr(desktop_module, "Desktop", Desktop)
    monkeypatch.setattr(comtypes, "CoInitialize", lambda: None)
    monkeypatch.setattr(comtypes, "CoUninitialize", lambda: None)
    monkeypatch.setattr(sys, "argv", ["accessibility", "--window", "123", "--dom"])
    main()
    assert json.loads(capsys.readouterr().out)["tree"] == "browser DOM"
