"""Pydantic models and loader for the prism service config."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field

CONFIG_PATH = Path("config.yaml")
_Path = Annotated[Path, Field(strict=False)]


class _Base(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class ServiceConfig(_Base):
    http_port: int
    manifest_path: _Path
    registry_url: str | None = None


class EmbeddingConfig(_Base):
    model: str
    version: str


class TopicConfig(_Base):
    model: str
    quantization: str
    confidence_threshold: float
    retrieval_top_k: int


class TagsConfig(_Base):
    model: str
    max_tags: int


class SentimentIntentConfig(_Base):
    bundled_with: Literal["tags", "topic"] | None = None


class PipelineConfig(_Base):
    embedding: EmbeddingConfig
    topic: TopicConfig
    tags: TagsConfig
    sentiment_intent: SentimentIntentConfig


class IngestConfig(_Base):
    source: str
    scraper_db_path: _Path
    poll_interval_ms: int
    embedding_enabled: bool = True


class PhoenixConfig(_Base):
    enabled: bool
    local_url: str


class AuditConfig(_Base):
    phoenix: PhoenixConfig
    runs_retention_days: int


class StorageConfig(_Base):
    sqlite_path: _Path
    sqlite_vec: bool
    duckdb_attach: bool


class BackfillConfig(_Base):
    batch_size: int


class EvalConfig(_Base):
    holdout_ratio: float


class DspyCompileConfig(_Base):
    max_rounds: int


class JobsConfig(_Base):
    backfill: BackfillConfig
    eval: EvalConfig
    dspy_compile: DspyCompileConfig


class Config(_Base):
    service: ServiceConfig
    pipeline: PipelineConfig
    ingest: IngestConfig
    audit: AuditConfig
    storage: StorageConfig
    jobs: JobsConfig


def load_config(path: Path) -> Config:
    return Config.model_validate(yaml.safe_load(path.read_text()))
