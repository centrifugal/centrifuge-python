import asyncio
import logging
import shutil
import ssl
import subprocess  # noqa: S404 - used to generate a throwaway TLS cert for tests.
import tempfile
import unittest
from pathlib import Path

from centrifuge import Client, ClientState
from tests.fake_server import FakeCentrifugoServer

# Tests for the ssl_context option, exercised against the in-process
# FakeCentrifugoServer running with TLS and a throwaway self-signed certificate.
#
# The important regression here: ssl must NOT be passed to websockets.connect()
# when the user did not configure a context. websockets rejects ssl=None on a
# wss:// address with ValueError instead of falling back to the default TLS
# context, so passing it unconditionally would break every wss:// connection.


def _generate_cert(directory):
    """Generate a self-signed localhost certificate, return (cert, key) paths."""
    cert_path = Path(directory) / "cert.pem"
    key_path = Path(directory) / "key.pem"
    command = [
        "openssl",
        "req",
        "-x509",
        "-newkey",
        "rsa:2048",
        "-keyout",
        str(key_path),
        "-out",
        str(cert_path),
        "-days",
        "1",
        "-nodes",
        "-subj",
        "/CN=localhost",
        "-addext",
        "subjectAltName=DNS:localhost,IP:127.0.0.1",
    ]
    subprocess.run(command, check=True, capture_output=True)  # noqa: S603
    return cert_path, key_path


@unittest.skipUnless(shutil.which("openssl"), "openssl is required to generate a test cert")
class TestSSLContext(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.cert_path, key_path = _generate_cert(self._tmpdir.name)
        server_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        server_ctx.load_cert_chain(self.cert_path, key_path)
        self.server = FakeCentrifugoServer()
        await self.server.start(ssl_context=server_ctx)

    async def asyncTearDown(self):
        await self.server.stop()
        self._tmpdir.cleanup()

    async def test_connects_to_wss_with_custom_ca(self):
        client_ctx = ssl.create_default_context(cafile=str(self.cert_path))
        client = Client(self.server.url, use_protobuf=True, ssl_context=client_ctx)
        await client.connect()
        await client.ready(timeout=5)
        self.assertEqual(client.state, ClientState.CONNECTED)
        await client.disconnect()

    async def test_connects_to_wss_with_verification_disabled(self):
        client_ctx = ssl.create_default_context()
        client_ctx.check_hostname = False
        client_ctx.verify_mode = ssl.CERT_NONE
        client = Client(self.server.url, use_protobuf=True, ssl_context=client_ctx)
        await client.connect()
        await client.ready(timeout=5)
        self.assertEqual(client.state, ClientState.CONNECTED)
        await client.disconnect()

    async def test_wss_without_ssl_context_uses_default_verification(self):
        # Without ssl_context the default TLS context must be used: connecting to
        # a server with a self-signed certificate fails verification and the error
        # is reported through on_error (rather than raising ValueError from
        # websockets, which is what passing ssl=None would do).
        # The aborted TLS handshake makes asyncio log a server-side traceback.
        asyncio_logger = logging.getLogger("asyncio")
        previous_level = asyncio_logger.level
        asyncio_logger.setLevel(logging.CRITICAL)
        self.addCleanup(asyncio_logger.setLevel, previous_level)

        error = asyncio.Future()

        async def on_error(ctx):
            if not error.done():
                error.set_result(ctx)

        client = Client(
            self.server.url,
            use_protobuf=True,
            # Keep the retry far away so a single attempt is observed.
            min_reconnect_delay=30,
            max_reconnect_delay=30,
        )
        client.events.on_error = on_error
        await client.connect()
        ctx = await asyncio.wait_for(error, timeout=5)
        self.assertIsInstance(ctx.error, ssl.SSLError)
        self.assertEqual(client.state, ClientState.CONNECTING)
        await client.disconnect()


class TestSSLContextValidation(unittest.IsolatedAsyncioTestCase):
    async def test_ssl_context_with_insecure_address_raises(self):
        with self.assertRaises(ValueError):  # noqa: PT027
            Client(
                "ws://localhost:8000/connection/websocket",
                ssl_context=ssl.create_default_context(),
            )
