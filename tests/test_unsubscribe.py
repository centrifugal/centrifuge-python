import asyncio
import unittest

import centrifuge.protocol.client_pb2 as protocol
from centrifuge import Client
from centrifuge.client import SubscriptionState
from tests.fake_server import FakeCentrifugoServer

# Subscription.unsubscribe() must work whatever the connection state is. The
# unsubscribe command is only meaningful over an established connection -
# server-side subscriptions do not outlive it - so it is not sent otherwise, and
# losing the connection before its reply comes is not an error either.


def _sent_unsubscribe(server):
    return any(cmd.HasField("unsubscribe") for cmd in server.received)


class TestUnsubscribeNotConnected(unittest.IsolatedAsyncioTestCase):
    async def test_unsubscribe_before_connect(self):
        client = Client("ws://localhost:0/connection/websocket", use_protobuf=True)
        sub = client.new_subscription("ch")
        await sub.subscribe()

        await sub.unsubscribe()  # used to raise "connection is not initialized"

        self.assertEqual(sub.state, SubscriptionState.UNSUBSCRIBED)


class TestUnsubscribeWire(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.server = FakeCentrifugoServer()
        await self.server.start()
        self.client = Client(self.server.url, use_protobuf=True, min_reconnect_delay=0.05)
        self.sub = self.client.new_subscription("ch")
        await self.client.connect()
        await self.sub.subscribe()
        await self.sub.ready()

    async def asyncTearDown(self):
        await self.client.disconnect()
        await self.server.stop()

    async def test_unsubscribe_after_disconnect_does_not_wait_for_timeout(self):
        await self.client.disconnect()
        self.assertEqual(self.sub.state, SubscriptionState.SUBSCRIBING)

        # Used to send the command over the closed connection and then block for
        # the whole command timeout (5 seconds by default) waiting for a reply.
        await asyncio.wait_for(self.sub.unsubscribe(), timeout=1)

        self.assertEqual(self.sub.state, SubscriptionState.UNSUBSCRIBED)
        self.assertFalse(_sent_unsubscribe(self.server))

    async def test_unsubscribe_connection_lost_before_reply(self):
        close_tasks = []

        def on_command(cmd):
            if cmd.HasField("unsubscribe"):
                # Drop the connection instead of replying to the unsubscribe.
                close_tasks.append(asyncio.ensure_future(self.server.close_connection()))
                return protocol.Reply(id=0xFFFFFF)
            return None

        self.server.on_command = on_command

        # Used to raise ClientDisconnectedError.
        await asyncio.wait_for(self.sub.unsubscribe(), timeout=1)

        self.assertEqual(self.sub.state, SubscriptionState.UNSUBSCRIBED)
        self.assertTrue(_sent_unsubscribe(self.server))
        await asyncio.gather(*close_tasks)


if __name__ == "__main__":
    unittest.main()
