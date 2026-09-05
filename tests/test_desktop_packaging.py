import json
from pathlib import Path
import tomllib

from click.testing import CliRunner
from fastmcp import Client

from desktop_mcp import __version__
from desktop_mcp.__main__ import main
from desktop_mcp.app import create_server
from tests.test_desktop_tools import FixtureApplication

ROOT = Path(__file__).resolve().parent.parent


def test_runtime_and_console_entry_points_match_the_project():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    assert project["version"] == __version__
    assert project["scripts"]["desktop-mcp"] == "desktop_mcp.__main__:main"
    assert project["scripts"]["windows-mcp"] == "desktop_mcp.__main__:main"
    result = CliRunner().invoke(main, ["--version"])
    assert result.exit_code == 0
    assert __version__ in result.output


def test_help_does_not_start_desktop_control():
    result = CliRunner().invoke(main, ["serve", "--help"])
    assert result.exit_code == 0
    assert "stdio" in result.output
    assert "http" not in result.output.casefold()


async def test_extension_manifest_names_the_actual_supervised_surface():
    manifest = json.loads((ROOT / "manifest.json").read_text(encoding="utf-8"))
    async with Client(create_server(FixtureApplication())) as client:
        tools = await client.list_tools()
    assert {tool["name"] for tool in manifest["tools"]} == {tool.name for tool in tools}
    assert manifest["server"]["entry_point"] == "src/desktop_mcp/__main__.py"
    assert manifest["server"]["mcp_config"]["env"]["ANONYMIZED_TELEMETRY"] == "false"
    assert manifest["compatibility"]["runtimes"]["python"] == ">=3.14"
