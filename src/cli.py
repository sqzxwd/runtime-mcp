"""Command line interface for runtime-mcp using Typer and Rich."""

from __future__ import annotations

import asyncio
import signal
import sys
from pathlib import Path

import typer
from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from src.server import get_runtime_snapshot, mcp, set_server_state_store
from src.storage.state_store import StateStore
from src.taps.http_tap import HttpTap, normalize_target_url

app = typer.Typer(
    name="runtime-mcp",
    help="Live runtime MCP bridge for AI agents: DB schema, HTTP error proxy, and log stack traces.",
    no_args_is_help=True,
)

console = Console()


def get_status_style(status_code: int) -> str:
    """Return styling based on HTTP status code."""
    if status_code < 300:
        return "bold green"
    if status_code < 400:
        return "bold yellow"
    if status_code < 500:
        return "bold magenta"
    return "bold white on red"


@app.command()
def tap(
    listen: int = typer.Option(
        8090,
        "--listen",
        "-l",
        help="Port for the proxy to listen on.",
    ),
    target: str = typer.Option(
        "3000",
        "--target",
        "-t",
        help="Target backend port or URL (e.g. 3000 or http://localhost:3000).",
    ),
    db: str = typer.Option(
        "",
        "--db",
        "-d",
        help="Path to local SQLite database file to introspect.",
    ),
    log: str = typer.Option(
        "",
        "--log",
        help="Path to application log file to tail.",
    ),
    state_file: str = typer.Option(
        ".runtime_state.json",
        "--state-file",
        help="Path to state file for persistence.",
    ),
) -> None:
    """Start live traffic tap proxy, link DB and logs, and stream requests in terminal."""
    target_normalized = normalize_target_url(target)
    store = StateStore(state_file=state_file)

    if db:
        store.set_db_path(str(Path(db).resolve()))
    if log:
        store.set_log_path(str(Path(log).resolve()))

    banner_content = (
        f"[bold cyan]Proxy Listen:[/bold cyan] http://127.0.0.1:{listen}\n"
        f"[bold cyan]Proxy Target:[/bold cyan] {target_normalized}\n"
        f"[bold cyan]Database Tap:[/bold cyan] {store.get_db_path() or '[dim]Auto-detect / Not set[/dim]'}\n"
        f"[bold cyan]Log Tap:[/bold cyan] {store.get_log_path() or '[dim]Auto-detect / Not set[/dim]'}\n"
        f"[bold cyan]State File:[/bold cyan] {store.state_file}\n\n"
        "[dim]Press Ctrl+C to terminate proxy[/dim]"
    )
    console.print(
        Panel(
            banner_content,
            title="[bold green]runtime-mcp Tap Bridge Active[/bold green]",
            border_style="green",
        )
    )

    def print_request(req: dict) -> None:
        """Render intercepted request row with Rich."""
        time_str = req.get("time_iso", "")
        method = req.get("method", "GET")
        url = req.get("url", "/")
        status = req.get("status_code", 0)
        dur = req.get("duration_ms", 0)
        is_error = req.get("is_error", False)

        method_color = {
            "GET": "cyan",
            "POST": "magenta",
            "PUT": "blue",
            "DELETE": "red",
            "PATCH": "yellow",
        }.get(method, "white")

        status_text = Text(f" {status} ", style=get_status_style(status))
        method_text = Text(f"{method:<6}", style=f"bold {method_color}")

        console.print(
            f"[dim]{time_str}[/dim]  {method_text}  {status_text}  "
            f"[bold]{url}[/bold]  [dim]({dur}ms)[/dim]"
        )

        if is_error:
            # Print error detail box for quick inspection
            resp_body = req.get("response_body")
            detail = req.get("error_detail")
            err_msg = detail or resp_body
            if err_msg:
                console.print(f"   [bold red]↳ Error Body:[/bold red] [dim]{str(err_msg)[:300]}[/dim]")

    async def run_server() -> None:
        http_tap = HttpTap(
            listen_host="127.0.0.1",
            listen_port=listen,
            target_url=target_normalized,
            state_store=store,
            on_request=print_request,
        )
        await http_tap.start()

        # Handle shutdown gracefully
        stop_event = asyncio.Event()
        loop = asyncio.get_running_loop()

        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, stop_event.set)
            except NotImplementedError:
                # Windows signal compatibility
                pass

        try:
            await stop_event.wait()
        except (KeyboardInterrupt, asyncio.CancelledError):
            pass
        finally:
            console.print("\n[yellow]Stopping runtime-mcp tap proxy...[/yellow]")
            await http_tap.stop()
            console.print("[green]Proxy stopped cleanly.[/green]")

    try:
        asyncio.run(run_server())
    except KeyboardInterrupt:
        console.print("\n[green]Shutdown complete.[/green]")


@app.command()
def snapshot(
    db: str = typer.Option(
        "",
        "--db",
        "-d",
        help="Path to local SQLite database file.",
    ),
    log: str = typer.Option(
        "",
        "--log",
        help="Path to application log file.",
    ),
    state_file: str = typer.Option(
        ".runtime_state.json",
        "--state-file",
        help="Path to state file.",
    ),
) -> None:
    """Output instantaneous snapshot of current runtime state (DB + HTTP + Logs) in terminal."""
    store = StateStore(state_file=state_file)
    if db:
        store.set_db_path(str(Path(db).resolve()))
    if log:
        store.set_log_path(str(Path(log).resolve()))

    set_server_state_store(store)
    snapshot_text = get_runtime_snapshot()
    console.print(Markdown(snapshot_text))


@app.command()
def serve(
    state_file: str = typer.Option(
        ".runtime_state.json",
        "--state-file",
        help="Path to state file.",
    ),
) -> None:
    """Launch the FastMCP server over standard I/O (stdio) for AI agent integration."""
    store = StateStore(state_file=state_file)
    set_server_state_store(store)
    # FastMCP stdio transport
    mcp.run(transport="stdio")


if __name__ == "__main__":
    app()
