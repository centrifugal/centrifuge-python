"""Preflight check for the Centrifugo server the integration tests run against.

Some suites talk to the real server from docker-compose.yml. The SDK reconnects
forever by design and those tests await publications without a deadline, so a
missing server does not make them fail - it makes them hang. Checking up front
turns "Centrifugo is not up" into an immediate, explanatory error instead.
"""

import time
import urllib.error
import urllib.request

CENTRIFUGO_HTTP_URL = "http://localhost:8000"
# How long to wait for the server on start: enough for a container which is
# still coming up, short enough to not look like a hang.
STARTUP_TIMEOUT = 5.0


def _responds():
    """True if something at the address answers HTTP - i.e. Centrifugo is up.

    Any status code counts: the URL is the WebSocket endpoint, so a plain GET is
    answered with an error, which is still proof the server is serving. A bare
    TCP connect is not enough - while a container starts, Docker's port
    forwarder already accepts connections and then resets them.
    """
    try:
        urllib.request.urlopen(  # noqa: S310 - constant http:// URL.
            CENTRIFUGO_HTTP_URL + "/connection/websocket", timeout=1
        )
    except urllib.error.HTTPError:
        return True
    except OSError:
        return False
    return True


def require_centrifugo(timeout=STARTUP_TIMEOUT):
    """Wait up to timeout seconds for Centrifugo, raise RuntimeError if absent.

    Call from setUpModule(): unittest reports the error against every test of
    the module, so the suite fails fast with a message saying what to start.
    """
    deadline = time.monotonic() + timeout
    while not _responds():
        if time.monotonic() >= deadline:
            raise RuntimeError(
                f"Centrifugo did not respond at {CENTRIFUGO_HTTP_URL} within "
                f"{timeout:.0f}s. These tests need the server from this repo's "
                f"docker-compose.yml - start it with `docker compose up -d`."
            )
        time.sleep(0.1)
