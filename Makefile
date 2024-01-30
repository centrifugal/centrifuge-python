.PHONY: proto test lint lint-ci

dev:
	pip install -e ".[dev]"

proto:
	protoc --python_out=centrifuge/protocol client.proto

test:
	python -m unittest discover -s tests

lint:
	ruff .

lint-ci:
	ruff . --output-format=github
