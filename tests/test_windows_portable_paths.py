"""Path resolution only; no real application discovery or audio playback."""

import ast
import ctypes
import os
from pathlib import Path
from types import SimpleNamespace

from win32com.shell import shell, shellcon

from windows_mcp.desktop.service import Desktop


def test_shortcut_discovery_uses_redirected_windows_known_folders(tmp_path, monkeypatch):
    common = tmp_path / "Common Programs"
    personal = tmp_path / "User \u03bb" / "Programs"
    unrelated = tmp_path / "Unrelated working directory"
    for folder in (common, personal, unrelated):
        folder.mkdir(parents=True)
    (common / "Shared App.lnk").touch()
    (personal / "Local App.lnk").touch()
    (unrelated / "Wrong App.lnk").touch()
    monkeypatch.chdir(unrelated)
    monkeypatch.setenv("PROGRAMDATA", str(unrelated))
    monkeypatch.delenv("APPDATA", raising=False)
    folders = {
        shellcon.CSIDL_COMMON_PROGRAMS: str(common),
        shellcon.CSIDL_PROGRAMS: str(personal),
    }
    calls = []

    def known_folder(owner, identifier, token, flags):
        calls.append(identifier)
        return folders[identifier]

    monkeypatch.setattr(shell, "SHGetFolderPath", known_folder)
    desktop = Desktop.__new__(Desktop)
    assert desktop._get_apps_from_shortcuts() == {
        "shared app": str(common / "Shared App.lnk"),
        "local app": str(personal / "Local App.lnk"),
    }
    assert calls == [shellcon.CSIDL_COMMON_PROGRAMS, shellcon.CSIDL_PROGRAMS]


def test_default_wave_path_uses_the_actual_windows_directory_without_playing_audio():
    path = Path(__file__).resolve().parent.parent / "src" / "windows_mcp" / "uia" / "core.py"
    module = ast.parse(path.read_text(encoding="utf-8"))
    function = next(
        node
        for node in module.body
        if isinstance(node, ast.FunctionDef) and node.name == "PlayWaveFile"
    )
    played = []

    def play_sound(path, module, flags):
        played.append(path.value)
        return 1

    namespace = {
        "os": os,
        "win32api": SimpleNamespace(GetWindowsDirectory=lambda: r"R:\System Windows"),
        "ctypes": SimpleNamespace(
            c_wchar_p=ctypes.c_wchar_p,
            c_void_p=ctypes.c_void_p,
            c_uint=ctypes.c_uint,
            windll=SimpleNamespace(winmm=SimpleNamespace(PlaySoundW=play_sound)),
        ),
    }
    exec(compile(ast.Module(body=[function], type_ignores=[]), str(path), "exec"), namespace)
    play = namespace["PlayWaveFile"]
    assert play()
    assert played[-1] == os.path.join(r"R:\System Windows", "Media", "notify.wav")
    assert play(r"Q:\Custom Sound.wav")
    assert played[-1] == r"Q:\Custom Sound.wav"
    assert play(None)
    assert played[-1] is None
    assert play("")
    assert played[-1] is None
