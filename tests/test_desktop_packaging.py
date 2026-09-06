import json
from pathlib import Path
import tomllib

from click.testing import CliRunner
from fastmcp import Client

from desktop_mcp import __version__
from desktop_mcp.__main__ import main
from desktop_mcp.app import AGENT_GUIDE_URI, create_server, read_agent_guide
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


def test_agent_guide_is_shipped_package_data_and_not_relative_to_cwd(tmp_path, monkeypatch):
    configuration = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert "AGENT_GUIDE.md" in configuration["tool"]["setuptools"]["package-data"]["desktop_mcp"]
    expected = (ROOT / "src" / "desktop_mcp" / "AGENT_GUIDE.md").read_text(encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    assert read_agent_guide() == expected


def test_source_distribution_includes_the_one_step_windows_setup():
    manifest = (ROOT / "MANIFEST.in").read_text(encoding="utf-8")
    for name in ("Setup.cmd", "scripts/setup.ps1", "scripts/configure_copilot.py"):
        assert (ROOT / name).is_file()
        assert f"include {name}" in manifest.splitlines()


async def test_initialization_and_resource_deliver_the_same_agent_guide():
    expected = read_agent_guide()
    async with Client(create_server(FixtureApplication())) as client:
        assert client.initialize_result.instructions == expected
        resources = await client.list_resources()
        assert AGENT_GUIDE_URI in {str(resource.uri) for resource in resources}
        content = await client.read_resource(AGENT_GUIDE_URI)
        assert len(content) == 1
        assert content[0].text == expected
        assert content[0].mimeType == "text/markdown"
        assert (await client.call_tool("DesktopStatus")).data["state"] == "stopped"
