import asyncio
import unittest

import centrifuge.protocol.client_pb2 as protocol
from centrifuge import Client
from centrifuge.client import ClientState, SubscriptionState
from tests.fake_server import FakeCentrifugoServer

# Pushes (publications, joins, leaves, unsubscribes) are applied to a client-side
# subscription only while it is subscribed. Otherwise they belong to a
# server-side subscription of the channel, if the connection has one, or are
# dropped.


async def wait_until(condition, timeout=3.0):
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if condition():
            return True
        await asyncio.sleep(0.01)
    return condition()


def commands(server, field):
    return [cmd for cmd in server.received if cmd.HasField(field)]


class TestPushRouting(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.server = FakeCentrifugoServer()
        await self.server.start()
        self.client = Client(self.server.url, use_protobuf=True, min_reconnect_delay=0.05)
        self.publications = []

    async def asyncTearDown(self):
        await self.client.disconnect()
        await self.server.stop()

    def _new_sub(self, channel="ch"):
        sub = self.client.new_subscription(channel)

        async def on_publication(ctx):
            self.publications.append(ctx.pub.data)

        sub.events.on_publication = on_publication
        return sub

    async def test_publication_after_unsubscribe_not_delivered(self):
        sub = self._new_sub()
        await self.client.connect()
        await sub.subscribe()
        await sub.ready()
        await self.server.publish(b"1", channel="ch")
        self.assertTrue(await wait_until(lambda: self.publications == [b"1"]))
        offset = sub._offset

        await sub.unsubscribe()
        # Sent by the server before it processed the unsubscribe command.
        push = protocol.Push(channel="ch")
        push.pub.data = b"2"
        push.pub.offset = 5
        await self.server.send_push(push)
        await asyncio.sleep(0.2)
        self.assertEqual(self.publications, [b"1"])
        self.assertEqual(sub._offset, offset)

    async def test_publication_while_subscribing_not_delivered(self):
        self.server.drop_command = lambda cmd: cmd.HasField("subscribe")
        sub = self._new_sub()
        await self.client.connect()
        await self.client.ready()
        await sub.subscribe()
        self.assertTrue(await wait_until(lambda: commands(self.server, "subscribe")))

        await self.server.publish(b"1", channel="ch")
        await asyncio.sleep(0.2)
        self.assertEqual(self.publications, [])
        self.assertEqual(sub.state, SubscriptionState.SUBSCRIBING)

    async def test_unsubscribe_push_while_subscribing_ignored(self):
        self.server.drop_command = lambda cmd: cmd.HasField("subscribe")
        sub = self._new_sub()
        await self.client.connect()
        await self.client.ready()
        await sub.subscribe()
        self.assertTrue(await wait_until(lambda: commands(self.server, "subscribe")))

        # For the previous subscription: it doesn't end the subscribe in progress.
        await self.server.unsubscribe("ch", 2000, "server unsubscribe")
        await asyncio.sleep(0.2)
        self.assertEqual(sub.state, SubscriptionState.SUBSCRIBING)

    async def test_resubscribe_push_after_unsubscribe_ignored(self):
        sub = self._new_sub()
        await self.client.connect()
        await sub.subscribe()
        await sub.ready()
        await sub.unsubscribe()

        # A "resubscribe" unsubscribe push must not subscribe what the app
        # unsubscribed.
        await self.server.unsubscribe("ch", 2500, "resubscribe")
        await asyncio.sleep(0.2)
        self.assertEqual(sub.state, SubscriptionState.UNSUBSCRIBED)
        self.assertEqual(len(commands(self.server, "subscribe")), 1)

    async def test_server_sub_publications_not_taken_by_unsubscribed_client_sub(self):
        self.server.connect_result.subs["ch"].CopyFrom(protocol.SubscribeResult())
        server_publications = []

        async def on_server_publication(ctx):
            server_publications.append(ctx.pub.data)

        self.client.events.on_publication = on_server_publication
        self._new_sub()  # created, never subscribed
        await self.client.connect()
        await self.client.ready()

        await self.server.publish(b"1", channel="ch")
        self.assertTrue(await wait_until(lambda: server_publications == [b"1"]))
        self.assertEqual(self.publications, [])

    async def test_recovered_publications_not_delivered_after_disconnect_from_handler(self):
        def on_subscribe(_channel, _req):
            result = protocol.SubscribeResult(recoverable=True, epoch="e", offset=0)
            for i in range(1, 4):
                result.publications.add(data=b"%d" % i, offset=i)
            return result

        self.server.on_subscribe = on_subscribe
        sub = self._new_sub()

        async def on_subscribed(_ctx):
            await self.client.disconnect()

        sub.events.on_subscribed = on_subscribed
        await self.client.connect()
        await sub.subscribe()
        self.assertTrue(await wait_until(lambda: self.client.state == ClientState.DISCONNECTED))
        await asyncio.sleep(0.2)
        self.assertEqual(self.publications, [])

    async def test_server_side_subscription_added_while_connected(self):
        subscribed = []
        server_publications = []

        async def on_subscribed(ctx):
            subscribed.append(ctx.channel)

        async def on_server_publication(ctx):
            server_publications.append((ctx.channel, ctx.pub.data))

        self.client.events.on_subscribed = on_subscribed
        self.client.events.on_publication = on_server_publication
        await self.client.connect()
        await self.client.ready()

        # The server subscribes the connection to a channel (server API).
        push = protocol.Push(channel="dyn")
        push.subscribe.recoverable = True
        push.subscribe.epoch = "e"
        await self.server.send_push(push)
        await self.server.publish(b"1", channel="dyn")

        self.assertTrue(await wait_until(lambda: server_publications == [("dyn", b"1")]))
        self.assertEqual(subscribed, ["dyn"])
