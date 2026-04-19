"""Alembic environment for the prism service."""

from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from prism.config import CONFIG_PATH, load_config

alembic_config = context.config

if alembic_config.config_file_name is not None:
    fileConfig(alembic_config.config_file_name)

target_metadata = None  # no ORM models in this slice


def _database_url() -> str:
    cfg = load_config(CONFIG_PATH)
    cfg.storage.sqlite_path.parent.mkdir(parents=True, exist_ok=True)
    return f"sqlite:///{cfg.storage.sqlite_path}"


def run_migrations_offline() -> None:
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    ini_section = alembic_config.get_section(
        alembic_config.config_ini_section, {}
    )
    ini_section["sqlalchemy.url"] = _database_url()
    connectable = engine_from_config(
        ini_section,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection, target_metadata=target_metadata
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
