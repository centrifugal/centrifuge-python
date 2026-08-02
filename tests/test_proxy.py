import asyncio
import base64
import contextlib
import logging
import os
import shutil
import ssl
import tempfile
import unittest
from unittest import mock

import centrifuge.client
from centrifuge import Client, ClientState
from tests.fake_server import FakeCentrifugoServer
from tests.test_ssl import _generate_cert

# Tests for the proxy option (https://github.com/centrifugal/centrifuge-python/issues/45),
# exercised against a minimal in-process HTTP proxy (CONNECT method) in front of
# the FakeCentrifugoServer.


async def _pipe(reader, writer, recorder=None):
    try:
        while True:
            data = await reader.read(65536)
            if not data:
                break
            if recorder is not None:
                recorder.append(data)
            writer.write(data)
            await writer.drain()
    except (ConnectionError, asyncio.IncompleteReadError):
        pass
    finally:
        with contextlib.suppress(ConnectionError):
            writer.close()


class ConnectProxy:
    """Minimal HTTP proxy supporting the CONNECT method, for tests."""

    def __init__(self, username="", password=""):
        self._server = None
        self.port = 0
        self._credentials = (username, password) if username else None
        # CONNECT targets ("host:port") received, in order.
        self.connect_targets = []
        # Proxy-Authorization header values received, in order.
        self.auth_headers = []
        # Everything the client sent through the established tunnel.
        self.tunneled = []

    async def start(self):
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        self.port = self._server.sockets[0].getsockname()[1]

    async def stop(self):
        if self._server:
            self._server.close()
            await self._server.wait_closed()

    @property
    def url(self):
        if self._credentials:
            username, password = self._credentials
            return f"http://{username}:{password}@127.0.0.1:{self.port}"
        return f"http://127.0.0.1:{self.port}"

    def _authorized(self, headers):
        if not self._credentials:
            return True
        username, password = self._credentials
        expected = base64.b64encode(f"{username}:{password}".encode()).decode()
        return headers.get("proxy-authorization") == f"Basic {expected}"

    async def _handle(self, reader, writer):
        try:
            request = await reader.readuntil(b"\r\n\r\n")
        except (asyncio.IncompleteReadError, ConnectionError):
            writer.close()
            return

        lines = request.decode().split("\r\n")
        method, _, target = lines[0].partition(" ")
        target = target.split(" ")[0]
        headers = {}
        for line in lines[1:]:
            name, sep, value = line.partition(":")
            if sep:
                headers[name.strip().lower()] = value.strip()

        if method == "CONNECT":
            self.connect_targets.append(target)
        if "proxy-authorization" in headers:
            self.auth_headers.append(headers["proxy-authorization"])

        if method != "CONNECT":
            writer.write(b"HTTP/1.1 405 Method Not Allowed\r\n\r\n")
            await writer.drain()
            writer.close()
            return

        if not self._authorized(headers):
            writer.write(b"HTTP/1.1 407 Proxy Authentication Required\r\n\r\n")
            await writer.drain()
            writer.close()
            return

        host, _, port = target.rpartition(":")
        try:
            remote_reader, remote_writer = await asyncio.open_connection(host, int(port))
        except OSError:
            writer.write(b"HTTP/1.1 502 Bad Gateway\r\n\r\n")
            await writer.drain()
            writer.close()
            return

        writer.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
        await writer.drain()
        await asyncio.gather(
            _pipe(reader, remote_writer, recorder=self.tunneled),
            _pipe(remote_reader, writer),
            return_exceptions=True,
        )


class TestProxyBase(unittest.IsolatedAsyncioTestCase):
    proxy_username = ""
    proxy_password = ""

    async def asyncSetUp(self):
        self.server = FakeCentrifugoServer()
        await self.server.start()
        self.proxy = ConnectProxy(self.proxy_username, self.proxy_password)
        await self.proxy.start()

    async def asyncTearDown(self):
        await self.proxy.stop()
        await self.server.stop()

    def target(self):
        return f"localhost:{self.server.port}"


