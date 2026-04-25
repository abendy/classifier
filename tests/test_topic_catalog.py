"""Tests for the YAML topic catalog shape."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from prism.topic_catalog import TopicCatalog, flatten, load_catalog


def test_load_catalog_parses_minimal_valid_yaml(tmp_path) -> None:
    path = tmp_path / "topics.yaml"
    path.write_text(
        """
version: "topics@test"
topics:
  - id: history
    name: History
    description: Historical analysis.
""".strip(),
        encoding="utf-8",
    )

    catalog = load_catalog(path)

    assert isinstance(catalog, TopicCatalog)
    assert catalog.version == "topics@test"
    assert catalog.topics[0].id == "history"
    assert catalog.topics[0].exemplars == []


def test_load_catalog_empty_file_raises_value_error(tmp_path) -> None:
    path = tmp_path / "topics.yaml"
    path.write_text("", encoding="utf-8")

    with pytest.raises(ValueError, match="empty"):
        load_catalog(path)


def test_topic_catalog_forbids_unknown_top_level_key() -> None:
    with pytest.raises(ValidationError, match="extra"):
        TopicCatalog.model_validate(
            {
                "version": "topics@test",
                "topics": [],
                "surprise": True,
            }
        )


def test_flatten_keeps_top_level_parent_topic_id_none() -> None:
    catalog = TopicCatalog.model_validate(
        {
            "version": "topics@test",
            "topics": [
                {
                    "id": "history",
                    "name": "History",
                    "description": "Historical analysis.",
                }
            ],
        }
    )

    flat = flatten(catalog)

    assert [topic.id for topic in flat] == ["history"]
    assert flat[0].parent_topic_id is None


def test_flatten_sets_subtopic_parent_topic_id() -> None:
    catalog = TopicCatalog.model_validate(
        {
            "version": "topics@test",
            "topics": [
                {
                    "id": "history",
                    "name": "History",
                    "description": "Historical analysis.",
                    "subtopics": [
                        {
                            "id": "history-rome",
                            "name": "Rome",
                            "description": "Roman history.",
                        }
                    ],
                }
            ],
        }
    )

    flat = flatten(catalog)

    assert [topic.id for topic in flat] == ["history", "history-rome"]
    assert flat[0].subtopics == []
    assert flat[1].parent_topic_id == "history"
    assert flat[1].subtopics == []


def test_flatten_rejects_duplicate_ids_across_tree() -> None:
    catalog = TopicCatalog.model_validate(
        {
            "version": "topics@test",
            "topics": [
                {
                    "id": "history",
                    "name": "History",
                    "description": "Historical analysis.",
                    "subtopics": [
                        {
                            "id": "duplicate",
                            "name": "Rome",
                            "description": "Roman history.",
                        }
                    ],
                },
                {
                    "id": "technology",
                    "name": "Technology",
                    "description": "Technology analysis.",
                    "subtopics": [
                        {
                            "id": "duplicate",
                            "name": "Systems",
                            "description": "Systems topics.",
                        }
                    ],
                },
            ],
        }
    )

    with pytest.raises(ValueError, match="duplicate topic id"):
        flatten(catalog)


def test_flatten_rejects_subtopics_of_subtopics() -> None:
    catalog = TopicCatalog.model_validate(
        {
            "version": "topics@test",
            "topics": [
                {
                    "id": "history",
                    "name": "History",
                    "description": "Historical analysis.",
                    "subtopics": [
                        {
                            "id": "history-rome",
                            "name": "Rome",
                            "description": "Roman history.",
                            "subtopics": [
                                {
                                    "id": "history-rome-army",
                                    "name": "Roman Army",
                                    "description": "Roman military history.",
                                }
                            ],
                        }
                    ],
                }
            ],
        }
    )

    with pytest.raises(ValueError, match="subtopics-of-subtopics"):
        flatten(catalog)
