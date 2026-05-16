PYTHON ?= python
PIP ?= $(PYTHON) -m pip
NEST_AGENT ?= $(PYTHON) -m nested_memvid_agent.cli
RUFF ?= $(PYTHON) -m ruff
MYPY ?= $(PYTHON) -m mypy
BACKEND ?= memory
PROVIDER ?= mock
MODEL ?= mock
MEMORY_DIR ?= .nest/memory
LOG_DIR ?= .nest/logs
STATE_PATH ?= .nest/state/agent.db
PORT ?= 8765
DOCKER_IMAGE ?= kestrel-agent:local

.PHONY: install install-dev install-memvid test lint typecheck compile golden validate bootstrap doctor chat-smoke server docker-build docker-doctor clean

install:
	$(PIP) install -e '.[dev]'

install-dev:
	$(PIP) install -e '.[memvid,openai,server,mcp,dev]'

install-memvid:
	$(PIP) install -e '.[dev,memvid]'

test:
	$(PYTHON) -m pytest -q

lint:
	$(RUFF) check scripts src tests

typecheck:
	$(MYPY) src

compile:
	$(PYTHON) -m compileall -q src tests scripts

bootstrap:
	$(NEST_AGENT) init --backend $(BACKEND) --memory-dir $(MEMORY_DIR)

doctor:
	$(NEST_AGENT) doctor --backend $(BACKEND) --memory-dir $(MEMORY_DIR) --provider $(PROVIDER) --model $(MODEL) --log-dir $(LOG_DIR) --state-path $(STATE_PATH)

chat-smoke:
	$(NEST_AGENT) chat --backend memory --provider mock --message "hello from packaging smoke"

golden:
	$(PYTHON) scripts/run_golden_evals.py --backend memory --provider mock

validate: compile lint typecheck test golden

server:
	$(NEST_AGENT) server --backend $(BACKEND) --memory-dir $(MEMORY_DIR) --provider $(PROVIDER) --model $(MODEL) --log-dir $(LOG_DIR) --state-path $(STATE_PATH) --host 127.0.0.1 --port $(PORT)

docker-build:
	docker build -t $(DOCKER_IMAGE) .

docker-doctor:
	docker run --rm $(DOCKER_IMAGE) nest-agent doctor --backend memory --memory-dir /tmp/kestrel-memory --provider mock --model mock

clean:
	rm -rf .pytest_cache .ruff_cache .mypy_cache build dist *.egg-info
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
