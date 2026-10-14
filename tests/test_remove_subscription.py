import asyncio
import unittest

import centrifuge.protocol.client_pb2 as protocol
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

    async def test_removed_subscription_calls_raise_centrifuge_error(self):
        sub = self.client.new_subscription("ch")
        self.client.remove_subscription(sub)
        for call in (sub.ready(), sub.history(), sub.presence(), sub.publish(b"x")):
            with self.assertRaises(CentrifugeError):  # noqa: PT027
                await call

    async def test_history_when_removed_while_waiting(self):
        def history_reply(cmd):
            if not cmd.HasField("history"):
                return None
            reply = protocol.Reply(id=cmd.id)
            reply.history.SetInParent()
            return reply

        self.server.on_command = history_reply
        sub = self.client.new_subscription("ch")
        await sub.subscribe()
        # history() waits for the subscription; the app removes it right after
        # the subscribe completed, before history() resumes.
        history = asyncio.ensure_future(sub.history())
        tasks = []

        async def on_subscribed(_ctx):
            async def remove():
                await sub.unsubscribe()
                self.client.remove_subscription(sub)

            tasks.append(asyncio.ensure_future(remove()))

        sub.events.on_subscribed = on_subscribed
        # It used to raise AttributeError; the call goes on, as in centrifuge-js.
        result = await history
        self.assertIsNotNone(result)
