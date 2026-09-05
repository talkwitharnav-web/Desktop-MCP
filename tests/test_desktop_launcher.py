from pathlib import Path
from types import SimpleNamespace

from PIL import Image
import pytest

from desktop_mcp import launcher


@pytest.fixture
def shortcut_environment(tmp_path, monkeypatch):
    import platformdirs
    import win32com.client
    from win32com.shell import shell

    programs = tmp_path / "Programs"
    programs.mkdir()
    executable = tmp_path / "desktop-mcp-ui.exe"
    executable.touch()
    shortcut_path = programs / "Desktop-MCP.lnk"
    shortcut = SimpleNamespace(
        TargetPath="",
        Arguments="old",
        WindowStyle=0,
        Save=lambda: shortcut_path.write_text("owned fixture shortcut", encoding="utf-8"),
    )
    notifications = []
    monkeypatch.setattr(launcher.sys, "executable", str(tmp_path / "python.exe"))
    monkeypatch.setattr(
        platformdirs, "user_data_dir", lambda *args, **kwargs: str(tmp_path / "data")
    )
    monkeypatch.setattr(shell, "SHGetFolderPath", lambda *args: str(programs))
    monkeypatch.setattr(shell, "SHChangeNotify", lambda *args: notifications.append(args))
    monkeypatch.setattr(
        win32com.client,
        "Dispatch",
        lambda name: SimpleNamespace(CreateShortcut=lambda path: shortcut),
    )
    return executable, shortcut_path, shortcut, notifications


def test_installed_shortcut_launches_the_gui_without_a_console(shortcut_environment):
    executable, path, shortcut, notifications = shortcut_environment
    assert launcher.install_shortcut() == path
    assert path.is_file()
    assert Path(shortcut.TargetPath) == executable
    assert shortcut.Arguments == ""
    assert shortcut.WindowStyle == 1
    assert notifications
    icon_path = Path(shortcut.IconLocation.rsplit(",", 1)[0])
    with Image.open(icon_path) as icon:
        assert icon.format == "ICO"
        assert icon.width == 128


def test_foreign_shortcut_is_not_overwritten(shortcut_environment):
    _, path, shortcut, notifications = shortcut_environment
    path.write_text("not our shortcut", encoding="utf-8")
    shortcut.TargetPath = r"C:\Other Application\desktop-mcp-ui.exe"
    with pytest.raises(FileExistsError, match="not overwritten"):
        launcher.install_shortcut()
    assert path.read_text(encoding="utf-8") == "not our shortcut"
    assert not notifications


def test_our_existing_shortcut_can_be_updated(shortcut_environment):
    executable, path, shortcut, _ = shortcut_environment
    path.write_text("old owned shortcut", encoding="utf-8")
    shortcut.TargetPath = str(executable)
    assert launcher.install_shortcut() == path


def test_shell_object_is_released_before_its_com_apartment(shortcut_environment, monkeypatch):
    import pythoncom
    import win32com.client

    _, path, _, _ = shortcut_environment
    events = []

    class Shortcut:
        TargetPath = ""

        def Save(self):
            path.write_text("owned shortcut", encoding="utf-8")

        def __del__(self):
            events.append("release")

    monkeypatch.setattr(pythoncom, "CoInitialize", lambda: events.append("initialize"))
    monkeypatch.setattr(pythoncom, "CoUninitialize", lambda: events.append("uninitialize"))
    monkeypatch.setattr(
        win32com.client,
        "Dispatch",
        lambda name: SimpleNamespace(CreateShortcut=lambda filename: Shortcut()),
    )
    launcher.install_shortcut()
    assert events == ["initialize", "release", "uninitialize"]
