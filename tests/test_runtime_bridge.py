"""Comprehensive tests for runtime-mcp: SQLite schema tap, HTTP proxy error interception,
log stack trace parsing, and FastMCP tools.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path
from typing import Any

import httpx
import pytest

from src.server import (
    get_live_db_schema,
    get_recent_http_errors,
    get_runtime_snapshot,
    mcp,
    set_server_state_store,
)
from src.storage.state_store import StateStore
from src.taps.db_tap import DatabaseSchemaTap
from src.taps.http_tap import HttpTap
from src.taps.log_tap import LogTailTap


# ==============================================================================
# 1. SQLite Database Schema Tap Tests
# ==============================================================================


def test_sqlite_schema_extraction_and_markdown(tmp_path: Path) -> None:
    """Verify extraction of tables, columns, types, keys, indexes, and markdown rendering."""
    db_file = tmp_path / "test_app.db"

    # Create test SQLite database
    conn = sqlite3.connect(db_file)
    cursor = conn.cursor()

    cursor.execute("""
        CREATE TABLE users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT NOT NULL,
            username TEXT,
            is_active INTEGER DEFAULT 1,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
    """)
    cursor.execute("CREATE UNIQUE INDEX idx_users_email ON users(email);")

    cursor.execute("""
        CREATE TABLE orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            amount REAL NOT NULL,
            status TEXT DEFAULT 'pending',
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        );
    """)
    cursor.execute("CREATE INDEX idx_orders_user ON orders(user_id);")

    # Insert sample rows
    cursor.execute(
        "INSERT INTO users (email, username) VALUES ('alice@example.com', 'alice'), ('bob@example.com', 'bob');"
    )
    cursor.execute("INSERT INTO orders (user_id, amount) VALUES (1, 149.99), (1, 29.50), (2, 85.00);")
    conn.commit()
    conn.close()

    # Introspect with DatabaseSchemaTap
    tap = DatabaseSchemaTap(db_file)
    schema = tap.extract_schema()

    assert schema["tables_count"] == 2
    assert "users" in schema["tables"]
    assert "orders" in schema["tables"]

    # Verify 'users' table
    users_meta = schema["tables"]["users"]
    assert users_meta["row_count"] == 2
    assert "id" in users_meta["primary_keys"]
    col_dict = {c["name"]: c for c in users_meta["columns"]}
    assert col_dict["id"]["type"] == "INTEGER"
    assert col_dict["id"]["pk"] == 1
    assert col_dict["email"]["type"] == "TEXT"
    assert col_dict["email"]["not_null"] is True

    # Verify unique index
    user_idx = {idx["name"]: idx for idx in users_meta["indexes"]}
    assert "idx_users_email" in user_idx
    assert user_idx["idx_users_email"]["unique"] is True
    assert user_idx["idx_users_email"]["columns"] == ["email"]

    # Verify 'orders' foreign keys
    orders_meta = schema["tables"]["orders"]
    assert orders_meta["row_count"] == 3
    assert len(orders_meta["foreign_keys"]) == 1
    fk = orders_meta["foreign_keys"][0]
    assert fk["from"] == "user_id"
    assert fk["to_table"] == "users"
    assert fk["to_column"] == "id"

    # Verify Markdown generation
    md = tap.to_markdown()
    assert "## Live Database Schema (`test_app.db`)" in md
    assert "### Table `users` (~2 rows)" in md
    assert "- `id`: INTEGER [PK]" in md
    assert "- `email`: TEXT [NOT NULL]" in md
    assert "🔗 FK `user_id` -> `users(id)`" in md
    assert "⚡ Index `idx_users_email` (UNIQUE email)" in md

    # Verify read-only enforcement
    ro_conn = tap._get_connection()
    try:
        with pytest.raises(sqlite3.OperationalError, match="readonly database"):
            ro_conn.execute("INSERT INTO users (email) VALUES ('charlie@example.com');")
    finally:
        ro_conn.close()


# ==============================================================================
# 2. HTTP Proxy Tap & Status 500 Interception Tests
# ==============================================================================


@pytest.mark.anyio
async def test_http_tap_proxy_and_error_interception(tmp_path: Path) -> None:
    """Verify HTTP proxy forwarding, response status 500 interception, and state_store recording."""
    state_file = tmp_path / ".runtime_state.json"
    store = StateStore(state_file=state_file)

    # 1. Start mock backend returning 200 for /api/health and 500 for /api/crash
    async def mock_backend_handler(
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        try:
            req_header = await reader.readuntil(b"\r\n\r\n")
            lines = req_header.decode("iso-8859-1").split("\r\n")
            req_line = lines[0]
            method, path, _ = req_line.split(" ", 2)

            headers: dict[str, str] = {}
            for line in lines[1:]:
                if ":" in line:
                    k, v = line.split(":", 1)
                    headers[k.strip().lower()] = v.strip()

            content_length = int(headers.get("content-length", 0))
            body = b""
            if content_length > 0:
                body = await reader.readexactly(content_length)

            if "/api/crash" in path:
                resp_payload = json.dumps({
                    "error": "Internal database crash during transaction",
                    "code": 5001,
                    "received_body": json.loads(body.decode("utf-8")) if body else None,
                }).encode("utf-8")
                writer.write(
                    b"HTTP/1.1 500 Internal Server Error\r\n"
                    b"Content-Type: application/json\r\n"
                    b"Content-Length: " + str(len(resp_payload)).encode() + b"\r\n"
                    b"Connection: close\r\n\r\n" + resp_payload
                )
            else:
                resp_payload = b'{"status": "healthy", "service": "orders-v1"}'
                writer.write(
                    b"HTTP/1.1 200 OK\r\n"
                    b"Content-Type: application/json\r\n"
                    b"Content-Length: " + str(len(resp_payload)).encode() + b"\r\n"
                    b"Connection: close\r\n\r\n" + resp_payload
                )
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    backend_server = await asyncio.start_server(mock_backend_handler, "127.0.0.1", 0)
    backend_port = backend_server.sockets[0].getsockname()[1]
    backend_url = f"http://127.0.0.1:{backend_port}"

    # 2. Start HttpTap proxy listening on random port (listen_port=0)
    intercepted_events: list[dict[str, Any]] = []
    http_tap = HttpTap(
        listen_host="127.0.0.1",
        listen_port=0,
        target_url=backend_url,
        state_store=store,
        on_request=lambda req: intercepted_events.append(req),
    )
    await http_tap.start()
    proxy_port = http_tap.get_actual_port()

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            # Request 1: Normal 200 OK
            res_200 = await client.get(f"http://127.0.0.1:{proxy_port}/api/health")
            assert res_200.status_code == 200
            assert res_200.json() == {"status": "healthy", "service": "orders-v1"}

            # Request 2: Error 500 Internal Server Error
            crash_request_body = {"action": "delete_all", "confirm": True}
            res_500 = await client.post(
                f"http://127.0.0.1:{proxy_port}/api/crash",
                json=crash_request_body,
            )
            assert res_500.status_code == 500
            error_data = res_500.json()
            assert error_data["error"] == "Internal database crash during transaction"
            assert error_data["received_body"] == crash_request_body

        # 3. Verify in-memory and persistent state store
        all_requests = store.get_http_requests()
        assert len(all_requests) == 2

        recent_errors = store.get_recent_errors()
        assert len(recent_errors) == 1

        error_record = recent_errors[0]
        assert error_record["status_code"] == 500
        assert error_record["is_error"] is True
        assert error_record["method"] == "POST"
        assert error_record["url"] == "/api/crash"
        assert error_record["request_body"] == crash_request_body
        assert error_record["response_body"]["error"] == "Internal database crash during transaction"
        assert error_record["duration_ms"] >= 0

        # Verify on_request callback caught both
        assert len(intercepted_events) == 2

        # Verify file persistence across process/instance boundaries
        new_store_instance = StateStore(state_file=state_file)
        persisted_errors = new_store_instance.get_recent_errors()
        assert len(persisted_errors) == 1
        assert persisted_errors[0]["url"] == "/api/crash"

    finally:
        await http_tap.stop()
        backend_server.close()
        await backend_server.wait_closed()


# ==============================================================================
# 3. Log Stack Trace Parsing Tests
# ==============================================================================


def test_log_tail_tap_parsing(tmp_path: Path) -> None:
    """Verify parsing and extraction of Python tracebacks, Node.js errors, and ERROR log lines."""
    log_file = tmp_path / "application.log"

    log_content = """2026-11-13 14:00:00 [INFO] Application booted successfully on port 8000
