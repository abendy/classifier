"""Integration tests for the storage helpers."""

from __future__ import annotations

import sqlite3
from typing import TYPE_CHECKING

import pytest

from prism.db import attach_duckdb, connect_sqlite, migration_head

if TYPE_CHECKING:
    from pathlib import Path


def test_connect_sqlite_loads_vec_extension(tmp_path: Path) -> None:
    conn = connect_sqlite(tmp_path / "vec.db", load_vec=True)
    try:
        version = conn.execute("SELECT vec_version()").fetchone()[0]
        assert isinstance(version, str) and version
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        assert mode == "wal"
    finally:
        conn.close()


def test_connect_sqlite_without_vec_lacks_extension(tmp_path: Path) -> None:
    conn = connect_sqlite(tmp_path / "novec.db", load_vec=False)
    try:
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("SELECT vec_version()").fetchone()
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        assert mode == "wal"
    finally:
        conn.close()


def test_connect_sqlite_uses_row_objects(tmp_path: Path) -> None:
    conn = connect_sqlite(tmp_path / "rows.db", load_vec=False)
    try:
        conn.execute("CREATE TABLE x (n INTEGER)")
        conn.execute("INSERT INTO x VALUES (42)")
        row = conn.execute("SELECT n FROM x").fetchone()
        assert row is not None
        assert isinstance(row, sqlite3.Row)
        assert row["n"] == 42
    finally:
        conn.close()


def test_connect_sqlite_creates_missing_parent(tmp_path: Path) -> None:
    nested = tmp_path / "deep" / "nested" / "dir" / "fresh.db"
    assert not nested.parent.exists()
    conn = connect_sqlite(nested, load_vec=False)
    try:
        assert nested.parent.is_dir()
        assert nested.exists()
    finally:
        conn.close()


def test_attach_duckdb_reads_sqlite_table(tmp_path: Path) -> None:
    db_path = tmp_path / "attach.db"
    seed = sqlite3.connect(db_path)
    try:
        seed.execute("CREATE TABLE x (n INTEGER)")
        seed.execute("INSERT INTO x VALUES (42)")
        seed.commit()
    finally:
        seed.close()

    duck = attach_duckdb(db_path)
    try:
        result = duck.execute("SELECT n FROM sqlite_db.x").fetchone()
        assert result is not None
        assert result[0] == 42
    finally:
        duck.close()


def test_migration_head_returns_none_when_table_missing(tmp_path: Path) -> None:
    db_path = tmp_path / "fresh.db"
    sqlite3.connect(db_path).close()
    assert migration_head(db_path) is None
