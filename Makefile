.PHONY: install install-memvid test lint typecheck validate bootstrap clean

install:
	python -m pip install -e .[dev]

install-memvid:
	python -m pip install -e .[dev,memvid]

test:
	pytest

lint:
	ruff check .

typecheck:
	mypy src

bootstrap:
	python scripts/bootstrap_memory.py --backend memory --memory-dir ./memory

validate:
	python scripts/run_validation.py --backend memory --memory-dir ./memory

clean:
	rm -rf .pytest_cache .ruff_cache .mypy_cache build dist *.egg-info
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
