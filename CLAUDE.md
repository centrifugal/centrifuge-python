# centrifuge-python

Python asyncio WebSocket SDK for Centrifugo / Centrifuge-based servers.

## Environment

The project uses a virtualenv at `.venv` in the repo root. Activate it, or call
its interpreter directly:

```bash
. .venv/bin/activate     # or use .venv/bin/python explicitly
make dev                 # pip install -e ".[dev]" + pre-commit install
```

Do not install into the system Python.

## Tests

Most of `tests/test_client.py` runs against a real Centrifugo — the one from
`docker-compose.yml`, configured with the channel namespaces and options those
tests expect. Start it before running the suite:

```bash
docker compose up -d
make test                # python -m unittest discover -s tests
```

`tests/test_client.py` calls `require_centrifugo()` (see `tests/centrifugo.py`)
in `setUpModule`: if nothing answers on `localhost:8000` within 5 seconds, the
module errors out with a message instead of hanging. The hang is what happens
otherwise — the SDK reconnects forever, so tests awaiting publications never
return. The check only proves something is listening: a Centrifugo of another
project on port 8000 passes it and then hangs the recovery tests, which need
this compose file's channel options.

The remaining suites (`test_background_tasks`, `test_cancelled_command`,
`test_filter`, `test_fossil`, `test_json_handshake`, `test_proxy`, `test_ready`,
`test_refresh`, `test_ssl`, `test_state_invalidation`, `test_sub_refresh`,
`test_unsubscribe`, `test_utils`) use the in-process `tests/fake_server.py` or no
server at all, and run without Docker. The fake server speaks protobuf by
default; `FakeCentrifugoServer(use_protobuf=False)` switches it to JSON.

## Lint

```bash
make lint                # ruff check .
make lint-fix            # ruff check . --fix
```

Ruff config lives in `pyproject.toml`; `pre-commit` runs it on commit.
