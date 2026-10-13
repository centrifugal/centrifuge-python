import asyncio
import unittest

import centrifuge.protocol.client_pb2 as protocol
from centrifuge import Client
from centrifuge.client import SubscriptionState
from tests.fake_server import FakeCentrifugoServer

# A subscription's stream position moves only within the subscribed session a
# publication belongs to: event handlers aren't cancelled, and one that
# finishes after its session ended must not move the position back.


async def wait_until(condition, timeout=3.0):
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if condition():
            return True
        await asyncio.sleep(0.01)
    return condition()


def subscribe_commands(server):
    return [cmd.subscribe for cmd in server.received if cmd.HasField("subscribe")]


async def publish(server, offset):
    push = protocol.Push(channel="ch")
    push.pub.data = b"%d" % offset
    push.pub.offset = offset
    await server.send_push(push)


class TestPosition(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.server = FakeCentrifugoServer()
        # Recover from whatever position the client sends.
        self.server.on_subscribe = lambda _ch, req: protocol.SubscribeResult(
            recoverable=True, epoch="e", offset=req.offset or 4
        )
        await self.server.start()
        self.client = Client(self.server.url, use_protobuf=True, min_reconnect_delay=0.05)
        self.sub = self.client.new_subscription("ch", recoverable=True)

    async def asyncTearDown(self):
        await self.client.disconnect()
        await self.server.stop()

    async def test_old_handler_does_not_rewind_position(self):
        gate = asyncio.Event()
        handled = []

        async def on_publication(ctx):
            if ctx.pub.offset == 5:
                await gate.wait()  # still running when the connection is lost
            handled.append(ctx.pub.offset)

        self.sub.events.on_publication = on_publication
        await self.client.connect()
        await self.sub.subscribe()
        await self.sub.ready()

        await publish(self.server, 5)
        await asyncio.sleep(0.1)
        await self.server.close_connection()
        self.assertTrue(await wait_until(lambda: len(subscribe_commands(self.server)) == 2))
        await self.sub.ready()
        await publish(self.server, 6)
        await publish(self.server, 7)
        self.assertTrue(await wait_until(lambda: handled == [6, 7]))

        gate.set()  # the old connection's handler finishes now
        self.assertTrue(await wait_until(lambda: handled == [6, 7, 5]))
        self.assertEqual(self.sub._offset, 7)

    async def test_resubscribe_from_handler_recovers_after_handled_publication(self):
        async def on_publication(ctx):
            if ctx.pub.offset == 5:
                await self.sub.unsubscribe()
                await self.sub.subscribe()
                await asyncio.sleep(0.2)

        self.sub.events.on_publication = on_publication
        await self.client.connect()
        await self.sub.subscribe()
        await self.sub.ready()

        await publish(self.server, 5)
        self.assertTrue(await wait_until(lambda: len(subscribe_commands(self.server)) == 2))
        # Publication 5 was delivered: the new subscription recovers after it.
        self.assertEqual(subscribe_commands(self.server)[1].offset, 5)
        self.assertTrue(await wait_until(lambda: self.sub.state == SubscriptionState.SUBSCRIBED))


class TestServerSidePosition(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.server = FakeCentrifugoServer()
        self.server.connect_result.subs["ss"].CopyFrom(
            protocol.SubscribeResult(recoverable=True, epoch="e", offset=4)
        )
        await self.server.start()
        self.client = Client(self.server.url, use_protobuf=True, min_reconnect_delay=0.05)

    async def asyncTearDown(self):
        await self.client.disconnect()
        await self.server.stop()

    def _recovery_offsets(self):
        return [
            cmd.connect.subs["ss"].offset
            for cmd in self.server.received
            if cmd.HasField("connect") and "ss" in cmd.connect.subs
        ]

    async def test_publication_handled_across_reconnect_counts_as_delivered(self):
        gate = asyncio.Event()

        async def on_publication(_ctx):
            await gate.wait()  # still running when the connection is lost

        self.client.events.on_publication = on_publication
        await self.client.connect()
        await self.client.ready()
        push = protocol.Push(channel="ss")
        push.pub.data = b"5"
        push.pub.offset = 5
        await self.server.send_push(push)
        await asyncio.sleep(0.1)

        await self.server.close_connection()
        self.assertTrue(await wait_until(self._recovery_offsets))
        # Publication 5 was delivered to the app: recover after it.
        self.assertEqual(self._recovery_offsets(), [5])
        gate.set()
