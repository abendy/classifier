install:
    uv sync

lint:
    uv run ruff check .

format:
    uv run ruff format .

typecheck:
    uv run basedpyright

test:
    uv run pytest || [ $? -eq 5 ]

check: lint typecheck test
