# 🔌 runtime-mcp: Live Runtime MCP Bridge for AI Agents

[![Python 3.14+](https://img.shields.io/badge/python-3.14+-3776AB.svg?style=flat&logo=python&logoColor=white)](https://www.python.org/)
[![FastMCP](https://img.shields.io/badge/MCP-FastMCP%202.x-8A2BE2.svg?style=flat)](https://modelcontextprotocol.io/)
[![Runtime Tap](https://img.shields.io/badge/Tap-Live%20Telemetry-00C7B7.svg?style=flat)](https://github.com/)
[![Cross-Platform](https://img.shields.io/badge/platform-macOS%20%7C%20Linux%20%7C%20Windows-lightgrey.svg?style=flat)](https://github.com/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

> **Eliminate environment and runtime hallucinations.**  
> `runtime-mcp` bridges the gap between static code analysis and live application runtime. It transparently inspects live SQLite schemas, intercepts live HTTP errors (4xx/5xx) through an async local reverse proxy, extracts log stack traces, and serves an instant, compact state snapshot to **Antigravity**, **Claude Code**, and **Cursor** via the Model Context Protocol (MCP).

---

## 🛑 The Problem

Modern AI coding agents (Claude, Cursor, Antigravity) are exceptional at reading static code, but they are completely blind to your application's actual runtime state:

1. **Database Hallucinations**: Because migrations are often complex or out-of-sync, agents frequently invent non-existent database columns, missing types, or incorrect foreign keys.
2. **Copy-Paste Friction**: When an API returns a `500 Internal Server Error`, you have to manually copy payloads, headers, and terminal stack traces back into chat.
3. **Stale Context**: Agents suggest code changes based on assumptions rather than verified ground-truth runtime state.

---

## ⚡ The Solution

`runtime-mcp` acts as a zero-overhead live telemetry sidecar for your local development stack:

```
┌──────────────────────┐         ┌──────────────────────────────┐
│  Browser / Frontend  │ ──────> │  runtime-mcp tap (:8090)     │ ─────> ┌────────────────────┐
└──────────────────────┘         │  (Captures 4xx/5xx failures) │        │ Local App (:3000)  │
                                 └──────────────┬───────────────┘        └────────────────────┘
                                                │
                                    .runtime_state.json
                                                │
┌──────────────────────┐         ┌──────────────┴───────────────┐
│ AI Assistant         │ <====== │   runtime-mcp serve (MCP)    │
│ (Antigravity/Claude) │  stdio  │  - get_live_db_schema()      │ <───── SQLite DB (?mode=ro)
└──────────────────────┘         │  - get_recent_http_errors()  │
                                 │  - get_runtime_snapshot()    │ <───── App Logs (*.log)
                                 └──────────────────────────────┘
```

- **DatabaseSchemaTap**: Connects to your local SQLite database in read-only URI mode (`?mode=ro`). Extracts real tables, column types, primary/foreign keys, indexes, and row counts into an ultra-compact, hallucination-resistant Markdown schema.
- **HttpTap**: An asynchronous local reverse proxy (`asyncio` + `httpx`) listening on `:8090` (or custom port) and forwarding to your backend (`:3000`, `:8000`, etc.). Automatically captures requests and deep response bodies for `4xx`/`5xx` failures into a thread-safe ring buffer.
- **LogTailTap**: Automatically tails your application log file and parses Python (`Traceback (most recent call last)...`) and Node.js (`Error: ... at ...`) stack traces, as well as `ERROR` and `CRITICAL` log events.
- **Zero-Latency State Store**: Synchronizes runtime events between the CLI proxy process and the MCP server process using an atomic, cross-platform file buffer (`.runtime_state.json`).

---

## 🚀 Quickstart

### 1. Installation

Requires Python 3.14+:

```bash
# Clone the repository
git clone https://github.com/your-username/runtime-mcp.git
cd runtime-mcp

# Create and activate virtual environment
python3 -m venv .venv

# macOS / Linux
source .venv/bin/activate

# Windows (PowerShell)
# .venv\Scripts\Activate.ps1

# Install in editable mode
pip install -e .
```

### 2. Run the Live Traffic Tap

Start the interceptor proxy, binding your backend port, database, and log file:

```bash
# macOS / Linux / Windows
python -m src.cli tap --listen 8090 --target 3000 --db ./app.db --log ./app.log
```

Now, point your frontend or API client to `http://localhost:8090` instead of `3000`. All requests pass through transparently with minimal latency, and all errors are captured in real-time.

### 3. Check the Runtime Snapshot in Terminal

At any time, inspect the current runtime snapshot directly in your terminal:

```bash
python -m src.cli snapshot --db ./app.db --log ./app.log
```

### 4. Run the MCP Server

Launch the FastMCP server over standard I/O:

```bash
python -m src.cli serve
```

---

## 🤖 AI Agent Integration (MCP Configuration)

Add `runtime-mcp` to your AI assistant's MCP configuration.

### For Google Antigravity & Claude Code & Cursor

Add this block to your `mcpServers` configuration file:

- **Antigravity**: `~/.gemini/antigravity/mcp_config.json` or project settings
- **Claude Code / Desktop**: `claude_desktop_config.json`
- **Cursor**: `.cursor/mcp.json`

```json
{
  "mcpServers": {
    "runtime-mcp": {
      "command": "python",
      "args": [
        "-m",
        "src.cli",
        "serve",
        "--state-file",
        "/absolute/path/to/project/.runtime_state.json"
      ],
      "env": {
        "PYTHONPATH": "/absolute/path/to/runtime-mcp"
      }
    }
  }
}
```

> **Tip for Windows users**: Use forward slashes or escaped backslashes for paths (e.g. `C:/Users/name/projects/runtime-mcp/.runtime_state.json`).

---

## 🛠️ MCP Tools Reference

Once connected, your AI assistant gains access to three live tools:

| Tool | Parameters | Description |
| :--- | :--- | :--- |
| `get_live_db_schema` | `db_path: str = ""` | Returns verified SQLite table definitions, column types, keys, and row counts in compact Markdown. |
| `get_recent_http_errors` | `limit: int = 10` | Returns recent HTTP 4xx/5xx failures, response payloads, request bodies, and latency. |
| `get_runtime_snapshot` | *none* | Generates a single, consolidated report combining environment telemetry, live DB schema, network failures, and log stack traces. |

---

## 💻 CLI Commands Reference

All commands can be invoked either via `runtime-mcp <command>` or `python -m src.cli <command>`:

### `tap`
Starts the live HTTP proxy tap and monitors DB / logs:
```bash
runtime-mcp tap [OPTIONS]

Options:
  -l, --listen INTEGER  Port to listen on [default: 8090]
  -t, --target TEXT     Target backend port or URL (e.g. 3000 or http://localhost:3000) [default: 3000]
  -d, --db TEXT         Path to local SQLite database file
  --log TEXT            Path to application log file
  --state-file TEXT     Path to state file for persistence [default: .runtime_state.json]
```

### `snapshot`
Prints the current verified runtime state in terminal with Rich formatting:
```bash
runtime-mcp snapshot [OPTIONS]

Options:
  -d, --db TEXT         Path to local SQLite database file
  --log TEXT            Path to application log file
  --state-file TEXT     Path to state file [default: .runtime_state.json]
```

### `serve`
Runs the FastMCP server over stdio for IDE and AI agent connections:
```bash
runtime-mcp serve [OPTIONS]

Options:
  --state-file TEXT     Path to state file [default: .runtime_state.json]
```

---

## 🧪 Running Tests

The test suite covers SQLite schema extraction, read-only enforcement, async HTTP proxying, status 500 interception, log stack trace parsing, and direct MCP tool execution:

```bash
pytest -v
```

---

## 📄 License

This project is open-source software licensed under the [MIT License](LICENSE).
