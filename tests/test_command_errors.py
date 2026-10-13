import asyncio
import unittest

import centrifuge.protocol.client_pb2 as protocol
from centrifuge import Client
from centrifuge.client import ClientState, SubscriptionState
from tests.fake_server import FakeCentrifugoServer


async def wait_until(condition, timeout=3.0):
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if condition():
            return True
        await asyncio.sleep(0.01)
    return condition()


def commands(server, field):
    return [cmd for cmd in server.received if cmd.HasField(field)]


class TestCommandErrors(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.server = FakeCentrifugoServer()
        await self.server.start()
        self.client = Client(
            self.server.url, use_protobuf=True, timeout=0.3, min_reconnect_delay=0.05
        )

    async def asyncTearDown(self):
        await self.client.disconnect()
        await self.server.stop()

    async def test_subscribe_timeout_reconnects(self):
        dropped = []

        def drop_first_subscribe(cmd):
            if cmd.HasField("subscribe") and not dropped:
                dropped.append(cmd)
                return True
            return False

        self.server.drop_command = drop_first_subscribe
        sub = self.client.new_subscription("ch")
        await self.client.connect()
        await sub.subscribe()

        # The retry goes over a new connection (on the same one the server could
        # answer "already subscribed").
        self.assertTrue(await wait_until(lambda: sub.state == SubscriptionState.SUBSCRIBED))
        self.assertEqual(len(commands(self.server, "connect")), 2)
        self.assertEqual(len(commands(self.server, "subscribe")), 2)

    async def test_unsubscribe_error_reply_reconnects(self):
        def on_command(cmd):
            if cmd.HasField("unsubscribe"):
                reply = protocol.Reply(id=cmd.id)
                reply.error.code = 100
                reply.error.message = "internal server error"
                return reply
            return None

        self.server.on_command = on_command
        sub = self.client.new_subscription("ch")
        await self.client.connect()
        await sub.subscribe()
        await sub.ready()
        await sub.unsubscribe()

        # The server may still hold the subscription: the client reconnects.
        self.assertTrue(await wait_until(lambda: len(commands(self.server, "connect")) == 2))
        self.assertTrue(await wait_until(lambda: self.client.state == ClientState.CONNECTED))

    async def test_unsubscribe_from_handler_does_not_stall(self):
        sub = self.client.new_subscription("ch")

        async def on_publication(_ctx):
            await sub.unsubscribe()

        sub.events.on_publication = on_publication
        await self.client.connect()
        await sub.subscribe()
        await sub.ready()
        await self.server.publish(b"1", channel="ch")

        self.assertTrue(await wait_until(lambda: sub.state == SubscriptionState.UNSUBSCRIBED))
        await asyncio.sleep(0.5)
        # The reply was processed: no timeout, no reconnect.
        self.assertEqual(len(commands(self.server, "connect")), 1)