2026-11-13 14:00:05 [INFO] Received GET /users/42
Traceback (most recent call last):
  File "/app/routes/user.py", line 58, in get_user
    user = db.find_user(user_id)
  File "/app/database.py", line 124, in find_user
    raise KeyError("User with ID 42 does not exist")
KeyError: 'User with ID 42 does not exist'
2026-11-13 14:01:00 [INFO] Healthcheck passed
2026-11-13 14:02:10 [ERROR] cache_worker: Redis connection timed out after 5000ms
2026-11-13 14:03:00 [INFO] Node auth service spawned
TypeError: Cannot read properties of undefined (reading 'jwt_token')
    at verifyAuthHeader (/app/src/auth.js:45:19)
    at router (/app/src/server.js:12:5)
2026-11-13 14:04:00 [INFO] Worker idle
"""
    log_file.write_text(log_content, encoding="utf-8")

    tap = LogTailTap(log_file)
    traces = tap.extract_traces()

    # Should detect all 3 errors (Node traceback, log ERROR line, Python traceback)
    assert len(traces) == 3

    types = [t["type"] for t in traces]
    assert "python_traceback" in types
    assert "node_traceback" in types
    assert "log_error" in types

    py_trace = next(t for t in traces if t["type"] == "python_traceback")
    assert "KeyError: 'User with ID 42 does not exist'" in py_trace["title"]
    assert "Traceback (most recent call last):" in py_trace["content"]

    node_trace = next(t for t in traces if t["type"] == "node_traceback")
    assert "TypeError: Cannot read properties of undefined (reading 'jwt_token')" in node_trace["title"]
    assert "at verifyAuthHeader" in node_trace["content"]

    log_err = next(t for t in traces if t["type"] == "log_error")
    assert "Redis connection timed out after 5000ms" in log_err["title"]

    # Check Markdown formatting
    md = tap.to_markdown()
    assert "### Recent Application Errors & Stack Traces" in md
    assert "**[Python Traceback]** `KeyError: 'User with ID 42 does not exist'`" in md
    assert "```python" in md
    assert "**[Node.js Traceback]** `TypeError: Cannot read properties of undefined" in md
    assert "```javascript" in md
    assert "**[Log Error]** `2026-11-13 14:02:10 [ERROR] cache_worker: Redis connection timed out after 5000ms`" in md


# ==============================================================================
# 4. FastMCP Tools Direct Invocation Tests
# ==============================================================================


@pytest.mark.anyio
async def test_mcp_tools_direct_invocation(tmp_path: Path) -> None:
    """Verify direct invocation of FastMCP tools: get_live_db_schema, get_recent_http_errors, get_runtime_snapshot."""
    # Setup test DB
    db_file = tmp_path / "mcp_app.db"
    conn = sqlite3.connect(db_file)
    conn.execute("CREATE TABLE products (id INTEGER PRIMARY KEY, name TEXT NOT NULL, price REAL);")
    conn.execute("INSERT INTO products (name, price) VALUES ('Widget Pro', 29.99);")
    conn.commit()
    conn.close()

    # Setup test Log
    log_file = tmp_path / "mcp_app.log"
    log_file.write_text(
        "2026-11-13 15:00:00 [ERROR] payment_gateway: Stripe API secret key invalid\n",
        encoding="utf-8",
    )

    # Setup test StateStore
    state_file = tmp_path / ".runtime_state.json"
    store = StateStore(state_file=state_file)
    store.set_db_path(str(db_file))
    store.set_log_path(str(log_file))
    store.set_proxy_info(listen_port=8090, target_url="http://127.0.0.1:3000", active=True)

    # Add a mock 500 error
    store.add_http_request({
        "timestamp": 1700000000.0,
        "time_iso": "15:00:01.123",
        "method": "POST",
        "url": "/api/checkout",
        "status_code": 500,
        "duration_ms": 42.5,
        "is_error": True,
        "request_body": {"cart_id": "c_999"},
        "response_body": {"error": "Payment service unavailable"},
    })

    # Point server state store to test store
    set_server_state_store(store)

    # 1. Test get_live_db_schema
    db_schema_output = get_live_db_schema(str(db_file))
    assert "## Live Database Schema (`mcp_app.db`)" in db_schema_output
    assert "### Table `products` (~1 rows)" in db_schema_output
    assert "- `name`: TEXT [NOT NULL]" in db_schema_output

    # 2. Test get_recent_http_errors
    http_errors_output = get_recent_http_errors(limit=5)
    assert "### Recent Intercepted HTTP Errors" in http_errors_output
    assert "POST /api/checkout" in http_errors_output
    assert "HTTP 500" in http_errors_output
    assert "Payment service unavailable" in http_errors_output

    # 3. Test get_runtime_snapshot
    snapshot_output = get_runtime_snapshot()
    assert "# Live Runtime State Snapshot" in snapshot_output
    assert "## Active Runtime Environment" in snapshot_output
    assert "Proxy Status**: ACTIVE" in snapshot_output
    assert "http://127.0.0.1:8090" in snapshot_output
    assert "## Intercepted HTTP Traffic & Failures" in snapshot_output
    assert "## Verified Database Schema" in snapshot_output
    assert "Table `products`" in snapshot_output
    assert "## Log Errors & Stack Traces" in snapshot_output
    assert "Stripe API secret key invalid" in snapshot_output

    # 4. Also verify tool execution via FastMCP call_tool interface
    tool_call_res = await mcp.call_tool("get_live_db_schema", {"db_path": str(db_file)})
    assert not tool_call_res.is_error
    assert any("products" in getattr(block, "text", "") for block in tool_call_res.content)
