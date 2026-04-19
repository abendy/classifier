"""Operator CLI for the classifier service."""

from __future__ import annotations

import json

import typer
from rich.console import Console
from rich.table import Table

from classifier.config import CONFIG_PATH, load_config
from classifier.manifest import ServiceManifest, export_schema, load_manifest

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
    table = Table()
    table.add_column("Field")
    table.add_column("Value")
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
    table = Table()
    table.add_column("Field")
    table.add_column("Value")
    table.add_row("HTTP port", str(loaded.service.http_port))
    table.add_row("Manifest path", str(loaded.service.manifest_path))
    table.add_row("Registry URL", loaded.service.registry_url or "(unset)")
    table.add_row("Embedding model", loaded.pipeline.embedding.model)
    table.add_row("Topic model", loaded.pipeline.topic.model)
    table.add_row("Ingest source", loaded.ingest.source)
    table.add_row("SQLite path", str(loaded.storage.sqlite_path))
    table.add_row("Phoenix enabled", str(loaded.audit.phoenix.enabled))
    Console().print(table)
