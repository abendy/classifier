"""Developer smoke and iteration commands.

These commands wrap existing public functions to provide a
faster manual-iteration loop than `prism serve` or
`pytest -m integration`. They are NOT a production surface and
NOT a parallel test framework.
"""

from __future__ import annotations

import pathlib
import sqlite3
import sys
from dataclasses import asdict
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from prism.config import CONFIG_PATH, load_config
from prism.db import connect_sqlite
from prism.ingest import ingest_once
from prism.manifest import load_manifest
from prism.serve import validate_scraper_db_schema

dev_app = typer.Typer(
    no_args_is_help=True,
    help="Developer smoke and iteration commands; not for production use.",
)


_SCRAPER_DDL = """
CREATE TABLE outbox (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_type TEXT NOT NULL,
    payload TEXT NOT NULL,
    created_at TEXT NOT NULL,
    available_at TEXT NOT NULL,
    dispatched_at TEXT,
    attempts INTEGER DEFAULT 0,
    last_error TEXT
);
CREATE TABLE tweets (
    id TEXT PRIMARY KEY,
    text TEXT,
    author_id TEXT,
    created_at TEXT,
    lang TEXT,
    full_json TEXT,
    unavailable_at TEXT
);
CREATE TABLE users (
    id TEXT PRIMARY KEY,
    name TEXT,
    username TEXT
);
CREATE TABLE media (
    tweet_id TEXT,
    type TEXT,
    url TEXT,
    preview_image_url TEXT
);
"""


@dev_app.command("seed-scraper")
def seed_scraper(
    path: Annotated[
        str | None,
        typer.Option(
            "--path",
            help="SQLite path to write. Defaults to ingest.scraper_db_path from config.",
        ),
    ] = None,
    rows: Annotated[
        int,
        typer.Option(
            "--rows", min=1, help="Number of synthetic bookmark rows to insert."
        ),
    ] = 1,
    force: Annotated[
        bool,
        typer.Option(
            "--force", help="Overwrite an existing file at the target path."
        ),
    ] = False,
) -> None:
    """Write the scraper's four-table DDL plus N synthetic bookmark rows."""
    target = (
        pathlib.Path(path)
        if path is not None
        else load_config(CONFIG_PATH).ingest.scraper_db_path
    )
    if target.exists() and not force:
        typer.echo(f"error: {target} exists. Pass --force to overwrite.", err=True)
        raise typer.Exit(code=1)
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        target.unlink()

    conn = sqlite3.connect(target)
    try:
        conn.executescript(_SCRAPER_DDL)
        ts = "2026-04-25T12:00:00+00:00"
        conn.execute(
            "INSERT INTO users (id, name, username) VALUES (?, ?, ?)",
            ("u-dev", "Dev User", "devuser"),
        )
        for i in range(rows):
            tweet_id = f"dev-{i}"
            conn.execute(
                "INSERT INTO tweets (id, text, author_id, created_at, lang, "
                "full_json, unavailable_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (tweet_id, f"synthetic body #{i}", "u-dev", ts, "en", None, None),
            )
            conn.execute(
                "INSERT INTO outbox (event_type, payload, created_at, available_at) "
                "VALUES (?, ?, ?, ?)",
                (
                    "bookmark.created",
                    f'{{"entityType":"bookmark","entityId":"{tweet_id}:root"}}',
                    ts,
                    ts,
                ),
            )
        conn.commit()
    finally:
        conn.close()

    typer.echo(f"seeded {rows} bookmark row(s) into {target}")


@dev_app.command("ingest-once")
def ingest_once_cmd(
    no_embed: Annotated[
        bool,
        typer.Option(
            "--no-embed",
            help="Skip the embed phase even when ingest.embedding_enabled is true.",
        ),
    ] = False,
) -> None:
    """Open the configured DBs, run a single ingest pass, print stats."""
    cfg = load_config(CONFIG_PATH)
    manifest = load_manifest(cfg.service.manifest_path)
    scraper_conn = connect_sqlite(cfg.ingest.scraper_db_path, load_vec=False)
    classifier_conn = connect_sqlite(
        cfg.storage.sqlite_path, load_vec=cfg.storage.sqlite_vec
    )
    http_client = None
    try:
        validate_scraper_db_schema(scraper_conn, cfg.ingest.scraper_db_path)
        embedder = None
        if cfg.ingest.embedding_enabled and not no_embed:
            from prism.embedding import create_default_embedder

            embedder = create_default_embedder()
        llm_client = None
        if cfg.ingest.classification_enabled:
            import httpx

            from prism.llm_client import OllamaClient

            http_client = httpx.Client()
            llm_client = OllamaClient(
                base_url=cfg.pipeline.topic.ollama_url,
                model=cfg.pipeline.topic.ollama_model,
                client=http_client,
            )
        stats = ingest_once(
            scraper_conn=scraper_conn,
            classifier_conn=classifier_conn,
            embedder=embedder,
            llm_client=llm_client,
            confidence_threshold=cfg.pipeline.topic.confidence_threshold,
            retrieval_top_k=cfg.pipeline.topic.retrieval_top_k,
            ollama_model=cfg.pipeline.topic.ollama_model,
            service_version=manifest.identity.version,
        )
        run_id = _latest_ingest_run_id(classifier_conn)
    finally:
        if http_client is not None:
            http_client.close()
        classifier_conn.close()
        scraper_conn.close()

    table = Table(show_header=False)
    table.add_column("Field")
    table.add_column("Count", justify="right")
    if run_id is not None:
        table.add_row("run_id", run_id)
    for field, value in asdict(stats).items():
        table.add_row(field, str(value))
    Console().print(table)


