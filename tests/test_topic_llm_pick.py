"""Tests for LLM topic-pick normalization."""

from __future__ import annotations

from typing import Any

import pytest

from prism.topic_llm_pick import TopicLowConfidence, TopicPicked, pick_topic
from prism.topic_retrieval import TopicCandidate


class StubClient:
    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload
        self.user: str | None = None

    def pick(
        self,
        *,
        system: str,
        user: str,
        schema: dict[str, Any],
        timeout_s: float,
    ) -> dict[str, Any]:
        _ = (system, schema, timeout_s)
        self.user = user
        return self.payload


def _candidate(topic_id: str) -> TopicCandidate:
    return TopicCandidate(
        topic_id=topic_id,
        score=0.9,
        best_exemplar_idx=0,
        best_exemplar_text=f"{topic_id} exemplar",
    )


def _payload(topic_id: str, confidence: float) -> dict[str, Any]:
    return {
        "topic_id": topic_id,
        "confidence": confidence,
        "reasoning": "best match",
    }


def test_pick_topic_returns_confident_pick() -> None:
    result = pick_topic(
        envelope_body="Roman archives",
        candidates=[_candidate("history")],
        descriptions_by_topic_id={"history": "Historical analysis."},
        llm_client=StubClient(_payload("history", 0.9)),
        confidence_threshold=0.55,
    )

    assert isinstance(result, TopicPicked)
    assert result.chosen_topic_id == "history"


def test_pick_topic_marks_below_threshold() -> None:
    result = pick_topic(
        envelope_body="Roman archives",
        candidates=[_candidate("history")],
        descriptions_by_topic_id={"history": "Historical analysis."},
        llm_client=StubClient(_payload("history", 0.54)),
        confidence_threshold=0.55,
    )

    assert isinstance(result, TopicLowConfidence)
    assert result.reason == "below-threshold"


def test_pick_topic_marks_empty_topic_id_unsure() -> None:
    result = pick_topic(
        envelope_body="Unclear note",
        candidates=[_candidate("history")],
        descriptions_by_topic_id={"history": "Historical analysis."},
        llm_client=StubClient(_payload("", 0.8)),
        confidence_threshold=0.55,
    )

    assert isinstance(result, TopicLowConfidence)
    assert result.reason == "llm-pick-unsure"


def test_pick_topic_marks_out_of_band_topic_id() -> None:
    result = pick_topic(
        envelope_body="Space telescope news",
        candidates=[_candidate("science")],
        descriptions_by_topic_id={"science": "Scientific discovery."},
        llm_client=StubClient(_payload("history", 0.9)),
        confidence_threshold=0.55,
    )

    assert isinstance(result, TopicLowConfidence)
    assert result.reason == "out-of-band"


def test_pick_topic_marks_malformed_llm_output() -> None:
    bad_payload = {
        "topic_id": "history",
        "confidence": 1.5,
        "reasoning": "schema-violating output",
    }

    result = pick_topic(
        envelope_body="anything",
        candidates=[_candidate("history")],
        descriptions_by_topic_id={"history": "Historical analysis."},
        llm_client=StubClient(bad_payload),
        confidence_threshold=0.55,
    )

    assert isinstance(result, TopicLowConfidence)
    assert result.pick is None
    assert result.reason == "llm-output-malformed"


def test_pick_topic_rejects_empty_candidates() -> None:
    with pytest.raises(ValueError, match="requires at least one candidate"):
        pick_topic(
            envelope_body="anything",
            candidates=[],
            descriptions_by_topic_id={},
            llm_client=StubClient(_payload("history", 0.9)),
            confidence_threshold=0.55,
        )


def test_pick_topic_truncates_body_in_user_prompt() -> None:
    client = StubClient(_payload("history", 0.9))

    pick_topic(
        envelope_body="abcdef",
        candidates=[_candidate("history")],
        descriptions_by_topic_id={"history": "Historical analysis."},
        llm_client=client,
        confidence_threshold=0.55,
        max_body_chars=3,
    )

    assert client.user is not None
    assert "abc" in client.user
    assert "def" not in client.user


def test_pick_topic_prompt_includes_candidate_descriptions() -> None:
    client = StubClient(_payload("science", 0.9))

    pick_topic(
        envelope_body="Space telescope news",
        candidates=[_candidate("history"), _candidate("science")],
        descriptions_by_topic_id={
            "history": "Historical analysis.",
            "science": "Scientific discovery.",
        },
        llm_client=client,
        confidence_threshold=0.55,
    )

    assert client.user is not None
    assert "id=history: Historical analysis." in client.user
    assert "id=science: Scientific discovery." in client.user
