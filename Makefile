.PHONY: proto test lint lint-fix lint-ci

dev:
	pip install -e ".[dev]"
	pre-commit install

proto:
	protoc --python_out=centrifuge/protocol client.proto

test:
	python -m unittest discover -s tests

lint:
	ruff .

lint-fix:
	ruff . --fix

lint-ci:
	ruff . --output-format=github