@dev_app.command("embed")
def embed_cmd(
    text: Annotated[
        str | None,
        typer.Argument(
            help="Text to embed. If omitted, reads stdin.",
        ),
    ] = None,
) -> None:
    """Embed one string with the default dense model and print metadata."""
    if text is None:
        text = sys.stdin.read()
    if not text or not text.strip():
        typer.echo("error: empty input.", err=True)
        raise typer.Exit(code=1)

    from prism.embedding import (
        EMBEDDING_DIMENSION,
        FASTEMBED_MODEL_NAME,
        MODEL_VERSION,
        create_default_embedder,
    )

    embedder = create_default_embedder()
    vec = embedder.embed(text)

    table = Table(show_header=False)
    table.add_column("Field")
    table.add_column("Value", overflow="fold")
    table.add_row("Fastembed model", FASTEMBED_MODEL_NAME)
    table.add_row("Model version", MODEL_VERSION)
    table.add_row("Dimension", str(EMBEDDING_DIMENSION))
    table.add_row("Vector dtype", str(vec.dtype))
    table.add_row(
        "First 8 components",
        ", ".join(f"{x:+.4f}" for x in vec[:8].tolist()),
    )
    Console().print(table)


@dev_app.command("load-topics")
def load_topics_cmd(
    catalog: Annotated[
        str | None,
        typer.Option(
            "--catalog",
            help="YAML catalog path. Defaults to pipeline.topic.catalog_path.",
        ),
    ] = None,
) -> None:
    """Embed every prototype in the topic catalog and persist."""
    cfg = load_config(CONFIG_PATH)
    catalog_path = (
        pathlib.Path(catalog)
        if catalog is not None
        else cfg.pipeline.topic.catalog_path
    )

    from prism.embedding import MODEL_VERSION, create_default_embedder
    from prism.topic_catalog import load_catalog
    from prism.topic_loader import embed_catalog
    from prism.topic_prototype_repo import TopicPrototypeRepo

    parsed = load_catalog(catalog_path)
    embedder = create_default_embedder()
    classifier_conn = connect_sqlite(
        cfg.storage.sqlite_path, load_vec=cfg.storage.sqlite_vec
    )
    try:
        repo = TopicPrototypeRepo(classifier_conn)
        summary = embed_catalog(
            parsed, embedder=embedder, repo=repo, model_version=MODEL_VERSION
        )
        classifier_conn.commit()
        total = repo.count(MODEL_VERSION)
    except Exception:
        classifier_conn.rollback()
        raise
    finally:
        classifier_conn.close()

    table = Table(show_header=False)
    table.add_column("Field")
    table.add_column("Value", overflow="fold")
    table.add_row("Catalog version", summary.catalog_version)
    table.add_row("Model version", summary.model_version)
    table.add_row("Topics processed", str(summary.topics_processed))
    table.add_row("Prototypes written", str(summary.prototypes_written))
    table.add_row("Total prototypes (this model)", str(total))
    Console().print(table)


@dev_app.command("retrieve-topics")
def retrieve_topics_cmd(
    envelope_id: Annotated[
        str,
        typer.Argument(help="Envelope id whose embedding seeds retrieval."),
    ],
    top_k: Annotated[
        int,
        typer.Option(
            "--top-k",
            min=1,
            help="Number of topic candidates to return.",
        ),
    ] = 5,
) -> None:
    """Retrieve top-k topic candidates for an envelope."""
    cfg = load_config(CONFIG_PATH)
    classifier_conn = connect_sqlite(
        cfg.storage.sqlite_path, load_vec=cfg.storage.sqlite_vec
    )
    try:
        from prism.embedding import MODEL_VERSION
        from prism.embedding_repo import EmbeddingRepo
        from prism.topic_prototype_repo import TopicPrototypeRepo
        from prism.topic_retrieval import retrieve_top_k_for_envelope

        candidates = retrieve_top_k_for_envelope(
            envelope_id,
            embedding_repo=EmbeddingRepo(classifier_conn),
            topic_repo=TopicPrototypeRepo(classifier_conn),
            model_version=MODEL_VERSION,
            k=top_k,
        )
    finally:
        classifier_conn.close()

    if not candidates:
        typer.echo(
            f"no topic candidates returned for envelope {envelope_id!r} "
            f"under model_version={MODEL_VERSION!r}.",
            err=True,
        )
        raise typer.Exit(code=1)

    table = Table()
    table.add_column("Rank", justify="right")
    table.add_column("Topic", overflow="fold")
    table.add_column("Score", justify="right")
    table.add_column("Best exemplar idx", justify="right")
    table.add_column("Best exemplar text", overflow="fold")
    for rank, candidate in enumerate(candidates, start=1):
        snippet = candidate.best_exemplar_text
        if len(snippet) > 80:
            snippet = snippet[:77] + "..."
        table.add_row(
            str(rank),
            candidate.topic_id,
            f"{candidate.score:+.4f}",
            str(candidate.best_exemplar_idx),
            snippet,
        )
    Console().print(table)


