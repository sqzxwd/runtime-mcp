"""Log tailing and automatic stack trace extractor for Python, Node.js, and log levels."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any


class LogTailTap:
    """Reads recent log entries and extracts Python/Node.js stack traces and error lines."""

    def __init__(self, log_path: str | Path) -> None:
        self.raw_path = str(log_path)
        self.log_path = Path(log_path).resolve()

    def read_tail_lines(self, max_bytes: int = 131072, max_lines: int = 500) -> list[str]:
        """Read the last chunk of the log file without loading the entire file into memory."""
        if not self.log_path.exists():
            return []

        try:
            file_size = self.log_path.stat().st_size
            with open(self.log_path, "rb") as f:
                if file_size > max_bytes:
                    f.seek(file_size - max_bytes)
                content_bytes = f.read()

            text = content_bytes.decode("utf-8", errors="replace")
            lines = text.splitlines()
            return lines[-max_lines:]
        except Exception:
            return []

    def extract_traces(self, max_lines: int = 500) -> list[dict[str, Any]]:
        """Extract Python tracebacks, Node stack traces, and standalone ERROR/CRITICAL lines."""
        lines = self.read_tail_lines(max_lines=max_lines)
        if not lines:
            return []

        traces: list[dict[str, Any]] = []
        i = 0
        n = len(lines)

        while i < n:
            line = lines[i]

            # 1. Python Traceback
            if "Traceback (most recent call last):" in line:
                tb_lines = [line]
                i += 1
                while i < n and (lines[i].startswith(" ") or lines[i].startswith("\t")):
                    tb_lines.append(lines[i])
                    i += 1
                # Final line: exception class and message (e.g. ValueError: ...)
                if i < n and lines[i].strip():
                    tb_lines.append(lines[i])
                    i += 1

                title = tb_lines[-1].strip() if len(tb_lines) > 1 else "Python Traceback"
                traces.append({
                    "type": "python_traceback",
                    "title": title,
                    "content": "\n".join(tb_lines),
                    "line_count": len(tb_lines),
                })
                continue

            # 2. Node.js / JavaScript Stack Trace
            node_err_match = re.search(r"^(?:[A-Za-z0-9_]*Error|Error):", line.strip())
            if node_err_match:
                node_lines = [line]
                i += 1
                while i < n and (
                    lines[i].strip().startswith("at ")
                    or lines[i].strip().startswith("Require stack:")
                    or lines[i].strip().startswith("- ")
                ):
                    node_lines.append(lines[i])
                    i += 1

                title = node_lines[0].strip()
                traces.append({
                    "type": "node_traceback",
                    "title": title,
                    "content": "\n".join(node_lines),
                    "line_count": len(node_lines),
                })
                continue

            # 3. Log ERROR / CRITICAL / FATAL lines
            if re.search(r"\b(ERROR|CRITICAL|FATAL)\b", line, re.IGNORECASE):
                traces.append({
                    "type": "log_error",
                    "title": line.strip(),
                    "content": line.strip(),
                    "line_count": 1,
                })
                i += 1
                continue

            i += 1

        # Return newest errors first
        traces.reverse()
        return traces

    def to_markdown(self, max_items: int = 5) -> str:
        """Format extracted errors and stack traces into concise Markdown."""
        if not self.log_path.exists():
            return f"⚠️ Log file not found at: `{self.raw_path}`"

        traces = self.extract_traces()
        if not traces:
            return f"✅ No errors or stack traces detected in `{self.log_path.name}`."

        items = traces[:max_items]
        md_lines = [
            f"### Recent Application Errors & Stack Traces (`{self.log_path.name}`)",
            f"*Extracted {len(traces)} error events (showing latest {len(items)})*",
            "",
        ]

        for item in items:
            t_type = item["type"]
            title = item["title"]
            content = item["content"]

            if t_type == "python_traceback":
                md_lines.append(f"**[Python Traceback]** `{title}`")
                md_lines.append(f"```python\n{content}\n```\n")
            elif t_type == "node_traceback":
                md_lines.append(f"**[Node.js Traceback]** `{title}`")
                md_lines.append(f"```javascript\n{content}\n```\n")
            else:
                md_lines.append(f"**[Log Error]** `{title}`\n")

        return "\n".join(md_lines).strip()
