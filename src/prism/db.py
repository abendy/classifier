"""Connection helpers for SQLite (with sqlite-vec) and DuckDB attach."""

from __future__ import annotations

import sqlite3
from typing import TYPE_CHECKING

import duckdb
import sqlite_vec

if TYPE_CHECKING:
    from pathlib import Path


def connect_sqlite(path: Path, *, load_vec: bool) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    # check_same_thread=False lets the serve loop hand the connection
    # to asyncio.to_thread. Single-task loop + single worker-thread per
    # iteration means no concurrent access, so the default thread
    # affinity guard adds no safety — only a ProgrammingError.
    conn = sqlite3.connect(path, check_same_thread=False)
    mode = conn.execute("PRAGMA journal_mode = WAL").fetchone()[0]
    if mode != "wal":
        conn.close()
        raise RuntimeError(f"expected WAL journal mode, got {mode!r}")
    if load_vec:
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        conn.enable_load_extension(False)
    return conn


def attach_duckdb(sqlite_path: Path) -> duckdb.DuckDBPyConnection:
    conn = duckdb.connect()
    conn.execute("INSTALL sqlite")
    conn.execute("LOAD sqlite")
    escaped = str(sqlite_path).replace("'", "''")
    conn.execute(f"ATTACH '{escaped}' AS sqlite_db (TYPE sqlite, READ_ONLY)")
    return conn


def migration_head(sqlite_path: Path) -> str | None:
    conn = sqlite3.connect(sqlite_path)
    try:
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='alembic_version'"
        ).fetchone()
        if row is None:
            return None
        head = conn.execute("SELECT version_num FROM alembic_version").fetchone()
        return head[0] if head else None
    finally:
        conn.close()
