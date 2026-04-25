"""LLM-based topic pick over retrieval candidates.

Reduces the top-k candidates from ``topic_retrieval`` to a
single picked topic with confidence, or one of three
low-confidence outcomes: the LLM was unsure, picked a topic
not in the candidate set (hallucination guard), or picked
with confidence below the configured threshold.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

if TYPE_CHECKING:
    from prism.llm_client import LlmClient
    from prism.topic_retrieval import TopicCandidate


LowConfidenceReason = Literal[
    "below-threshold",
    "out-of-band",
    "llm-pick-unsure",
    "llm-output-malformed",
]


class TopicPick(BaseModel):
    """LLM output shape (validated against the response).

    ``topic_id`` is allowed to be the empty string when the LLM
    judges no candidate clearly matches; that path short-circuits
    to ``llm-pick-unsure``. ``confidence`` is constrained to
    ``[0, 1]``; out-of-range values raise ``ValidationError`` at
    ``model_validate`` time.
    """

    model_config = ConfigDict(extra="forbid", strict=True)

    topic_id: str
    confidence: float = Field(ge=0.0, le=1.0)
    reasoning: str


_TOPIC_PICK_SCHEMA = TopicPick.model_json_schema()


@dataclass(frozen=True)
class TopicPickResult:
    pick: TopicPick | None
    chosen_topic_id: str | None
    low_confidence_reason: LowConfidenceReason | None


_LOGGER = logging.getLogger(__name__)


_SYSTEM_PROMPT = (
    "You are a content classifier. Given a piece of content "
    "and a short list of candidate topics, select the single "
    "topic that best matches. If none of the candidates "
    "clearly fit, set topic_id to the empty string. Confidence "
    "is your degree of certainty in [0, 1]; values below 0.55 "
    "indicate the choice should be reviewed manually."
)


def pick_topic(
    *,
    envelope_body: str,
    candidates: list[TopicCandidate],
    descriptions_by_topic_id: dict[str, str],
    llm_client: LlmClient,
    confidence_threshold: float,
    timeout_s: float = 30.0,
    max_body_chars: int = 4000,
) -> TopicPickResult:
    """Reduce retrieval candidates to a single picked topic."""
    if not candidates:
        raise ValueError("pick_topic requires at least one candidate")
    candidate_ids = {candidate.topic_id for candidate in candidates}

    user_prompt = _build_user_prompt(
        envelope_body=envelope_body[:max_body_chars],
        candidates=candidates,
        descriptions=descriptions_by_topic_id,
    )
    raw = llm_client.pick(
        system=_SYSTEM_PROMPT,
        user=user_prompt,
        schema=_TOPIC_PICK_SCHEMA,
        timeout_s=timeout_s,
    )
    try:
        pick = TopicPick.model_validate(raw)
    except ValidationError:
        _LOGGER.warning("topic_llm_pick.malformed_response", extra={"raw": raw})
        return TopicPickResult(
            pick=None,
            chosen_topic_id=None,
            low_confidence_reason="llm-output-malformed",
        )

    if not pick.topic_id:
        return TopicPickResult(
            pick=pick,
            chosen_topic_id=None,
            low_confidence_reason="llm-pick-unsure",
        )
    if pick.topic_id not in candidate_ids:
        return TopicPickResult(
            pick=pick,
            chosen_topic_id=None,
            low_confidence_reason="out-of-band",
        )
    if pick.confidence < confidence_threshold:
        return TopicPickResult(
            pick=pick,
            chosen_topic_id=None,
            low_confidence_reason="below-threshold",
        )
    return TopicPickResult(
        pick=pick,
        chosen_topic_id=pick.topic_id,
        low_confidence_reason=None,
    )


def _build_user_prompt(
    *,
    envelope_body: str,
    candidates: list[TopicCandidate],
    descriptions: dict[str, str],
) -> str:
    """Compose the user-message body fed to the LLM."""
    lines: list[str] = ["Content:", envelope_body, "", "Candidate topics:"]
    for candidate in candidates:
        description = descriptions.get(candidate.topic_id, "")
        lines.append(f"- id={candidate.topic_id}: {description}")
    lines.append("")
    lines.append(
        "Respond with topic_id (one of the candidate ids above, or "
        "empty string), confidence in [0, 1], and a one-sentence "
        "reasoning."
    )
    return "\n".join(lines)
