VENV := .venv
PYTHON := $(VENV)/bin/python
PIP := $(VENV)/bin/pip
PORT ?= 8000

.PHONY: install run clean test

$(VENV)/bin/activate:
	python3 -m venv $(VENV)
	$(PIP) install --upgrade pip

install: $(VENV)/bin/activate
	$(PIP) install -r requirements.txt

run: install
	$(PYTHON) -m uvicorn backend.main:app --host 127.0.0.1 --port $(PORT) --reload

test: install
	$(PIP) install pytest pytest-asyncio
	$(PYTHON) -m pytest tests/ -v

clean:
	rm -rf $(VENV) scans/*.db __pycache__ backend/__pycache__ tests/__pycache__
	find . -name '*.pyc' -delete
