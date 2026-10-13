import asyncio
import unittest

from centrifuge import Client
from centrifuge.client import ClientState
from tests.fake_server import FakeCentrifugoServer


async def wait_until(condition, timeout=3.0):
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if condition():
            return True
        await asyncio.sleep(0.01)
    return condition()


def connect_commands(server):
    return [cmd for cmd in server.received if cmd.HasField("connect")]


class TestReconnectAfterConnect(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.server = FakeCentrifugoServer()
        await self.server.start()
        self.client = Client(
            self.server.url, use_protobuf=True, min_reconnect_delay=0.05, max_reconnect_delay=0.1
        )

    async def asyncTearDown(self):
        await self.client.disconnect()
        await self.server.stop()

    async def test_reconnects_after_disconnect_and_connect(self):
        await self.client.connect()
        await self.client.ready()
        await self.client.disconnect()
        await self.client.connect()
        await self.client.ready()

        # The connection is lost: the client reconnects by itself, as it does
        # before any disconnect().
        await self.server.close_connection()
        self.assertTrue(await wait_until(lambda: len(connect_commands(self.server)) == 3))
        self.assertTrue(await wait_until(lambda: self.client.state == ClientState.CONNECTED))

    async def test_reconnects_after_terminal_disconnect_and_connect(self):
        await self.client.connect()
        await self.client.ready()
        # A terminal server disconnect, then the app connects again.
        await self.server.disconnect_close(3501, "bad request")
        self.assertTrue(await wait_until(lambda: self.client.state == ClientState.DISCONNECTED))
        await self.client.connect()
        await self.client.ready()

        await self.server.close_connection()
        self.assertTrue(await wait_until(lambda: len(connect_commands(self.server)) == 3))
        self.assertTrue(await wait_until(lambda: self.client.state == ClientState.CONNECTED))
