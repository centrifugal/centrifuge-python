import asyncio
import base64
import contextlib
import os
import unittest
from unittest import mock

import websockets

import centrifuge.client
from centrifuge import Client, ClientState
from tests.fake_server import FakeCentrifugoServer

# Tests for the proxy option (https://github.com/centrifugal/centrifuge-python/issues/45),
# exercised against a minimal in-process HTTP proxy (CONNECT method) in front of
# the FakeCentrifugoServer.

requires_proxy_support = unittest.skipUnless(
    centrifuge.client._websockets_supports_proxy(),
    f"proxy requires websockets >= 15.0, installed {websockets.__version__}",
)


async def _pipe(reader, writer):
    try:
        while True:
            data = await reader.read(65536)
            if not data:
                break
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
            _pipe(reader, remote_writer),
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


@requires_proxy_support
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


@requires_proxy_support
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


class TestProxyVersionGuard(unittest.IsolatedAsyncioTestCase):
    async def test_proxy_requires_recent_websockets(self):
        with mock.patch("centrifuge.client._websockets_supports_proxy", return_value=False):
            with self.assertRaises(ValueError):  # noqa: PT027
                Client("ws://localhost:8000/connection/websocket", proxy="http://localhost:3128")
            # The default (proxy from environment) keeps working on old websockets.
            Client("ws://localhost:8000/connection/websocket")
