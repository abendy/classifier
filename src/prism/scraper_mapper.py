"""Map scraper x-sync rows into a dada.stream ContentEnvelope."""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from uuid_utils import uuid7

from prism.envelope import ENVELOPE_VERSION, ContentEnvelope

if TYPE_CHECKING:
    from collections.abc import Iterable
    from pathlib import Path

_SERVICE = "x-sync"
_URL_RE = re.compile(r"""https?://[^\s<>"']+""")


def open_scraper_readonly(path: Path) -> sqlite3.Connection:
    """Open the scraper SQLite file in URI read-only mode."""
    conn = sqlite3.connect(
        f"file:{path}?mode=ro",
        uri=True,
        detect_types=0,
    )
    conn.row_factory = sqlite3.Row
    return conn


class ScraperMapper:
    """Build ContentEnvelopes from rows in the scraper SQLite file."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def fetch_envelope(self, source_id: str) -> ContentEnvelope | None:
        tweet = self._fetch_tweet(source_id)
        if tweet is None:
            return None

        author_id = tweet["author_id"]
        user = self._fetch_user(author_id) if author_id else None
        media_rows = self._fetch_media(source_id)
        now = datetime.now(UTC)

        links = _dedupe(
            [
                *_extract_links_from_json(tweet["full_json"]),
                *_extract_links_from_text(tweet["text"]),
            ]
        )
        media_list: list[dict[str, Any]] = []
        for m in media_rows:
            url = m["url"] or m["preview_image_url"]
            if url:
                media_list.append({"url": url, "type": m["type"]})
        link_list = [{"url": u} for u in links]

        author_dict: dict[str, Any] | None = None
        source_url: str | None = None
        if user is not None:
            username = user["username"]
            author_dict = {
                "name": user["name"] or f"@{username}",
                "handle": username,
            }
            if username:
                source_url = f"https://x.com/{username}/status/{tweet['id']}"

        return ContentEnvelope.model_validate(
            {
                "identity": {"id": str(uuid7())},
                "source": {
                    "service": _SERVICE,
                    "sourceId": tweet["id"],
                    "sourceUrl": source_url,
                    "ingestedAt": now,
                    "sourceData": {
                        "isComplete": tweet["full_json"] is not None
                        and tweet["unavailable_at"] is None,
                    },
                },
                "content": {
                    "type": "post",
                    "title": None,
                    "body": tweet["text"],
                    "mediaUrls": media_list or None,
                    "links": link_list or None,
                    "author": author_dict,
                    "publishedAt": tweet["created_at"],
                    "language": tweet["lang"],
                    "duration": None,
                },
                "system": {
                    "version": ENVELOPE_VERSION,
                    "createdAt": now,
                    "updatedAt": now,
                },
            }
        )

    def _fetch_tweet(self, source_id: str) -> sqlite3.Row | None:
        cursor = self._conn.execute(
            "SELECT id, text, author_id, created_at, lang, full_json, unavailable_at "
            "FROM tweets WHERE id = ?",
            (source_id,),
        )
        try:
            return cursor.fetchone()
        finally:
            cursor.close()

    def _fetch_user(self, author_id: str) -> sqlite3.Row | None:
        cursor = self._conn.execute(
            "SELECT id, name, username FROM users WHERE id = ?",
            (author_id,),
        )
        try:
            return cursor.fetchone()
        finally:
            cursor.close()

    def _fetch_media(self, source_id: str) -> list[sqlite3.Row]:
        cursor = self._conn.execute(
            "SELECT url, preview_image_url, type FROM media WHERE tweet_id = ?",
            (source_id,),
        )
        try:
            return cursor.fetchall()
        finally:
            cursor.close()


def _extract_links_from_json(full_json: str | None) -> list[str]:
    if full_json is None:
        return []
    try:
        payload = json.loads(full_json)
    except (ValueError, TypeError):
        return []
    if not isinstance(payload, dict):
        return []
    entities = payload.get("entities")
    if not isinstance(entities, dict):
        return []
    urls = entities.get("urls")
    if not isinstance(urls, list):
        return []
    out: list[str] = []
    for entry in urls:
        if not isinstance(entry, dict):
            continue
        chosen = entry.get("expanded_url") or entry.get("url")
        if isinstance(chosen, str) and chosen:
            out.append(chosen)
    return out


def _extract_links_from_text(text: str | None) -> list[str]:
    if not text:
        return []
    return _URL_RE.findall(text)


def _dedupe(values: Iterable[str]) -> list[str]:
    return list(dict.fromkeys(values))
