"""Local-only Desktop-MCP command-line entry point."""

import logging
import os
import asyncio
import json

import click

from desktop_mcp import __version__


@click.group(invoke_without_command=True)
@click.version_option(version=__version__, prog_name="Desktop-MCP")
@click.pass_context
def main(ctx: click.Context) -> None:
    """Supervised Windows desktop control with a local emergency stop."""
    if ctx.invoked_subcommand is None:
        ctx.invoke(open_command)


@main.command()
@click.option("--transport", type=click.Choice(["stdio"]), default="stdio", show_default=True)
@click.option("--debug", is_flag=True, help="Log diagnostics to stderr, never the MCP stream.")
def serve(transport: str, debug: bool) -> None:
    """Connect Copilot's stdio to the single local desktop application."""
    logging.basicConfig(level=logging.DEBUG if debug else logging.WARNING)
    from desktop_mcp.stdio_bridge import run_bridge

    try:
        asyncio.run(run_bridge())
    except (OSError, RuntimeError, ValueError) as error:
        raise click.ClickException(str(error)) from error


@main.command("open")
def open_command() -> None:
    """Open or reveal Desktop-MCP, without starting a duplicate controller."""
    from desktop_mcp.launcher import main as open_gui

    open_gui()


@main.command("install-shortcut")
def install_shortcut_command() -> None:
    """Add Desktop-MCP to Windows Start search for the current user."""
    from desktop_mcp.launcher import install_shortcut

    click.echo(str(install_shortcut()))


@main.command()
def doctor() -> None:
    """Show local connection/exit status without starting or arming the app."""
    from desktop_mcp.service import doctor as read_status

    click.echo(json.dumps(asyncio.run(read_status()), indent=2))


@main.command(hidden=True)
def host() -> None:
    """Internal single-instance GUI host."""
    os.environ["ANONYMIZED_TELEMETRY"] = "false"
    os.environ["WINDOWS_MCP_DISABLE_FLASH"] = "1"
    os.environ["WINDOWS_MCP_WATCHDOG"] = "off"
    logging.basicConfig(level=logging.WARNING)
    from desktop_mcp.service import run_host

    asyncio.run(run_host())


if __name__ == "__main__":
    main()
