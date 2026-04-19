"""Tests for ScraperMapper and open_scraper_readonly."""

from __future__ import annotations

import contextlib
import json
import sqlite3
import uuid
from typing import TYPE_CHECKING

import pytest

from prism.scraper_mapper import ScraperMapper, open_scraper_readonly

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from prism.envelope import ContentEnvelope


_DDL = """
CREATE TABLE tweets (id TEXT PRIMARY KEY, text TEXT, author_id TEXT, created_at TEXT,
    lang TEXT, full_json TEXT, unavailable_at TEXT);
CREATE TABLE users (id TEXT PRIMARY KEY, name TEXT, username TEXT);
CREATE TABLE media (tweet_id TEXT, type TEXT, url TEXT, preview_image_url TEXT);
"""


@pytest.fixture
def scraper_db(tmp_path: Path) -> Path:
    db_path = tmp_path / "scraper.db"
    seed = sqlite3.connect(db_path)
    try:
        seed.executescript(_DDL)
        seed.commit()
    finally:
        seed.close()
    return db_path


@contextlib.contextmanager
def _writer(db_path: Path) -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(db_path)
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def _insert_tweet(
    conn: sqlite3.Connection,
    *,
    tweet_id: str = "t-1",
    text: str | None = "hello world",
    author_id: str | None = "u-1",
    full_json: str | None = None,
    unavailable_at: str | None = None,
) -> None:
    conn.execute(
        "INSERT INTO tweets "
        "(id, text, author_id, created_at, lang, full_json, unavailable_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (tweet_id, text, author_id, "2026-04-19T11:00:00+00:00", "en", full_json, unavailable_at),
    )


def _insert_user(
    conn: sqlite3.Connection, *, name: str | None = "Ada", username: str | None = "ada"
) -> None:
    conn.execute("INSERT INTO users (id, name, username) VALUES (?, ?, ?)", ("u-1", name, username))


def _insert_media(
    conn: sqlite3.Connection,
    *,
    tweet_id: str,
    url: str | None,
    type_: str,
    preview_image_url: str | None = None,
) -> None:
    conn.execute(
        "INSERT INTO media (tweet_id, type, url, preview_image_url) VALUES (?, ?, ?, ?)",
        (tweet_id, type_, url, preview_image_url),
    )


def _map(db_path: Path, source_id: str) -> ContentEnvelope | None:
    conn = open_scraper_readonly(db_path)
    try:
        return ScraperMapper(conn).fetch_envelope(source_id)
    finally:
        conn.close()


def test_minimal_tweet_with_author(scraper_db: Path) -> None:
    with _writer(scraper_db) as w:
        _insert_user(w)
        _insert_tweet(w)

    envelope = _map(scraper_db, "t-1")
    assert envelope is not None
    assert envelope.content.body == "hello world"
    assert envelope.content.author is not None
    assert envelope.content.author.handle == "ada"
    assert envelope.content.links is None
    assert envelope.content.media_urls is None
    assert envelope.source.service == "x-sync"
    assert envelope.source.source_url == "https://x.com/ada/status/t-1"


def test_author_id_points_to_missing_user(scraper_db: Path) -> None:
    with _writer(scraper_db) as w:
        _insert_tweet(w, author_id="u-ghost")

    envelope = _map(scraper_db, "t-1")
    assert envelope is not None
    assert envelope.content.author is None
    assert envelope.source.source_url is None


def test_author_id_is_null(scraper_db: Path) -> None:
    with _writer(scraper_db) as w:
        _insert_tweet(w, author_id=None)

    envelope = _map(scraper_db, "t-1")
    assert envelope is not None
    assert envelope.content.author is None
    assert envelope.source.source_url is None


def test_tweet_with_media_rows(scraper_db: Path) -> None:
    with _writer(scraper_db) as w:
        _insert_user(w)
        _insert_tweet(w)
        _insert_media(w, tweet_id="t-1", url="https://cdn/a.jpg", type_="photo")
        _insert_media(w, tweet_id="t-1", url="https://cdn/b.mp4", type_="video")

    envelope = _map(scraper_db, "t-1")
    assert envelope is not None
    assert envelope.content.media_urls is not None
    media = envelope.content.media_urls
    assert [m.url for m in media] == ["https://cdn/a.jpg", "https://cdn/b.mp4"]
    assert [m.type for m in media] == ["photo", "video"]


