import asyncio
import unittest

from centrifuge import Client
from centrifuge.client import ClientState
from centrifuge.exceptions import ClientDisconnectedError, OperationTimeoutError

# ready() (and therefore publish/history/presence/rpc, which all call it first)
# is meant to tolerate a client that is mid-reconnect: it should wait up to
# `timeout` for `_connected_future` to resolve instead of failing instantly.
# _check_state() used to raise whenever state != CONNECTED, which included
# CONNECTING - the exact state a transient reconnect is in - defeating the
# wait/timeout mechanism entirely.


class TestClientReady(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.client = Client("ws://localhost:8000/connection/websocket")

    async def test_ready_waits_while_connecting(self):
        self.client.state = ClientState.CONNECTING
        task = asyncio.ensure_future(self.client.ready(timeout=1))
        await asyncio.sleep(0)
        self.assertFalse(task.done())

        self.client.state = ClientState.CONNECTED
        self.client._connected_future.set_result(True)
        await task  # does not raise

    async def test_ready_times_out_while_connecting(self):
        self.client.state = ClientState.CONNECTING
        with self.assertRaises(OperationTimeoutError):  # noqa: PT027
            await self.client.ready(timeout=0.05)

    async def test_ready_raises_immediately_when_disconnected(self):
        self.client.state = ClientState.DISCONNECTED
        with self.assertRaises(ClientDisconnectedError):  # noqa: PT027
            await self.client.ready(timeout=1)


if __name__ == "__main__":
    unittest.main()