class TestHTTPProxy(TestProxyBase):
    async def test_connects_through_proxy(self):
        client = Client(self.server.url, use_protobuf=True, proxy=self.proxy.url)
        await client.connect()
        await client.ready(timeout=5)
        self.assertEqual(client.state, ClientState.CONNECTED)
        self.assertEqual(self.proxy.connect_targets, [self.target()])
        await client.disconnect()

    async def test_proxy_from_environment_used_by_default(self):
        # Without an explicit option websockets takes the proxy from the
        # environment - the SDK must not interfere with that.
        env = {"ws_proxy": self.proxy.url, "no_proxy": ""}
        with mock.patch.dict(os.environ, env, clear=False):
            client = Client(self.server.url, use_protobuf=True)
            await client.connect()
            await client.ready(timeout=5)
            self.assertEqual(client.state, ClientState.CONNECTED)
            self.assertEqual(self.proxy.connect_targets, [self.target()])
            await client.disconnect()

    async def test_proxy_none_ignores_environment(self):
        env = {"ws_proxy": self.proxy.url, "no_proxy": ""}
        with mock.patch.dict(os.environ, env, clear=False):
            client = Client(self.server.url, use_protobuf=True, proxy=None)
            await client.connect()
            await client.ready(timeout=5)
            self.assertEqual(client.state, ClientState.CONNECTED)
            self.assertEqual(self.proxy.connect_targets, [])
            await client.disconnect()

    async def test_unreachable_proxy_reported_as_error(self):
        error = asyncio.Future()

        async def on_error(ctx):
            if not error.done():
                error.set_result(ctx)

        # Port 1 is reserved and nothing listens on it.
        client = Client(
            self.server.url,
            use_protobuf=True,
            proxy="http://127.0.0.1:1",
            min_reconnect_delay=30,
            max_reconnect_delay=30,
        )
        client.events.on_error = on_error
        await client.connect()
        await asyncio.wait_for(error, timeout=5)
        self.assertEqual(client.state, ClientState.CONNECTING)
        await client.disconnect()


class TestHTTPProxyAuth(TestProxyBase):
    proxy_username = "user"
    proxy_password = "pass"  # noqa: S105 - test credentials.

    async def test_credentials_from_proxy_url_sent(self):
        client = Client(self.server.url, use_protobuf=True, proxy=self.proxy.url)
        await client.connect()
        await client.ready(timeout=5)
        self.assertEqual(client.state, ClientState.CONNECTED)
        self.assertEqual(self.proxy.connect_targets, [self.target()])
        expected = base64.b64encode(b"user:pass").decode()
        self.assertEqual(self.proxy.auth_headers, [f"Basic {expected}"])
        await client.disconnect()

    async def test_credentials_not_forwarded_to_server(self):
        # Proxy credentials must only be sent to the proxy itself, never to the
        # Centrifugo server behind it.
        client = Client(self.server.url, use_protobuf=True, proxy=self.proxy.url)
        await client.connect()
        await client.ready(timeout=5)
        await client.disconnect()
        tunneled = b"".join(self.proxy.tunneled).lower()
        self.assertNotIn(b"proxy-authorization", tunneled)
        self.assertNotIn(base64.b64encode(b"user:pass").lower(), tunneled)
        self.assertNotIn(b"user:pass", tunneled)


@unittest.skipUnless(shutil.which("openssl"), "openssl is required to generate a test cert")
class TestProxyWithTLS(unittest.IsolatedAsyncioTestCase):
    """A proxy must not weaken TLS: the tunnel stays end to end encrypted."""

    async def asyncSetUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.cert_path, key_path = _generate_cert(self._tmpdir.name)
        server_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        server_ctx.load_cert_chain(self.cert_path, key_path)
        self.server = FakeCentrifugoServer()
        await self.server.start(ssl_context=server_ctx)
        self.proxy = ConnectProxy()
        await self.proxy.start()

    async def asyncTearDown(self):
        await self.proxy.stop()
        await self.server.stop()
        self._tmpdir.cleanup()

    async def test_wss_through_proxy_is_opaque_to_proxy(self):
        client_ctx = ssl.create_default_context(cafile=str(self.cert_path))
        client = Client(
            self.server.url, use_protobuf=True, ssl_context=client_ctx, proxy=self.proxy.url
        )
        await client.connect()
        await client.ready(timeout=5)
        self.assertEqual(client.state, ClientState.CONNECTED)
        self.assertEqual(self.proxy.connect_targets, [f"localhost:{self.server.port}"])
        await client.disconnect()

        # Beyond the CONNECT request the proxy only relays TLS records (the first
        # byte of a TLS handshake record is 0x16) - the WebSocket handshake and
        # the connection token inside it are not visible to it.
        tunneled = b"".join(self.proxy.tunneled)
        self.assertTrue(tunneled.startswith(b"\x16"))
        self.assertNotIn(b"Sec-WebSocket-Key", tunneled)

    async def test_certificate_still_verified_through_proxy(self):
        # Tunneling must not silently skip verification of the server
        # certificate: without a matching CA the self-signed certificate of the
        # test server is rejected, exactly like on a direct connection.
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
            proxy=self.proxy.url,
            # Keep the retry far away so a single attempt is observed.
            min_reconnect_delay=30,
            max_reconnect_delay=30,
        )
        client.events.on_error = on_error
        await client.connect()
        ctx = await asyncio.wait_for(error, timeout=5)
        self.assertIsInstance(ctx.error, ssl.SSLCertVerificationError)
        self.assertEqual(client.state, ClientState.CONNECTING)
        self.assertEqual(self.proxy.connect_targets, [f"localhost:{self.server.port}"])
        await client.disconnect()


