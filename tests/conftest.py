"""Shared test fixtures for the classifier test suite."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from alembic import command
from alembic.config import Config

from prism.db import connect_sqlite

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Iterator
    from pathlib import Path


def _migrated_db(
    tmp_path: Path, *, load_vec: bool
) -> Iterator[sqlite3.Connection]:
    db_path = tmp_path / "prism.db"
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    command.upgrade(cfg, "head")

    conn = connect_sqlite(db_path, load_vec=load_vec)
    try:
        yield conn
    finally:
        conn.close()


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    """Migrated SQLite connection without sqlite-vec loaded."""
    yield from _migrated_db(tmp_path, load_vec=False)


@pytest.fixture
def conn_with_vec(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    """Migrated SQLite connection with sqlite-vec loaded."""
    yield from _migrated_db(tmp_path, load_vec=True)
