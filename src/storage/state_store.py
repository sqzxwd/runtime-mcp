"""Thread-safe ring buffer state store with atomic JSON persistence.

Enables seamless zero-latency runtime state exchange between the CLI proxy
and the MCP server across separate operating system processes.
"""

from __future__ import annotations

import json
import os
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any


class StateStore:
    """Thread-safe ring buffer and state coordinator backed by .runtime_state.json."""

    def __init__(
        self,
        state_file: str | Path = ".runtime_state.json",
        max_requests: int = 50,
    ) -> None:
        self.state_file = Path(state_file).resolve()
        self.max_requests = max_requests
        self._lock = threading.Lock()
        self._requests: deque[dict[str, Any]] = deque(maxlen=max_requests)
        self._db_path: str = ""
        self._log_path: str = ""
        self._proxy_info: dict[str, Any] = {
            "listen_port": None,
            "target_url": None,
            "active": False,
        }
        self._last_loaded_mtime: float = 0.0

        # Load existing state if available on disk
        self.load_from_file()

    def set_db_path(self, db_path: str) -> None:
        """Set tracked database path and persist."""
        with self._lock:
            self._db_path = str(db_path) if db_path else ""
            self.save_to_file()

    def get_db_path(self) -> str:
        """Get tracked database path, refreshing from disk if updated."""
        self.load_from_file()
        with self._lock:
            return self._db_path

    def set_log_path(self, log_path: str) -> None:
        """Set tracked log path and persist."""
        with self._lock:
            self._log_path = str(log_path) if log_path else ""
            self.save_to_file()

    def get_log_path(self) -> str:
        """Get tracked log path, refreshing from disk if updated."""
        self.load_from_file()
        with self._lock:
            return self._log_path

    def set_proxy_info(self, listen_port: int, target_url: str, active: bool = True) -> None:
        """Set active proxy configuration and persist."""
        with self._lock:
            self._proxy_info = {
                "listen_port": listen_port,
                "target_url": target_url,
                "active": active,
            }
            self.save_to_file()

    def get_proxy_info(self) -> dict[str, Any]:
        """Get active proxy configuration."""
        self.load_from_file()
        with self._lock:
            return dict(self._proxy_info)

    def add_http_request(self, request_data: dict[str, Any]) -> None:
        """Add an HTTP request record to the ring buffer and atomically persist."""
        with self._lock:
            entry = dict(request_data)
            if "timestamp" not in entry:
                entry["timestamp"] = time.time()
            if "status_code" in entry:
                entry["is_error"] = int(entry["status_code"]) >= 400
            self._requests.append(entry)
            self.save_to_file()

    def get_http_requests(
        self,
        limit: int = 50,
        errors_only: bool = False,
    ) -> list[dict[str, Any]]:
        """Retrieve recent HTTP requests from memory/file, newest first."""
        self.load_from_file()
        with self._lock:
            items = list(self._requests)

        if errors_only:
            items = [r for r in items if r.get("is_error", False) or r.get("status_code", 0) >= 400]

        # Return most recent requests first up to limit
        items.reverse()
        return items[:limit]

    def get_recent_errors(self, limit: int = 10) -> list[dict[str, Any]]:
        """Retrieve the most recent HTTP 4xx/5xx errors."""
        return self.get_http_requests(limit=limit, errors_only=True)

    def clear(self) -> None:
        """Clear all stored in-memory and on-disk requests."""
        with self._lock:
            self._requests.clear()
            self.save_to_file()

    def to_dict(self) -> dict[str, Any]:
        """Convert current state store to dictionary representation."""
        with self._lock:
            return {
                "version": "1.0",
                "updated_at": time.time(),
                "db_path": self._db_path,
                "log_path": self._log_path,
                "proxy": dict(self._proxy_info),
                "requests": list(self._requests),
            }

    def save_to_file(self) -> None:
        """Atomically persist state to disk via temp file replacement."""
        try:
            parent = self.state_file.parent
            parent.mkdir(parents=True, exist_ok=True)

            data = {
                "version": "1.0",
                "updated_at": time.time(),
                "db_path": self._db_path,
                "log_path": self._log_path,
                "proxy": dict(self._proxy_info),
                "requests": list(self._requests),
            }

            temp_path = parent / f"{self.state_file.name}.tmp.{os.getpid()}_{time.time_ns()}"
            with open(temp_path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
                f.flush()
                os.fsync(f.fileno())

            # Atomic replace works cross-platform (POSIX and Windows)
            os.replace(temp_path, self.state_file)
            self._last_loaded_mtime = self.state_file.stat().st_mtime
        except Exception:
            # Avoid crashing the application if disk write temporarily fails
            pass

    def load_from_file(self) -> bool:
        """Reload state from file if modified on disk by another process."""
        if not self.state_file.exists():
            return False

        try:
            mtime = self.state_file.stat().st_mtime
            if mtime <= self._last_loaded_mtime:
                return False

            with open(self.state_file, "r", encoding="utf-8") as f:
                content = f.read().strip()
                if not content:
                    return False
                data = json.loads(content)

            with self._lock:
                self._db_path = data.get("db_path", self._db_path)
                self._log_path = data.get("log_path", self._log_path)
                self._proxy_info = data.get("proxy", self._proxy_info)

                requests = data.get("requests", [])
                self._requests = deque(requests, maxlen=self.max_requests)
                self._last_loaded_mtime = mtime
            return True
        except (json.JSONDecodeError, OSError):
            return False
