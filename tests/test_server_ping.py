import asyncio
import unittest

import websockets

import centrifuge.protocol.client_pb2 as protocol
from centrifuge import Client, ClientState
from tests.fake_server import FakeCentrifugoServer

# Tests for the wait for server pings: the client drops a connection that goes
# ping interval + max_server_ping_delay without a ping. A busy server may skip
# pings while it writes other messages, so any data from the server restarts
# that wait, as in centrifuge-js. The fake server never sends pings.

_NO_PING = 2


class TestServerPingWait(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.server = FakeCentrifugoServer()
        # 1 second ping interval: with a 0.5 second delay, the client waits
        # 1.5 seconds for a ping.
        self.server.connect_result = protocol.ConnectResult(
            client="fake-client", version="0.0.0", ping=1
        )
        await self.server.start()

    async def asyncTearDown(self):
        await self.server.stop()

    def _client(self):
        client = Client(self.server.url, use_protobuf=True, max_server_ping_delay=0.5)
        self.disconnects = []

        async def on_disconnected(ctx):
            self.disconnects.append((ctx.code, ctx.reason))

        client.events.on_disconnected = on_disconnected
        return client

    async def test_data_without_pings_keeps_the_connection(self):
        client = self._client()
        await client.connect()

        # 3 seconds of publications and no ping: twice the ping wait.
        for _ in range(15):
            try:
                await self.server.publish(b"{}", channel="news")
            except websockets.ConnectionClosed:
                break  # The client dropped the connection; asserted below.
            await asyncio.sleep(0.2)

        self.assertEqual(self.disconnects, [])
        self.assertEqual(client.state, ClientState.CONNECTED)
        await client.disconnect()

    async def test_silence_still_ends_the_connection(self):
        client = self._client()
        await client.connect()

        await asyncio.sleep(2.5)

        self.assertEqual(self.disconnects[:1], [(_NO_PING, "no ping")])
        await client.disconnect()
