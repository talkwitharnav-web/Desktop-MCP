"""Searchable per-user Windows launcher, without a console or another controller."""

from __future__ import annotations

import asyncio
from pathlib import Path
import sys


async def open_interface() -> None:
    from desktop_mcp.service import ensure_host

    channel, _ = await ensure_host(show=True)
    channel.close()


def main() -> None:
    """GUI executable entrypoint; show actionable errors rather than disappearing."""
    try:
        asyncio.run(open_interface())
    except (OSError, RuntimeError, ValueError) as error:
        import win32con
        import win32gui

        win32gui.MessageBox(
            0, str(error), "Desktop-MCP could not open", win32con.MB_OK | win32con.MB_ICONERROR
        )
        raise SystemExit(1) from error


def install_shortcut() -> Path:
    """Create/update our own per-user Start-menu shortcut; no machine-wide changes."""
    import pythoncom
    from PIL import Image, ImageDraw
    from platformdirs import user_data_dir
    from win32com.client import Dispatch
    from win32com.shell import shell, shellcon

    from desktop_mcp.cursor import render_cursor

    executable = Path(sys.executable).with_name("desktop-mcp-ui.exe")
    if not executable.is_file():
        raise FileNotFoundError(
            "The GUI launcher is missing. Run python -m uv sync --frozen in the project first."
        )
    programs = Path(shell.SHGetFolderPath(0, shellcon.CSIDL_PROGRAMS, None, 0))
    shortcut_path = programs / "Desktop-MCP.lnk"
    pythoncom.CoInitialize()
    try:
        shortcut = Dispatch("WScript.Shell").CreateShortcut(str(shortcut_path))
        if shortcut_path.exists() and Path(shortcut.TargetPath).resolve() != executable.resolve():
            raise FileExistsError(
                "A different Desktop-MCP shortcut already exists. It was not overwritten."
            )
        data = Path(user_data_dir("Desktop-MCP", appauthor=False))
        data.mkdir(parents=True, exist_ok=True)
        icon_path = data / "desktop-mcp.ico"
        with Image.new("RGBA", (128, 128)) as icon:
            ImageDraw.Draw(icon).rounded_rectangle((4, 4, 123, 123), radius=28, fill="#dddddd")
            sprite = render_cursor(dpi=240)
            # The renderer's own monochrome arrow is also the application icon.
            with sprite.image as image:
                icon.alpha_composite(image, ((128 - image.width) // 2, (128 - image.height) // 2))
            icon.save(icon_path, format="ICO", sizes=[(16, 16), (32, 32), (48, 48), (128, 128)])
        shortcut.TargetPath = str(executable)
        shortcut.Arguments = ""
        shortcut.WorkingDirectory = str(executable.parent)
        shortcut.WindowStyle = 1
        shortcut.Description = "Desktop-MCP - local desktop control and teaching"
        shortcut.IconLocation = f"{icon_path},0"
        shortcut.Save()
        shell.SHChangeNotify(shellcon.SHCNE_CREATE, shellcon.SHCNF_PATHW, str(shortcut_path), None)
    finally:
        pythoncom.CoUninitialize()
    return shortcut_path
