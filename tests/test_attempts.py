import asyncio
import unittest

import centrifuge.protocol.client_pb2 as protocol
from centrifuge import Client, StreamPosition, UnauthorizedError
from centrifuge.client import ClientState, SubscriptionState
from tests.fake_server import FakeCentrifugoServer

# A token request, a state request, a reply or a refresh step belongs to the
# connection attempt, subscribe attempt or subscribed session it was made for:
# a result that arrives after it is over is not used.


async def wait_until(condition, timeout=3.0):
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if condition():
            return True
        await asyncio.sleep(0.01)
    return condition()


def commands(server, field):
    return [cmd for cmd in server.received if cmd.HasField(field)]


class Getter:
    """A getter whose calls the test completes by hand (or at once, for the call
    indices in auto)."""

    def __init__(self, auto=()):
        self.calls = []
        self.auto = set(auto)

    async def __call__(self, *_args):
        index = len(self.calls)
        future = asyncio.get_running_loop().create_future()
        self.calls.append(future)
        if index in self.auto:
            return f"auto-{index}"
        return await future


class TestSubscriptionAttempts(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.server = FakeCentrifugoServer()
        await self.server.start()
        self.client = Client(self.server.url, use_protobuf=True, min_reconnect_delay=0.05)
        self.errors = []

    async def asyncTearDown(self):
        await self.client.disconnect()
        await self.server.stop()

    def _new_sub(self, **kwargs):
        sub = self.client.new_subscription("ch", **kwargs)

        async def on_error(ctx):
            self.errors.append(ctx.error)

        sub.events.on_error = on_error
        return sub

    async def test_stale_unauthorized_does_not_end_newer_subscription(self):
        getter = Getter()
        sub = self._new_sub(get_token=getter)
        await self.client.connect()
        await self.client.ready()
        await sub.subscribe()
        self.assertTrue(await wait_until(lambda: len(getter.calls) == 1))

        await sub.unsubscribe()
        await sub.subscribe()
        self.assertTrue(await wait_until(lambda: len(getter.calls) == 2))
        getter.calls[1].set_result("token")
        await sub.ready()

        # The abandoned attempt's request fails late.
        getter.calls[0].set_exception(UnauthorizedError())
        await asyncio.sleep(0.2)
        self.assertEqual(sub.state, SubscriptionState.SUBSCRIBED)
        self.assertEqual(len(commands(self.server, "subscribe")), 1)

    async def test_refresh_token_of_previous_session_not_sent(self):
        self.server.on_subscribe = lambda _ch, _req: protocol.SubscribeResult(expires=True, ttl=1)
        # Call 0: the subscribe token; call 1: the refresh, kept pending across a
        # reconnect.
        getter = Getter(auto=[0])
        sub = self._new_sub(get_token=getter)
        await self.client.connect()
        await sub.subscribe()
        await sub.ready()
        self.assertTrue(await wait_until(lambda: len(getter.calls) == 2, timeout=3))

        await self.server.close_connection()
        self.assertTrue(
            await wait_until(
                lambda: (
                    len(commands(self.server, "subscribe")) == 2
                    and sub.state == SubscriptionState.SUBSCRIBED
                )
            )
        )
        getter.calls[1].set_result("stale")
        await asyncio.sleep(0.3)
        # The server would deny a refresh sent while the subscription
        # resubscribes; the new session runs its own refresh.
        self.assertEqual(commands(self.server, "sub_refresh"), [])
        self.assertEqual(sub.state, SubscriptionState.SUBSCRIBED)

    async def test_stale_get_state_does_not_rewind_position(self):
        positions = []

        async def get_state(_channel):
            future = asyncio.get_running_loop().create_future()
            positions.append(future)
            return await future

        sub = self._new_sub(get_state=get_state, recoverable=True)
        self.server.on_subscribe = lambda _ch, req: protocol.SubscribeResult(
            recoverable=True, epoch="e", offset=req.offset
        )
        await self.client.connect()
        await self.client.ready()
        await sub.subscribe()
        self.assertTrue(await wait_until(lambda: len(positions) == 1))
        await sub.unsubscribe()
        await sub.subscribe()
        self.assertTrue(await wait_until(lambda: len(positions) == 2))
        # The first request completes while the second attempt is waiting.
        positions[0].set_result(StreamPosition(offset=1, epoch="e"))
        await asyncio.sleep(0.1)
        positions[1].set_result(StreamPosition(offset=10, epoch="e"))
        await sub.ready()
        await asyncio.sleep(0.2)
        offsets = [cmd.subscribe.offset for cmd in commands(self.server, "subscribe")]
        self.assertEqual(offsets, [10])
        self.assertEqual(sub._offset, 10)

    async def test_subscribe_interrupted_by_lost_connection_not_reported(self):
        dropped = []

        def drop_first_subscribe(cmd):
            if cmd.HasField("subscribe") and not dropped:
                dropped.append(cmd)
                return True
            return False

        self.server.drop_command = drop_first_subscribe
        sub = self._new_sub()
        await self.client.connect()
        await self.client.ready()
        await sub.subscribe()
        self.assertTrue(await wait_until(lambda: dropped))

        await self.server.close_connection()
        self.assertTrue(await wait_until(lambda: sub.state == SubscriptionState.SUBSCRIBED))
        self.assertEqual(self.errors, [])
        self.assertEqual(self.client.state, ClientState.CONNECTED)

    async def test_one_subscribe_flow_per_connection(self):
        # A token error arms a resubscribe timer; the connection is lost and the
        # reconnect subscribes; the server answers slowly, so the timer fires
        # while that subscribe is in flight.
        calls = []

        async def get_token(_channel):
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError("token service down")
            return "token"

        loop = asyncio.get_running_loop()
        delayed = []

        def drop_subscribe(cmd):
            if not cmd.HasField("subscribe"):
                return False
            reply = protocol.Reply(id=cmd.id)
            reply.subscribe.SetInParent()
            delayed.append(
                loop.call_later(
                    0.4, lambda: delayed.append(loop.create_task(self.server.send_reply(reply)))
                )
            )
            return True

        sub = self._new_sub(
            get_token=get_token, min_resubscribe_delay=0.2, max_resubscribe_delay=0.25
        )
        await self.client.connect()
        await self.client.ready()
        self.server.drop_command = drop_subscribe
        await sub.subscribe()
        self.assertTrue(await wait_until(lambda: len(calls) == 1))
        await self.server.close_connection()

        self.assertTrue(await wait_until(lambda: sub.state == SubscriptionState.SUBSCRIBED))
        await asyncio.sleep(0.6)
        # One subscribe on the new connection: a second one would make the first
        # reply stale and get "already subscribed" from the server.
        self.assertEqual(len(commands(self.server, "subscribe")), 1)
        self.assertEqual(sub.state, SubscriptionState.SUBSCRIBED)
