"""Tests for the log_event JSON stderr helper."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import UUID

from prism.logs import log_event

if TYPE_CHECKING:
    import pytest


def _parse(captured_err: str) -> list[dict[str, Any]]:
    return [json.loads(line) for line in captured_err.splitlines() if line]


def _strip_ts(line: str) -> str:
    data = json.loads(line)
    data.pop("ts", None)
    return json.dumps(data, sort_keys=True)


def test_log_event_emits_one_json_line_to_stderr(
    capsys: pytest.CaptureFixture[str],
) -> None:
    log_event("x.y", a=1)
    err = capsys.readouterr().err
    lines = _parse(err)
    assert len(lines) == 1
    assert lines[0]["event"] == "x.y"
    assert lines[0]["a"] == 1
    assert "ts" in lines[0]


def test_log_event_coerces_datetime_uuid_path_via_str(
    capsys: pytest.CaptureFixture[str],
) -> None:
    ts = datetime(2026, 4, 19, 12, 0, tzinfo=UTC)
    uid = UUID("00000000-0000-0000-0000-000000000001")
    p = Path("/tmp/x")
    log_event("coerce", when=ts, uid=uid, path=p)
    err = capsys.readouterr().err
    lines = _parse(err)
    assert lines[0]["when"] == str(ts)
    assert lines[0]["uid"] == str(uid)
    assert lines[0]["path"] == str(p)


def test_log_event_sort_keys_stable(
    capsys: pytest.CaptureFixture[str],
) -> None:
    log_event("x", b=1, a=2)
    log_event("x", a=2, b=1)
    err = capsys.readouterr().err
    lines = [line for line in err.splitlines() if line]
    assert len(lines) == 2
    assert _strip_ts(lines[0]) == _strip_ts(lines[1])


def test_log_event_appears_in_captured_stderr_immediately(
    capsys: pytest.CaptureFixture[str],
) -> None:
    log_event("flush.me", key="value")
    err = capsys.readouterr().err
    assert err
    assert err.endswith("\n")
    lines = _parse(err)
    assert lines[0]["key"] == "value"


def test_log_event_serializes_nested_structures(
    capsys: pytest.CaptureFixture[str],
) -> None:
    log_event("nested", payload={"n": 3, "items": [1, 2, 3]})
    err = capsys.readouterr().err
    lines = _parse(err)
    assert lines[0]["payload"] == {"n": 3, "items": [1, 2, 3]}
