"""Local-only Desktop-MCP command-line entry point."""

import logging
import os

import click

from desktop_mcp import __version__


@click.group(invoke_without_command=True)
@click.version_option(version=__version__, prog_name="Desktop-MCP")
@click.pass_context
def main(ctx: click.Context) -> None:
    """Supervised Windows desktop control with a local emergency stop."""
    if ctx.invoked_subcommand is None:
        ctx.invoke(serve)


@main.command()
@click.option("--transport", type=click.Choice(["stdio"]), default="stdio", show_default=True)
@click.option("--debug", is_flag=True, help="Log diagnostics to stderr, never the MCP stream.")
def serve(transport: str, debug: bool) -> None:
    """Start stopped, with an Alt-Tab-visible local control window."""
    os.environ["ANONYMIZED_TELEMETRY"] = "false"
    os.environ["WINDOWS_MCP_DISABLE_FLASH"] = "1"
    os.environ["WINDOWS_MCP_WATCHDOG"] = "off"
    logging.basicConfig(level=logging.DEBUG if debug else logging.WARNING)
    from desktop_mcp.app import create_server

    create_server().run(transport=transport, show_banner=False)


if __name__ == "__main__":
    main()
