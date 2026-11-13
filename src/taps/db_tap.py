"""Database schema introspector for SQLite in read-only mode."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any


class DatabaseSchemaTap:
    """Connects to local SQLite databases in read-only URI mode and extracts compact schemas."""

    def __init__(self, db_path: str | Path) -> None:
        self.raw_path = str(db_path)
        self.db_path = Path(db_path).resolve()

    def _get_connection(self) -> sqlite3.Connection:
        """Establish a strictly read-only SQLite connection using URI mode."""
        if not self.db_path.exists():
            raise FileNotFoundError(f"Database file does not exist: {self.db_path}")

        # Construct URI ensuring cross-platform compatibility (macOS/Linux/Windows)
        uri = f"{self.db_path.as_uri()}?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=5.0)
        conn.row_factory = sqlite3.Row
        return conn

    def extract_schema(self) -> dict[str, Any]:
        """Extract tables, columns, constraints, foreign keys, indexes, and row counts."""
        conn = self._get_connection()
        try:
            cursor = conn.cursor()

            # Retrieve user tables (excluding SQLite internal tables)
            cursor.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name;"
            )
            tables = [row["name"] for row in cursor.fetchall()]

            schema_data: dict[str, Any] = {
                "db_path": str(self.db_path),
                "tables_count": len(tables),
                "tables": {},
            }

            for table in tables:
                # 1. Columns & data types
                cursor.execute(f'PRAGMA table_info("{table}");')
                col_rows = cursor.fetchall()
                columns = []
                pks = []
                for col in col_rows:
                    col_info = {
                        "name": col["name"],
                        "type": col["type"] or "ANY",
                        "not_null": bool(col["notnull"]),
                        "default_value": col["dflt_value"],
                        "pk": int(col["pk"]),
                    }
                    if col["pk"] > 0:
                        pks.append((col["pk"], col["name"]))
                    columns.append(col_info)

                pks.sort(key=lambda x: x[0])
                primary_keys = [pk[1] for pk in pks]

                # 2. Foreign keys
                cursor.execute(f'PRAGMA foreign_key_list("{table}");')
                fk_rows = cursor.fetchall()
                foreign_keys = []
                for fk in fk_rows:
                    foreign_keys.append({
                        "from": fk["from"],
                        "to_table": fk["table"],
                        "to_column": fk["to"],
                        "on_update": fk["on_update"],
                        "on_delete": fk["on_delete"],
                    })

                # 3. Indexes
                cursor.execute(f'PRAGMA index_list("{table}");')
                idx_rows = cursor.fetchall()
                indexes = []
                for idx in idx_rows:
                    idx_name = idx["name"]
                    cursor.execute(f'PRAGMA index_info("{idx_name}");')
                    idx_cols = [c["name"] for c in cursor.fetchall()]
                    indexes.append({
                        "name": idx_name,
                        "unique": bool(idx["unique"]),
                        "columns": idx_cols,
                    })

                # 4. Row count
                try:
                    cursor.execute(f'SELECT COUNT(*) AS cnt FROM "{table}";')
                    row_count = cursor.fetchone()["cnt"]
                except Exception:
                    row_count = 0

                schema_data["tables"][table] = {
                    "columns": columns,
                    "primary_keys": primary_keys,
                    "foreign_keys": foreign_keys,
                    "indexes": indexes,
                    "row_count": row_count,
                }

            return schema_data
        finally:
            conn.close()

    def to_markdown(self) -> str:
        """Generate an ultra-compact, hallucination-resistant Markdown schema."""
        try:
            data = self.extract_schema()
        except FileNotFoundError:
            return f"⚠️ Database file not found at: `{self.raw_path}`"
        except Exception as exc:
            return f"⚠️ Failed to inspect database `{self.raw_path}`: {exc}"

        tables = data.get("tables", {})
        if not tables:
            return f"ℹ️ Database `{self.db_path.name}` contains no tables (empty schema)."

        lines: list[str] = [
            f"## Live Database Schema (`{self.db_path.name}`)",
            f"*Total tables: {len(tables)} | Path: `{self.db_path}`*",
            "",
        ]

        for table_name, meta in tables.items():
            row_count = meta.get("row_count", 0)
            lines.append(f"### Table `{table_name}` (~{row_count} rows)")

            # Columns
            for col in meta.get("columns", []):
                flags = []
                if col.get("pk"):
                    flags.append("PK")
                if col.get("not_null"):
                    flags.append("NOT NULL")
                if col.get("default_value") is not None:
                    flags.append(f"DEFAULT {col['default_value']}")

                flag_str = f" [{', '.join(flags)}]" if flags else ""
                lines.append(f"- `{col['name']}`: {col['type']}{flag_str}")

            # Foreign keys
            for fk in meta.get("foreign_keys", []):
                lines.append(f"  - 🔗 FK `{fk['from']}` -> `{fk['to_table']}({fk['to_column']})`")

            # Indexes (excluding SQLite internal ones if already obvious)
            for idx in meta.get("indexes", []):
                u_flag = "UNIQUE " if idx.get("unique") else ""
                cols_str = ", ".join(idx.get("columns", []))
                lines.append(f"  - ⚡ Index `{idx['name']}` ({u_flag}{cols_str})")

            lines.append("")

        return "\n".join(lines).strip()
