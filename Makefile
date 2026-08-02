.PHONY: proto test lint lint-fix lint-ci

dev:
	pip install -e ".[dev]"
	pre-commit install

# Generate centrifuge/protocol/client_pb2.py from client.proto.
#
# Requires `pip install -e ".[proto]"` first, on Python 3.13 or older —
# grpcio-tools 1.71.x has no wheels for newer ones. Note that it downgrades the
# protobuf runtime in the current environment to 5.29.x, reinstall a newer one
# after regenerating if needed.
#
# Uses the protoc bundled with grpcio-tools instead of a system protoc, and not
# the newest one: since protoc 27 the generated code asserts, through
# google.protobuf.runtime_version, that the runtime is at least the version
# which generated it - so a newer toolchain produces code which the older
# runtimes allowed by pyproject.toml (protobuf>=5.29.6) can not load. Checked by
# importing the result under each supported runtime:
#
#   grpcio-tools 1.71.0 (pinned)  loads on 5.29.6, 6.33.6, 7.35.1
#   grpcio-tools 1.83.0           fails on everything below 7.35.1
#
# So raising the pin further means raising the protobuf floor with it. See
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
