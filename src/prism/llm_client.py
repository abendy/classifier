"""Synchronous LLM client.

The ``LlmClient`` Protocol is the structural injection seam
every facet head (topic, subtopic, tags, sentiment, intent)
will reuse. ``OllamaClient`` is the production implementation,
backed by a local Ollama daemon's ``/api/chat`` endpoint with
JSON-mode constrained generation.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    import httpx


class LlmClient(Protocol):
    """Sync structured-output completion."""

    def pick(
        self,
        *,
        system: str,
        user: str,
        schema: dict[str, Any],
        timeout_s: float,
    ) -> dict[str, Any]:
        """Send a prompt and return a parsed JSON object matching ``schema``.

        ``schema`` is a JSON-Schema draft compatible with Ollama's
        ``format`` parameter (typically generated via
        ``Pydantic.model_json_schema()``). The return value is the
        parsed JSON dict; the caller validates the dict against its
        own Pydantic model — defense in depth, since Ollama's
        ``format`` is a generation hint rather than a guarantee.
        """
        ...


class OllamaClient:
    """Production ``LlmClient`` over Ollama's ``/api/chat`` endpoint."""

    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        client: httpx.Client | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._client = client

    def pick(
        self,
        *,
        system: str,
        user: str,
        schema: dict[str, Any],
        timeout_s: float,
    ) -> dict[str, Any]:
        import httpx

        body = {
            "model": self._model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "format": schema,
            "stream": False,
            "options": {"temperature": 0.0},
        }
        if self._client is not None:
            response = self._client.post(
                f"{self._base_url}/api/chat", json=body, timeout=timeout_s
            )
        else:
            response = httpx.post(
                f"{self._base_url}/api/chat", json=body, timeout=timeout_s
            )
        response.raise_for_status()
        envelope = response.json()
        content = envelope.get("message", {}).get("content")
        if not isinstance(content, str):
            raise RuntimeError(f"ollama response missing message.content: {envelope!r}")
        try:
            parsed = json.loads(content)
        except json.JSONDecodeError as exc:
            raise RuntimeError(
                f"ollama response content was not valid JSON: {content!r}"
            ) from exc
        if not isinstance(parsed, dict):
            raise RuntimeError(f"ollama response JSON was not an object: {parsed!r}")
        return parsed
