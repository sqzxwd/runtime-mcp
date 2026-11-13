"""FastMCP server providing live runtime introspection tools for AI agents."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    from mcp.server.fastmcp import FastMCP
except (ImportError, ModuleNotFoundError):
    from mcp.server.mcpserver import MCPServer as FastMCP

from src.storage.state_store import StateStore
from src.taps.db_tap import DatabaseSchemaTap
from src.taps.log_tap import LogTailTap

# Initialize the FastMCP server instance
mcp = FastMCP("runtime-mcp")

# Default global state store instance
_state_store = StateStore()


def set_server_state_store(store: StateStore) -> None:
    """Override the state store instance (useful for testing or custom paths)."""
    global _state_store
    _state_store = store


def get_server_state_store() -> StateStore:
    """Get the active state store instance."""
    return _state_store


def find_candidate_db(hint_path: str = "") -> str:
    """Resolve database path from hint, state store, or local directory scan."""
    if hint_path and hint_path.strip():
        return hint_path.strip()

    store_db = _state_store.get_db_path()
    if store_db and store_db.strip():
        return store_db.strip()

    # Search local working directory for sqlite files
    cwd = Path.cwd()
    for ext in ("*.db", "*.sqlite", "*.sqlite3"):
        for f in cwd.glob(ext):
            if f.is_file() and not f.name.startswith("."):
                return str(f)

    return ""


def find_candidate_log(hint_path: str = "") -> str:
    """Resolve log file path from hint, state store, or local directory scan."""
    if hint_path and hint_path.strip():
        return hint_path.strip()

    store_log = _state_store.get_log_path()
    if store_log and store_log.strip():
        return store_log.strip()

    cwd = Path.cwd()
    for ext in ("*.log",):
        for f in cwd.glob(ext):
            if f.is_file() and not f.name.startswith("."):
                return str(f)

    return ""


@mcp.tool()
def get_live_db_schema(db_path: str = "") -> str:
    """Inspect and return the actual live SQLite database schema in ultra-compact Markdown.

    Eliminates database hallucinations by returning exact table names, columns, data types,
    primary keys, foreign key relationships, indexes, and row counts in read-only mode.
    """
    resolved_db = find_candidate_db(db_path)
    if not resolved_db:
        return (
            "⚠️ No SQLite database specified or detected in the current project.\n"
            "Specify `db_path` or run `runtime-mcp tap --db ./path/to/app.db`."
        )

    tap = DatabaseSchemaTap(resolved_db)
    return tap.to_markdown()


@mcp.tool()
def get_recent_http_errors(limit: int = 10) -> str:
    """Return recent HTTP 4xx/5xx errors intercepted by the runtime-mcp reverse proxy.

    Includes HTTP status code, request URL, payload structure, duration, and error response body.
    """
    errors = _state_store.get_recent_errors(limit=limit)
    if not errors:
        proxy_info = _state_store.get_proxy_info()
        active = proxy_info.get("active", False)
        listen_port = proxy_info.get("listen_port")
        target_url = proxy_info.get("target_url")
        status_note = (
            f"Proxy active on port {listen_port} -> {target_url}."
            if active
            else "Proxy is currently offline."
        )
        return f"✅ No HTTP errors (4xx/5xx) intercepted recently. ({status_note})"

    lines = [
        f"### Recent Intercepted HTTP Errors (Showing {len(errors)})",
        "",
    ]

    for err in errors:
        method = err.get("method", "UNKNOWN")
        url = err.get("url", "/")
        status = err.get("status_code", 0)
        dur = err.get("duration_ms", 0)
        time_str = err.get("time_iso", "")
        req_body = err.get("request_body")
        resp_body = err.get("response_body")
        detail = err.get("error_detail")

        lines.append(f"- **{method} {url}** -> `HTTP {status}` ({dur}ms at {time_str})")
        if detail:
            lines.append(f"  - **Proxy Alert**: `{detail}`")
        if req_body is not None:
            if isinstance(req_body, (dict, list)):
                req_json = json.dumps(req_body, indent=2, ensure_ascii=False)
                lines.append(f"  - **Request Body**:\n    ```json\n    {req_json}\n    ```")
            else:
                lines.append(f"  - **Request Body**: `{req_body}`")

        if resp_body is not None:
            if isinstance(resp_body, (dict, list)):
                resp_json = json.dumps(resp_body, indent=2, ensure_ascii=False)
                lines.append(f"  - **Response Error**:\n    ```json\n    {resp_json}\n    ```")
            else:
                lines.append(f"  - **Response Error**: `{resp_body}`")

    return "\n".join(lines).strip()


@mcp.tool()
def get_runtime_snapshot() -> str:
    """Generate a unified, compact snapshot of the entire runtime state for the AI agent.

    Combines:
    1. Active proxy telemetry and monitored resources
    2. Verified database schema
    3. Recent HTTP failures and payloads
    4. Recent log stack traces and critical errors
    """
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    proxy_info = _state_store.get_proxy_info()
    db_path = find_candidate_db()
    log_path = find_candidate_log()

    header_lines = [
        f"# Live Runtime State Snapshot ({now_str})",
        "",
        "## Active Runtime Environment",
        f"- **Proxy Status**: {'ACTIVE' if proxy_info.get('active') else 'INACTIVE'}",
        f"- **Proxy Route**: `http://127.0.0.1:{proxy_info.get('listen_port')}` -> `{proxy_info.get('target_url')}`",
        f"- **Database Path**: `{db_path or 'Not configured'}`",
        f"- **Log File Path**: `{log_path or 'Not configured'}`",
        "",
    ]

    # Section 1: HTTP Errors
    errors_md = get_recent_http_errors(limit=5)

    # Section 2: Database Schema
    db_md = get_live_db_schema(db_path) if db_path else "ℹ️ No database configured."

    # Section 3: Log Stack Traces
    if log_path:
        log_tap = LogTailTap(log_path)
        log_md = log_tap.to_markdown(max_items=3)
    else:
        log_md = "ℹ️ No application log file configured."

    snapshot_parts = [
        "\n".join(header_lines),
        "---",
        "## Intercepted HTTP Traffic & Failures",
        errors_md,
        "",
        "---",
        "## Verified Database Schema",
        db_md,
        "",
        "---",
        "## Log Errors & Stack Traces",
        log_md,
    ]

    return "\n".join(snapshot_parts).strip()


def run_stdio() -> None:
    """Run FastMCP server via standard I/O."""
    mcp.run(transport="stdio")


if __name__ == "__main__":
    run_stdio()
