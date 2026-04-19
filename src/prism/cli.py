"""Operator CLI for the prism service."""

from __future__ import annotations

import json

import typer
import uvicorn
from rich.console import Console
from rich.table import Table

from prism.config import CONFIG_PATH, load_config
from prism.db import attach_duckdb, connect_sqlite, migration_head
from prism.manifest import ServiceManifest, export_schema, load_manifest

app = typer.Typer(no_args_is_help=True)


def _load_manifest_via_config() -> ServiceManifest:
    cfg = load_config(CONFIG_PATH)
    return load_manifest(cfg.service.manifest_path)


@app.command()
def manifest() -> None:
    """Validate and pretty-print the service manifest."""
    model = _load_manifest_via_config()
    typer.echo(model.model_dump_json(indent=2, exclude_none=True, by_alias=True))


@app.command()
def status() -> None:
    """Show a summary of the service manifest."""
    model = _load_manifest_via_config()
    table = Table(show_header=False)
    table.add_column("Field")
    table.add_column("Value", overflow="fold")
    table.add_row("Name", model.identity.name)
    table.add_row("Version", model.identity.version)
    table.add_row("Language", model.identity.language)
    table.add_row("Capabilities", str(len(model.capabilities)))
    table.add_row("Commands", str(len(model.commands)))
    table.add_row("Events published", str(len(model.events.publishes or [])))
    table.add_row("Events subscribed", str(len(model.events.subscribes or [])))
    table.add_row("HTTP port", str(model.api.port))
    Console().print(table)


@app.command()
def schema() -> None:
    """Print the JSON Schema generated from the manifest models."""
    _load_manifest_via_config()
    typer.echo(json.dumps(export_schema(), indent=2, sort_keys=True))


@app.command()
def config() -> None:
    """Validate config.yaml and show a summary."""
    loaded = load_config(CONFIG_PATH)
    table = Table(show_header=False)
    table.add_column("Field")
    table.add_column("Value", overflow="fold")
    table.add_row("HTTP port", str(loaded.service.http_port))
    table.add_row("Manifest path", str(loaded.service.manifest_path))
    table.add_row("Registry URL", loaded.service.registry_url or "(unset)")
    table.add_row("Embedding model", loaded.pipeline.embedding.model)
    table.add_row("Topic model", loaded.pipeline.topic.model)
    table.add_row("Ingest source", loaded.ingest.source)
    table.add_row("SQLite path", str(loaded.storage.sqlite_path))
    table.add_row("Phoenix enabled", str(loaded.audit.phoenix.enabled))
    Console().print(table)


@app.command()
def db() -> None:
    """Probe the storage layer and print a status table."""
    storage = load_config(CONFIG_PATH).storage
    table = Table(show_header=False)
    table.add_column("Field")
    table.add_column("Value", overflow="fold")
    table.add_row("SQLite path", str(storage.sqlite_path.resolve()))

    sqlite_ok = False
    try:
        conn = connect_sqlite(storage.sqlite_path, load_vec=False)
        try:
            mode = conn.execute("PRAGMA journal_mode").fetchone()
            table.add_row("Journal mode", mode[0] if mode else "unknown")
            sqlite_ok = True
        finally:
            conn.close()
    except Exception as exc:
        table.add_row("Journal mode", f"error: {exc}")

    if not sqlite_ok:
        for label in ("sqlite-vec", "DuckDB attach", "Migration head"):
            table.add_row(label, "not probed")
        Console().print(table)
        return

    if not storage.sqlite_vec:
        table.add_row("sqlite-vec", "disabled")
    else:
        try:
            conn = connect_sqlite(storage.sqlite_path, load_vec=True)
            try:
                table.add_row("sqlite-vec", "loaded")
            finally:
                conn.close()
        except Exception as exc:
            table.add_row("sqlite-vec", f"error: {exc}")

    if not storage.duckdb_attach:
        table.add_row("DuckDB attach", "disabled")
    else:
        try:
            duck = attach_duckdb(storage.sqlite_path)
            try:
                table.add_row("DuckDB attach", "ok")
            finally:
                duck.close()
        except Exception as exc:
            table.add_row("DuckDB attach", f"error: {exc}")

    try:
        head = migration_head(storage.sqlite_path)
        table.add_row("Migration head", head or "no migrations run")
    except Exception as exc:
        table.add_row("Migration head", f"error: {exc}")

    Console().print(table)


@app.command()
def serve() -> None:
    """Run the prism HTTP service."""
    cfg = load_config(CONFIG_PATH)
    mf = load_manifest(cfg.service.manifest_path)
    if cfg.service.http_port != mf.api.port:
        typer.echo(
            f"error: port mismatch — config.service.http_port is "
            f"{cfg.service.http_port}, manifest api.port is {mf.api.port}. "
            f"Update one to match the other before serving.",
            err=True,
        )
        raise typer.Exit(code=1)
    uvicorn.run(
        "prism.api:app",
        host="127.0.0.1",
        port=cfg.service.http_port,
        log_level="info",
    )
