.PHONY: proto test lint lint-fix lint-ci

dev:
	pip install -e ".[dev]"
	pre-commit install

# Generate centrifuge/protocol/client_pb2.py from client.proto.
#
# Requires `pip install -e ".[proto]"` first, on Python 3.12 or older —
# grpcio-tools 1.62.x has no wheels for newer ones. Note that it downgrades the
# protobuf runtime in the current environment to 4.25.x, reinstall a newer one
# after regenerating if needed.
#
# Uses the protoc bundled with grpcio-tools instead of a system protoc, and an
# old one deliberately: generated code must load with every protobuf runtime
# allowed by pyproject.toml (protobuf>=4.25.9). Since protoc 27 the generated
# code imports google.protobuf.runtime_version and asserts the runtime is at
# least the version which generated it - that module does not exist before
# protobuf 5.27, and the assert rules out older runtimes anyway. Checked by
# importing the result under each supported runtime:
#
#   grpcio-tools 1.62.3 (pinned)  loads on 4.25.9, 5.29.6, 6.33.6, 7.35.1
#   grpcio-tools 1.71.0           fails on 4.25.9
#   grpcio-tools 1.83.0           fails on everything below 7.35.1
#
# So the pin can only be raised once the protobuf 4.x line is dropped: with a
# 5.29.6 floor, grpcio-tools 1.71.x becomes usable. See
# https://github.com/centrifugal/centrifuge-python/issues/29 for background.
proto:
	python -m grpc_tools.protoc -I. --python_out=centrifuge/protocol client.proto

test:
	python -m unittest discover -s tests

lint:
	ruff check .

lint-fix:
	ruff check . --fix

lint-ci:
	ruff check . --output-format=github