class TestProxyValidation(unittest.IsolatedAsyncioTestCase):
    """Proxy misconfiguration must be reported by the constructor.

    Otherwise it only surfaces in the middle of connecting, where it is either
    swallowed as an unretrieved exception of the reconnect task, or reported to
    on_error and retried forever - even though it can never start working.
    """

    address = "ws://localhost:8000/connection/websocket"

    async def test_invalid_proxy_url_raises(self):
        for proxy in (
            "bogus://localhost:3128",  # Unsupported scheme.
            "http://localhost:3128/path",  # Meaningless path.
            "http://user@localhost:3128",  # Username without password.
        ):
            with self.subTest(proxy=proxy), self.assertRaises(ValueError):  # noqa: PT027
                Client(self.address, proxy=proxy)

    async def test_non_string_proxy_raises(self):
        # False is a natural way to spell "no proxy" - websockets would only
        # reject it while connecting, with a message about an empty scheme.
        with self.assertRaises(ValueError):  # noqa: PT027
            Client(self.address, proxy=False)

    async def test_socks_proxy_without_python_socks_raises(self):
        with mock.patch("centrifuge.client._python_socks_installed", return_value=False):
            with self.assertRaises(ValueError):  # noqa: PT027
                Client(self.address, proxy="socks5://localhost:1080")
            # An HTTP proxy does not need python-socks.
            Client(self.address, proxy="http://localhost:3128")

    async def test_socks_proxy_accepted_with_python_socks(self):
        with mock.patch("centrifuge.client._python_socks_installed", return_value=True):
            Client(self.address, proxy="socks5://localhost:1080")

    async def test_proxy_url_parser_is_found(self):
        # websockets moved parse_proxy from websockets.uri to websockets.proxy in
        # 16.0. If it moves again, validation below silently turns into a no-op -
        # the other tests here would catch that, but only indirectly.
        self.assertIsNotNone(centrifuge.client._load_parse_proxy())

    async def test_validation_error_does_not_leak_credentials(self):
        # Constructor errors end up in logs and error trackers, so the proxy URL
        # must not be echoed back with its credentials in place.
        with self.assertRaises(ValueError) as raised:  # noqa: PT027
            Client(self.address, proxy="bogus://user:s3cret@localhost:3128")
        message = str(raised.exception)
        self.assertNotIn("s3cret", message)
        self.assertNotIn("user", message)
        self.assertIn("***@localhost:3128", message)


@unittest.skipIf(
    centrifuge.client._python_socks_installed(),
    "test needs python-socks to be absent to trigger the ImportError",
)
class TestSocksProxyFromEnvironment(unittest.IsolatedAsyncioTestCase):
    async def test_missing_python_socks_reported_as_error(self):
        # A proxy taken from the environment can not be validated in the
        # constructor: websockets raises ImportError for it while connecting,
        # which is neither OSError nor WebSocketException - so without explicit
        # handling it would escape _create_connection instead of reaching
        # on_error, leaving the client stuck in the connecting state.
        error = asyncio.Future()

        async def on_error(ctx):
            if not error.done():
                error.set_result(ctx)

        env = {"ws_proxy": "socks5://127.0.0.1:1080", "no_proxy": ""}
        with mock.patch.dict(os.environ, env, clear=False):
            client = Client(
                "ws://localhost:8000/connection/websocket",
                use_protobuf=True,
                # Keep the retry far away so a single attempt is observed.
                min_reconnect_delay=30,
                max_reconnect_delay=30,
            )
            client.events.on_error = on_error
            await client.connect()
            ctx = await asyncio.wait_for(error, timeout=5)
            self.assertIsInstance(ctx.error, ImportError)
            self.assertEqual(client.state, ClientState.CONNECTING)
            await client.disconnect()