def test_links_extracted_from_entities_urls(scraper_db: Path) -> None:
    full_json = json.dumps(
        {
            "entities": {
                "urls": [
                    {"url": "https://t.co/abc", "expanded_url": "https://example.com/a"},
                    {"url": "https://t.co/def"},
                    {"expanded_url": "https://example.com/c"},
                    {},
                ]
            }
        }
    )
    with _writer(scraper_db) as w:
        _insert_user(w)
        _insert_tweet(w, text="no urls here", full_json=full_json)

    envelope = _map(scraper_db, "t-1")
    assert envelope is not None
    assert envelope.content.links is not None
    urls = [link.url for link in envelope.content.links]
    assert urls == [
        "https://example.com/a",
        "https://t.co/def",
        "https://example.com/c",
    ]


def test_links_union_and_dedupe_across_sources(scraper_db: Path) -> None:
    full_json = json.dumps(
        {
            "entities": {
                "urls": [
                    {"expanded_url": "https://example.com/a"},
                    {"expanded_url": "https://example.com/b"},
                ]
            }
        }
    )
    text = "visit https://example.com/a and https://example.com/c right now"
    with _writer(scraper_db) as w:
        _insert_user(w)
        _insert_tweet(w, text=text, full_json=full_json)

    envelope = _map(scraper_db, "t-1")
    assert envelope is not None
    assert envelope.content.links is not None
    urls = [link.url for link in envelope.content.links]
    assert urls == [
        "https://example.com/a",
        "https://example.com/b",
        "https://example.com/c",
    ]


def test_full_json_null_still_extracts_text_links(scraper_db: Path) -> None:
    with _writer(scraper_db) as w:
        _insert_user(w)
        _insert_tweet(w, text="text-only https://example.com/x link", full_json=None)

    envelope = _map(scraper_db, "t-1")
    assert envelope is not None
    assert envelope.source.source_data == {"isComplete": False}
    assert envelope.content.links is not None
    assert [link.url for link in envelope.content.links] == ["https://example.com/x"]


def test_fetch_nonexistent_tweet_returns_none(scraper_db: Path) -> None:
    envelope = _map(scraper_db, "does-not-exist")
    assert envelope is None


def test_identity_id_is_parseable_uuid_and_unique(scraper_db: Path) -> None:
    with _writer(scraper_db) as w:
        _insert_user(w)
        _insert_tweet(w)

    first = _map(scraper_db, "t-1")
    second = _map(scraper_db, "t-1")
    assert first is not None
    assert second is not None
    assert uuid.UUID(first.identity.id)
    assert uuid.UUID(second.identity.id)
    assert first.identity.id != second.identity.id


def test_timestamps_share_single_instant(scraper_db: Path) -> None:
    with _writer(scraper_db) as w:
        _insert_user(w)
        _insert_tweet(w)

    envelope = _map(scraper_db, "t-1")
    assert envelope is not None
    assert envelope.source.ingested_at == envelope.system.created_at
    assert envelope.system.created_at == envelope.system.updated_at


def test_open_scraper_readonly_refuses_writes(scraper_db: Path) -> None:
    conn = open_scraper_readonly(scraper_db)
    try:
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("CREATE TABLE x (n INT)")
    finally:
        conn.close()


def test_media_url_null_falls_back_to_preview_image_url(scraper_db: Path) -> None:
    with _writer(scraper_db) as w:
        _insert_user(w)
        _insert_tweet(w)
        _insert_media(
            w, tweet_id="t-1", url=None, type_="video",
            preview_image_url="https://cdn/thumb.jpg",
        )

    envelope = _map(scraper_db, "t-1")
    assert envelope is not None
    assert envelope.content.media_urls is not None
    assert [m.url for m in envelope.content.media_urls] == ["https://cdn/thumb.jpg"]


def test_user_name_null_falls_back_to_handle(scraper_db: Path) -> None:
    with _writer(scraper_db) as w:
        _insert_user(w, name=None, username="ada")
        _insert_tweet(w)

    envelope = _map(scraper_db, "t-1")
    assert envelope is not None
    assert envelope.content.author is not None
    assert envelope.content.author.name == "@ada"
    assert envelope.content.author.handle == "ada"


def test_malformed_full_json_does_not_raise(scraper_db: Path) -> None:
    with _writer(scraper_db) as w:
        _insert_user(w)
        _insert_tweet(w, full_json="{not json", text="no urls here")

    envelope = _map(scraper_db, "t-1")
    assert envelope is not None
    assert envelope.content.links is None


def test_unavailable_tweet_marks_is_complete_false(scraper_db: Path) -> None:
    with _writer(scraper_db) as w:
        _insert_user(w)
        _insert_tweet(
            w,
            full_json='{"entities": {"urls": []}}',
            unavailable_at="2026-04-19T10:00:00+00:00",
        )

    envelope = _map(scraper_db, "t-1")
    assert envelope is not None
    assert envelope.source.source_data == {"isComplete": False}
