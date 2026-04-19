"""HTTP API for the classifier service."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from classifier import __version__
from classifier.config import CONFIG_PATH, load_config
from classifier.db import attach_duckdb, connect_sqlite, migration_head

if TYPE_CHECKING:
    from classifier.config import StorageConfig

app = FastAPI(title="classifier", version=__version__)


def _check_health(storage: StorageConfig) -> tuple[dict[str, Any], int]:
    details: dict[str, Any] = {}
    healthy = True

    try:
        conn = connect_sqlite(storage.sqlite_path, load_vec=False)
        try:
            mode = conn.execute("PRAGMA journal_mode").fetchone()
            details["journal_mode"] = mode[0] if mode else "unknown"
        finally:
            conn.close()
    except Exception as exc:
        details["journal_mode"] = f"error: {exc}"
        healthy = False

    if not storage.sqlite_vec:
        details["sqlite_vec"] = "disabled"
    else:
        try:
            conn = connect_sqlite(storage.sqlite_path, load_vec=True)
            try:
                details["sqlite_vec"] = "loaded"
            finally:
                conn.close()
        except Exception as exc:
            details["sqlite_vec"] = f"error: {exc}"
            healthy = False

    if not storage.duckdb_attach:
        details["duckdb_attach"] = "disabled"
    else:
        try:
            duck = attach_duckdb(storage.sqlite_path)
            try:
                details["duckdb_attach"] = "ok"
            finally:
                duck.close()
        except Exception as exc:
            details["duckdb_attach"] = f"error: {exc}"
            healthy = False

    try:
        details["migration_head"] = migration_head(storage.sqlite_path)
    except Exception as exc:
        details["migration_head"] = f"error: {exc}"
        healthy = False

    body: dict[str, Any] = {
        "status": "healthy" if healthy else "unhealthy",
        "message": "all probes ok" if healthy else "one or more probes failed",
        "timestamp": datetime.now(UTC).isoformat(),
        "details": details,
    }
    return body, 200 if healthy else 503


@app.get("/health")
def health() -> JSONResponse:
    try:
        storage = load_config(CONFIG_PATH).storage
    except Exception as exc:
        body: dict[str, Any] = {
            "status": "unhealthy",
            "message": f"config load failed: {exc}",
            "timestamp": datetime.now(UTC).isoformat(),
            "details": {"config": f"error: {exc}"},
        }
        return JSONResponse(content=body, status_code=503)
    body, http_status = _check_health(storage)
    return JSONResponse(content=body, status_code=http_status)
