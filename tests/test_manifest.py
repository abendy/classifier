"""Tests for the service manifest models, loader, and schema export."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from classifier.manifest import ServiceManifest, export_schema, load_manifest


def test_load_manifest_happy_path() -> None:
    model = load_manifest(Path("service.json"))
    assert model.identity.name == "classifier"


def test_missing_response_description_is_rejected() -> None:
    data = json.loads(Path("service.json").read_text())
    del data["commands"][0]["response"]["description"]
    with pytest.raises(ValidationError):
        ServiceManifest.model_validate(data)


def test_wrong_type_for_idempotent_is_rejected() -> None:
    data = json.loads(Path("service.json").read_text())
    data["commands"][0]["idempotent"] = "true"
    with pytest.raises(ValidationError):
        ServiceManifest.model_validate(data)


def test_unknown_field_at_root_is_rejected() -> None:
    data = json.loads(Path("service.json").read_text())
    data["foo"] = "bar"
    with pytest.raises(ValidationError):
        ServiceManifest.model_validate(data)


def test_unknown_field_in_nested_model_is_rejected() -> None:
    data = json.loads(Path("service.json").read_text())
    data["identity"]["spurious"] = "x"
    with pytest.raises(ValidationError):
        ServiceManifest.model_validate(data)


def test_whole_number_float_port_is_accepted() -> None:
    data = json.loads(Path("service.json").read_text())
    data["api"]["port"] = 3200.0
    model = ServiceManifest.model_validate(data)
    assert model.api.port == 3200.0


def test_snake_case_async_alias_is_rejected() -> None:
    data = json.loads(Path("service.json").read_text())
    data["commands"][0].pop("async", None)
    data["commands"][0]["is_async"] = False
    with pytest.raises(ValidationError):
        ServiceManifest.model_validate(data)


def test_manifest_cli_output_round_trips() -> None:
    model = load_manifest(Path("service.json"))
    serialized = model.model_dump_json(indent=2, by_alias=True, exclude_none=True)
    assert ServiceManifest.model_validate_json(serialized) == model


def test_committed_schema_matches_export() -> None:
    on_disk = json.loads(Path("contracts/service-manifest.schema.json").read_text())
    current = export_schema()
    assert on_disk == current, (
        "JSON Schema is stale. Re-run: "
        "uv run classifier schema > contracts/service-manifest.schema.json"
    )


def test_contract_number_fields_export_as_json_number() -> None:
    schema = export_schema()
    assert schema["$defs"]["Api"]["properties"]["port"]["type"] == "number"
    assert schema["$defs"]["HealthCheck"]["properties"]["interval"]["type"] == "number"
    assert schema["$defs"]["HealthCheck"]["properties"]["timeout"]["type"] == "number"
    heartbeat_types = {
        option["type"]
        for option in schema["$defs"]["Health"]["properties"]["heartbeatInterval"]["anyOf"]
    }
    assert heartbeat_types == {"number", "null"}
