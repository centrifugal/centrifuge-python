.PHONY: proto test lint lint-fix lint-ci

dev:
	pip install -e ".[dev]"
	pre-commit install

# Generate centrifuge/protocol/client_pb2.py from client.proto.
#
# Requires `pip install -e ".[proto]"` first (note: it downgrades the protobuf
# runtime in the current environment to 4.25.x — reinstall a newer one after
# regenerating if needed).
#
# Uses the protoc bundled with grpcio-tools instead of a system protoc: the
# generated code must not require a protobuf runtime newer than the minimum
# allowed by pyproject.toml (protobuf>=4.23.4), and a system protoc is usually
# too new — its output would fail to import for users on older runtimes.
# grpcio-tools 1.62.x emits gencode for the protobuf 4.25 era which any
# runtime in the supported range can load. See
# https://github.com/centrifugal/centrifuge-python/issues/29 for background.
proto:
	python -m grpc_tools.protoc -I. --python_out=centrifuge/protocol client.proto

test:
	python -m unittest discover -s tests

lint:
	ruff .

lint-fix:
	ruff . --fix

lint-ci:
	ruff . --output-format=github
