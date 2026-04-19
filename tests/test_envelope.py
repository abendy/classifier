"""Tests for the content envelope models and schema export."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from prism.envelope import ContentEnvelope, State, export_schema

TS = datetime(2026, 4, 19, 12, 0, tzinfo=UTC)


def _minimal_dict() -> dict[str, Any]:
    return {
        "identity": {"id": "env-1"},
        "source": {"service": "x-sync", "ingestedAt": TS},
        "content": {"type": "post"},
        "system": {"version": "1.0.0", "createdAt": TS, "updatedAt": TS},
    }


def test_minimal_envelope_validates() -> None:
    envelope = ContentEnvelope.model_validate(_minimal_dict())
    assert envelope.identity.id == "env-1"


def test_fully_populated_envelope_round_trips() -> None:
    data: dict[str, Any] = {
        "identity": {"id": "env-2"},
        "source": {
            "service": "x-sync",
            "sourceId": "tweet-42",
            "sourceUrl": "https://x.com/u/status/42",
            "ingestedAt": TS,
            "sourceData": {"retweets": 7, "raw": {"nested": True}},
        },
        "content": {
            "type": "video",
            "title": "Demo clip",
            "body": "Long description here.",
            "summary": "Demo.",
            "mediaUrls": [
                {"url": "https://cdn/m.mp4", "type": "video/mp4", "alt": "clip"},
            ],
            "links": [
                {"url": "https://ref", "title": "Ref", "domain": "ref"},
            ],
            "author": {
                "name": "Ada",
                "handle": "ada",
                "url": "https://x.com/ada",
                "avatarUrl": "https://cdn/a.png",
            },
            "publishedAt": TS,
            "language": "en",
            "duration": 42.5,
        },
        "classification": {
            "tags": ["ml", "demo"],
            "topicAssignments": [
                {
                    "topicId": "t-1",
                    "subtopicId": "st-1",
                    "confidence": 0.87,
                    "matchedTerms": ["ml"],
                },
            ],
            "classifiedAt": TS,
            "classifiedBy": "rules@1.0",
        },
        "routing": {
            "destinations": ["inbox-a"],
            "routedAt": TS,
            "rules": ["r-1"],
        },
        "state": {
            "status": "starred",
            "taskStatus": "todo",
            "priority": "high",
            "annotations": [
                {
                    "id": "a-1",
                    "body": "interesting",
                    "type": "note",
                    "createdAt": TS,
                    "anchor": "00:12",
                },
            ],
            "lastInteractedAt": TS,
        },
        "relationships": {"parentId": "env-0", "type": "reply"},
        "system": {"version": "1.0.0", "createdAt": TS, "updatedAt": TS},
    }
    envelope = ContentEnvelope.model_validate(data)
    serialized = envelope.model_dump_json(by_alias=True, exclude_none=True)
    assert ContentEnvelope.model_validate_json(serialized) == envelope


def test_unknown_field_at_root_is_rejected() -> None:
    data = _minimal_dict()
    data["foo"] = "bar"
    with pytest.raises(ValidationError):
        ContentEnvelope.model_validate(data)


def test_unknown_field_in_nested_model_is_rejected() -> None:
    data = _minimal_dict()
    data["content"]["spurious"] = "x"
    with pytest.raises(ValidationError):
        ContentEnvelope.model_validate(data)


def test_snake_case_alias_input_is_rejected() -> None:
    data = _minimal_dict()
    data["source"]["source_id"] = "tweet-42"
    with pytest.raises(ValidationError):
        ContentEnvelope.model_validate(data)


def test_invalid_content_type_is_rejected() -> None:
    data = _minimal_dict()
    data["content"]["type"] = "tweet"
    with pytest.raises(ValidationError):
        ContentEnvelope.model_validate(data)


def test_state_status_defaults_to_unread() -> None:
    assert State().status == "unread"


def test_iso8601_string_accepted_on_dict_validation_path() -> None:
    data = _minimal_dict()
    data["source"]["ingestedAt"] = "2026-04-19T12:00:00+00:00"
    data["system"]["createdAt"] = "2026-04-19T12:00:00+00:00"
    data["system"]["updatedAt"] = "2026-04-19T12:00:00+00:00"
    envelope = ContentEnvelope.model_validate(data)
    assert isinstance(envelope.source.ingested_at, datetime)
    assert envelope.source.ingested_at.tzinfo is not None


def test_empty_identity_id_is_rejected() -> None:
    data = _minimal_dict()
    data["identity"]["id"] = ""
    with pytest.raises(ValidationError):
        ContentEnvelope.model_validate(data)


def test_committed_schema_matches_export() -> None:
    on_disk = json.loads(Path("contracts/content-envelope.schema.json").read_text())
    current = export_schema()
    assert on_disk == current, (
        "JSON Schema is stale. Re-run: "
        'uv run python -c "from prism.envelope import export_schema; import json; '
        'print(json.dumps(export_schema(), indent=2, sort_keys=True))" '
        "> contracts/content-envelope.schema.json"
    )
