"""Asynchronous HTTP reverse-proxy traffic tap using asyncio and httpx."""

from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime, timezone
from typing import Any, Callable
from urllib.parse import urlparse

import httpx

from src.storage.state_store import StateStore


def normalize_target_url(target: str) -> str:
    """Normalize target into a valid HTTP URL."""
    target = target.strip()
    if target.isdigit():
        return f"http://127.0.0.1:{target}"
    if not (target.startswith("http://") or target.startswith("https://")):
        return f"http://{target}"
    return target


class HttpTap:
    """Lightweight reverse proxy tapping into HTTP requests/responses and capturing runtime errors."""

    def __init__(
        self,
        listen_host: str = "127.0.0.1",
        listen_port: int = 8090,
        target_url: str = "3000",
        state_store: StateStore | None = None,
        on_request: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        self.listen_host = listen_host
        self.listen_port = listen_port
        self.target_url = normalize_target_url(target_url)
        self.state_store = state_store or StateStore()
        self.on_request = on_request

        parsed_target = urlparse(self.target_url)
        self.target_host_header = parsed_target.netloc or "127.0.0.1"

        self._server: asyncio.Server | None = None
        self._client: httpx.AsyncClient | None = None
        self._is_running = False

    async def start(self) -> asyncio.Server:
        """Start the async proxy server."""
        if self._is_running:
            assert self._server is not None
            return self._server

        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(30.0, connect=5.0),
            follow_redirects=False,
            verify=False,
        )

        self._server = await asyncio.start_server(
            self._handle_client,
            host=self.listen_host,
            port=self.listen_port,
        )
        self._is_running = True

        actual_port = self.get_actual_port()
        self.state_store.set_proxy_info(
            listen_port=actual_port,
            target_url=self.target_url,
            active=True,
        )
        return self._server

    async def stop(self) -> None:
        """Gracefully stop proxy server and clean up connections."""
        self._is_running = False
        if self._server:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

        if self._client:
            await self._client.aclose()
            self._client = None

        self.state_store.set_proxy_info(
            listen_port=self.listen_port,
            target_url=self.target_url,
            active=False,
        )

    def get_actual_port(self) -> int:
        """Return the port the server is listening on."""
        if self._server and self._server.sockets:
            return self._server.sockets[0].getsockname()[1]
        return self.listen_port

    async def __aenter__(self) -> "HttpTap":
        await self.start()
        return self

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        await self.stop()

    async def _handle_client(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        """Handle incoming client TCP connection and proxy HTTP request."""
        try:
            try:
                header_bytes = await reader.readuntil(b"\r\n\r\n")
            except (asyncio.IncompleteReadError, ConnectionResetError):
                writer.close()
                await writer.wait_closed()
                return

            header_text = header_bytes.decode("iso-8859-1")
            lines = header_text.split("\r\n")
            if not lines or not lines[0].strip():
                writer.close()
                await writer.wait_closed()
                return

            # Parse HTTP Request line
            req_line_parts = lines[0].split()
            if len(req_line_parts) < 2:
                writer.close()
                await writer.wait_closed()
                return

            method = req_line_parts[0].upper()
            path = req_line_parts[1]

            # Parse headers
            req_headers: dict[str, str] = {}
            for line in lines[1:]:
                if ":" in line:
                    key, val = line.split(":", 1)
                    req_headers[key.strip().lower()] = val.strip()

            # Read request body if present
            content_length = int(req_headers.get("content-length", 0))
            req_body_bytes = b""
            if content_length > 0:
                req_body_bytes = await reader.readexactly(content_length)

            # Parse request body representation
            req_content_type = req_headers.get("content-type", "")
            req_body_parsed = self._parse_body(req_body_bytes, req_content_type)

            # Build destination URL and headers
            forward_url = f"{self.target_url.rstrip('/')}{path}"
            forward_headers = {
                k: v
                for k, v in req_headers.items()
                if k not in ("host", "content-length", "connection", "accept-encoding")
            }
            forward_headers["host"] = self.target_host_header

            start_time = time.perf_counter()
            resp_status: int = 502
            resp_reason: str = "Bad Gateway"
            resp_headers_dict: dict[str, str] = {}
            resp_body_bytes = b""
            error_message: str | None = None

            try:
                assert self._client is not None
                response = await self._client.request(
                    method=method,
                    url=forward_url,
                    headers=forward_headers,
                    content=req_body_bytes,
                )
                duration_ms = round((time.perf_counter() - start_time) * 1000, 2)
                resp_status = response.status_code
                resp_reason = response.reason_phrase
                resp_body_bytes = response.content

                for k, v in response.headers.items():
                    if k.lower() not in (
                        "transfer-encoding",
                        "content-encoding",
                        "content-length",
                        "connection",
                    ):
                        resp_headers_dict[k] = v

            except httpx.ConnectError as err:
                duration_ms = round((time.perf_counter() - start_time) * 1000, 2)
                resp_status = 502
                resp_reason = "Bad Gateway"
                error_message = f"Failed to connect to target backend {self.target_url}: {err}"
                resp_body_bytes = json.dumps(
                    {"error": "Backend Target Unreachable", "detail": error_message},
                    indent=2,
                ).encode("utf-8")
                resp_headers_dict["content-type"] = "application/json"
            except Exception as err:
                duration_ms = round((time.perf_counter() - start_time) * 1000, 2)
                resp_status = 500
                resp_reason = "Internal Proxy Error"
                error_message = f"Proxy error: {err}"
                resp_body_bytes = json.dumps(
                    {"error": "Proxy Error", "detail": error_message},
                    indent=2,
                ).encode("utf-8")
                resp_headers_dict["content-type"] = "application/json"

            # Parse response body
            resp_content_type = resp_headers_dict.get("content-type", "")
            resp_body_parsed = self._parse_body(
                resp_body_bytes,
                resp_content_type,
                max_len=4096 if resp_status >= 400 else 512,
            )

            # Store in ring buffer state store
            request_record = {
                "timestamp": time.time(),
                "time_iso": datetime.now(timezone.utc).strftime("%H:%M:%S.%f")[:-3],
                "method": method,
                "url": path,
                "target_url": forward_url,
                "status_code": resp_status,
                "duration_ms": duration_ms,
                "is_error": resp_status >= 400,
                "request_content_type": req_content_type,
                "request_body": req_body_parsed,
                "response_content_type": resp_content_type,
                "response_body": resp_body_parsed,
            }
            if error_message:
                request_record["error_detail"] = error_message

            self.state_store.add_http_request(request_record)

            if self.on_request:
                try:
                    self.on_request(request_record)
                except Exception:
                    pass

            # Write HTTP response back to client
            status_line = f"HTTP/1.1 {resp_status} {resp_reason}\r\n"
            writer.write(status_line.encode("iso-8859-1"))

            for header_name, header_val in resp_headers_dict.items():
                writer.write(f"{header_name}: {header_val}\r\n".encode("iso-8859-1"))

            writer.write(f"Content-Length: {len(resp_body_bytes)}\r\n".encode("iso-8859-1"))
            writer.write(b"Connection: close\r\n\r\n")
            writer.write(resp_body_bytes)
            await writer.drain()

        except Exception:
            pass
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass

    def _parse_body(
        self,
        body_bytes: bytes,
        content_type: str,
        max_len: int = 4096,
    ) -> Any:
        """Parse request/response body into structured JSON or truncated text."""
        if not body_bytes:
            return None

        # Attempt JSON decoding if declared or formatted like JSON
        ct_lower = content_type.lower()
        if "application/json" in ct_lower or body_bytes.lstrip().startswith((b"{", b"[")):
            try:
                return json.loads(body_bytes.decode("utf-8"))
            except Exception:
                pass

        try:
            text = body_bytes.decode("utf-8", errors="replace")
            if len(text) > max_len:
                return text[:max_len] + f"... [truncated {len(text) - max_len} bytes]"
            return text
        except Exception:
            return f"<binary data: {len(body_bytes)} bytes>"
