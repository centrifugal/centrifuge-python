import asyncio
import unittest

from centrifuge import CentrifugeError, Client
from centrifuge.client import SubscriptionState
from tests.fake_server import FakeCentrifugoServer


async def wait_until(condition, timeout=3.0):
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if condition():
            return True
        await asyncio.sleep(0.01)
    return condition()


class TestRemoveSubscription(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.server = FakeCentrifugoServer()
        await self.server.start()
        self.client = Client(self.server.url, use_protobuf=True)
        await self.client.connect()
        await self.client.ready()

    async def asyncTearDown(self):
        await self.client.disconnect()
        await self.server.stop()

    async def test_removing_stale_subscription_keeps_newer_one(self):
        old = self.client.new_subscription("ch")
        self.client.remove_subscription(old)
        sub = self.client.new_subscription("ch")
        await sub.subscribe()
        await sub.ready()

        # Removed a second time, e.g. from a cleanup path.
        self.client.remove_subscription(old)
        self.assertIs(self.client.get_subscription("ch"), sub)
        self.assertEqual(sub.state, SubscriptionState.SUBSCRIBED)

    async def test_removed_subscription_cannot_subscribe(self):
        sub = self.client.new_subscription("ch")
        self.client.remove_subscription(sub)
        with self.assertRaises(CentrifugeError):  # noqa: PT027
            await sub.subscribe()
        self.assertEqual(sub.state, SubscriptionState.UNSUBSCRIBED)

    async def test_remove_from_unsubscribed_handler(self):
        sub = self.client.new_subscription("ch")

        async def on_unsubscribed(_ctx):
            self.client.remove_subscription(sub)

        sub.events.on_unsubscribed = on_unsubscribed
        await sub.subscribe()
        await sub.ready()

        await sub.unsubscribe()  # used to raise AttributeError
        self.assertIsNone(self.client.get_subscription("ch"))
        # The server is told: it would keep the subscription otherwise.
        self.assertTrue(
            await wait_until(
                lambda: any(cmd.HasField("unsubscribe") for cmd in self.server.received)
            )
        )
