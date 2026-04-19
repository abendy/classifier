"""Pydantic models and loaders for the dada.stream service manifest."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, WithJsonSchema
from pydantic.alias_generators import to_camel

MANIFEST_PATH = Path("service.json")
ManifestNumber = Annotated[int | float, WithJsonSchema({"type": "number"})]


class _Base(BaseModel):
    model_config = ConfigDict(
        alias_generator=to_camel,
        extra="forbid",
        strict=True,
    )


class Identity(_Base):
    name: str
    version: str
    description: str
    language: str
    manifest_version: str
    repository: str | None = None


class Capability(_Base):
    type: Literal[
        "ingest",
        "classify",
        "route",
        "store",
        "search",
        "process",
        "display",
        "orchestrate",
    ]
    description: str
    content_types: list[str] | None = None


class Parameter(_Base):
    name: str
    type: Literal["string", "number", "boolean", "object", "array"]
    required: bool
    description: str
    default: Any = None
    enum: list[Any] | None = None


class ResponseSchema(_Base):
    type: str
    description: str
    schema_: dict[str, Any] | None = Field(default=None, alias="schema")


class CommandContext(_Base):
    scope: Literal["item", "service", "global"]
    label: str
    content_types: list[str] | None = None
    source_services: list[str] | None = None
    icon: str | None = None


class Command(_Base):
    name: str
    description: str
    response: ResponseSchema
    idempotent: bool
    parameters: list[Parameter] | None = None
    side_effects: list[str] | None = None
    is_async: bool = Field(default=False, alias="async")
    context: CommandContext | None = None


class ApiEndpoint(_Base):
    command: str
    method: str
    path: str


class Api(_Base):
    protocol: Literal["http", "grpc", "ws"]
    host: str
    port: ManifestNumber
    endpoints: list[ApiEndpoint]
    base_path: str | None = None


class EventDeclaration(_Base):
    type: str
    description: str
    schema_: dict[str, Any] | None = Field(default=None, alias="schema")


class Events(_Base):
    publishes: list[EventDeclaration] | None = None
    subscribes: list[EventDeclaration] | None = None


class HealthCheck(_Base):
    endpoint: str
    method: str
    interval: ManifestNumber
    timeout: ManifestNumber


class Health(_Base):
    health_check: HealthCheck
    heartbeat_interval: ManifestNumber | None = None


class ConfigItem(_Base):
    name: str
    type: str
    description: str
    secret: bool
    default: Any = None


class Configuration(_Base):
    required: list[ConfigItem] | None = None
    runtime: list[ConfigItem] | None = None


class Dependencies(_Base):
    services: list[str] | None = None
    infrastructure: list[str] | None = None


class ServiceManifest(_Base):
    identity: Identity
    capabilities: list[Capability]
    commands: list[Command]
    api: Api
    events: Events
    health: Health
    configuration: Configuration
    dependencies: Dependencies


def load_manifest(path: Path) -> ServiceManifest:
    return ServiceManifest.model_validate_json(path.read_text())


def export_schema() -> dict[str, Any]:
    return ServiceManifest.model_json_schema()
