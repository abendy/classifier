"""Tests for the Ollama LLM client."""

from __future__ import annotations

from typing import Any, cast

import httpx
import pytest

from prism.llm_client import OllamaClient


class _Response:
    def __init__(
        self,
        payload: dict[str, Any],
        *,
        error: httpx.HTTPStatusError | None = None,
    ) -> None:
        self._payload = payload
        self._error = error

    def raise_for_status(self) -> None:
        if self._error is not None:
            raise self._error

    def json(self) -> dict[str, Any]:
        return self._payload


def test_ollama_pick_posts_expected_body_and_returns_parsed_dict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    def fake_post(
        url: str, *, json: dict[str, Any], timeout: float
    ) -> _Response:
        captured["url"] = url
        captured["json"] = json
        captured["timeout"] = timeout
        return _Response({"message": {"content": '{"topic_id":"history"}'}})

    monkeypatch.setattr(httpx, "post", fake_post)

    result = OllamaClient(base_url="http://ollama/", model="qwen").pick(
        system="sys",
        user="user",
        schema={"type": "object"},
        timeout_s=12.5,
    )

    assert result == {"topic_id": "history"}
    assert captured["url"] == "http://ollama/api/chat"
    assert captured["timeout"] == 12.5
    assert captured["json"] == {
        "model": "qwen",
        "messages": [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "user"},
        ],
        "format": {"type": "object"},
        "stream": False,
        "options": {"temperature": 0.0},
    }


def test_ollama_pick_uses_injected_client(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    class _FakeClient:
        def post(
            self,
            url: str,
            *,
            json: dict[str, Any],
            timeout: float,
        ) -> _Response:
            _ = (json, timeout)
            captured["url"] = url
            captured["via"] = "injected"
            return _Response({"message": {"content": '{"topic_id":"x"}'}})

    def fail_module_post(*args: object, **kwargs: object) -> None:
        _ = (args, kwargs)
        raise AssertionError("module-level httpx.post should not be called")

    monkeypatch.setattr(httpx, "post", fail_module_post)

    OllamaClient(
        base_url="http://ollama",
        model="qwen",
        client=cast("httpx.Client", _FakeClient()),
    ).pick(
        system="sys",
        user="user",
        schema={"type": "object"},
        timeout_s=1.0,
    )

    assert captured["url"] == "http://ollama/api/chat"
    assert captured["via"] == "injected"


def test_ollama_pick_propagates_http_status_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = httpx.Request("POST", "http://ollama/api/chat")
    response = httpx.Response(500, request=request)
    error = httpx.HTTPStatusError("boom", request=request, response=response)

    def fake_post(
        url: str, *, json: dict[str, Any], timeout: float
    ) -> _Response:
        return _Response({}, error=error)

    monkeypatch.setattr(httpx, "post", fake_post)

    with pytest.raises(httpx.HTTPStatusError):
        OllamaClient(base_url="http://ollama", model="qwen").pick(
            system="sys",
            user="user",
            schema={},
            timeout_s=1.0,
        )


def test_ollama_pick_rejects_missing_message_content(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_post(
        url: str, *, json: dict[str, Any], timeout: float
    ) -> _Response:
        return _Response({"message": {}})

    monkeypatch.setattr(httpx, "post", fake_post)

    with pytest.raises(RuntimeError, match=r"missing message\.content"):
        OllamaClient(base_url="http://ollama", model="qwen").pick(
            system="sys",
            user="user",
            schema={},
            timeout_s=1.0,
        )


def test_ollama_pick_rejects_non_json_content(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_post(
        url: str, *, json: dict[str, Any], timeout: float
    ) -> _Response:
        return _Response({"message": {"content": "not-json"}})

    monkeypatch.setattr(httpx, "post", fake_post)

    with pytest.raises(RuntimeError, match="not valid JSON"):
        OllamaClient(base_url="http://ollama", model="qwen").pick(
            system="sys",
            user="user",
            schema={},
            timeout_s=1.0,
        )