@dev_app.command("pick-topic")
def pick_topic_cmd(
    envelope_id: Annotated[
        str,
        typer.Argument(help="Envelope id to classify."),
    ],
    top_k: Annotated[
        int | None,
        typer.Option(
            "--top-k",
            min=1,
            help="Override pipeline.topic.retrieval_top_k.",
        ),
    ] = None,
) -> None:
    """Retrieve top-k candidates and run the LLM pick."""
    cfg = load_config(CONFIG_PATH)
    classifier_conn = connect_sqlite(
        cfg.storage.sqlite_path, load_vec=cfg.storage.sqlite_vec
    )
    try:
        import httpx

        from prism.embedding import MODEL_VERSION
        from prism.embedding_repo import EmbeddingRepo
        from prism.envelope_repo import EnvelopeRepo
        from prism.llm_client import OllamaClient
        from prism.topic_llm_pick import pick_topic
        from prism.topic_prototype_repo import TopicPrototypeRepo
        from prism.topic_retrieval import retrieve_top_k_for_envelope

        envelope = EnvelopeRepo(classifier_conn).get(envelope_id)
        if envelope is None:
            typer.echo(f"envelope {envelope_id!r} not found.", err=True)
            raise typer.Exit(code=1)
        if not envelope.content.body or not envelope.content.body.strip():
            typer.echo(
                f"envelope {envelope_id!r} has no body to classify.",
                err=True,
            )
            raise typer.Exit(code=1)

        topic_repo = TopicPrototypeRepo(classifier_conn)
        candidates = retrieve_top_k_for_envelope(
            envelope_id,
            embedding_repo=EmbeddingRepo(classifier_conn),
            topic_repo=topic_repo,
            model_version=MODEL_VERSION,
            k=top_k or cfg.pipeline.topic.retrieval_top_k,
        )
        if not candidates:
            typer.echo("no topic candidates returned.", err=True)
            raise typer.Exit(code=1)
        descriptions = topic_repo.get_descriptions(
            (candidate.topic_id for candidate in candidates), MODEL_VERSION
        )

        with httpx.Client() as http_client:
            client = OllamaClient(
                base_url=cfg.pipeline.topic.ollama_url,
                model=cfg.pipeline.topic.ollama_model,
                client=http_client,
            )
            result = pick_topic(
                envelope_body=envelope.content.body,
                candidates=candidates,
                descriptions_by_topic_id=descriptions,
                llm_client=client,
                confidence_threshold=cfg.pipeline.topic.confidence_threshold,
            )
    finally:
        classifier_conn.close()

    table = Table(show_header=False)
    table.add_column("Field")
    table.add_column("Value", overflow="fold")
    table.add_row("Envelope", envelope_id)
    table.add_row("Candidates considered", str(len(candidates)))
    table.add_row(
        "Chosen topic",
        result.chosen_topic_id or "(none - low confidence)",
    )
    if result.pick is None:
        table.add_row("LLM topic_id", "(malformed)")
        table.add_row("LLM confidence", "(malformed)")
        table.add_row("LLM reasoning", "(malformed)")
    else:
        table.add_row("LLM topic_id", result.pick.topic_id or "(empty)")
        table.add_row("LLM confidence", f"{result.pick.confidence:.4f}")
        table.add_row("LLM reasoning", result.pick.reasoning)
    if result.low_confidence_reason is not None:
        table.add_row("Low-confidence reason", result.low_confidence_reason)
    Console().print(table)


def _latest_ingest_run_id(conn: sqlite3.Connection) -> str | None:
    cursor = conn.execute(
        "SELECT id FROM runs WHERE operation = 'ingest' "
        "ORDER BY started_at DESC LIMIT 1"
    )
    try:
        row = cursor.fetchone()
    finally:
        cursor.close()
    return None if row is None else str(row[0])
