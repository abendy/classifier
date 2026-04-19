"""Operator CLI for the classifier service."""

from __future__ import annotations

import json

import typer
from rich.console import Console
from rich.table import Table

from classifier.manifest import MANIFEST_PATH, export_schema, load_manifest

app = typer.Typer(no_args_is_help=True)


@app.command()
def manifest() -> None:
    """Validate and pretty-print the service manifest."""
    model = load_manifest(MANIFEST_PATH)
    typer.echo(model.model_dump_json(indent=2, exclude_none=True, by_alias=True))


@app.command()
def status() -> None:
    """Show a summary of the service manifest."""
    model = load_manifest(MANIFEST_PATH)
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
    typer.echo(json.dumps(export_schema(), indent=2, sort_keys=True))
