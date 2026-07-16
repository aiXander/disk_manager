PORT ?= 8000

.PHONY: install run test clean

install:
	uv sync

run: install
	uv run python -m uvicorn backend.main:app --host 127.0.0.1 --port $(PORT) --reload

test: install
	uv run python -m pytest tests/ -v

clean:
	rm -rf .venv scans/*.db __pycache__ backend/__pycache__ tests/__pycache__
	find . -name '*.pyc' -delete
