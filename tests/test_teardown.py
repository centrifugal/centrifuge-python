import asyncio
import logging
import sys
import time
import unittest

import centrifuge.protocol.client_pb2 as protocol
from centrifuge import Client
from centrifuge.client import ClientState, SubscriptionState
from tests.fake_server import FakeCentrifugoServer
from tests.tcp_helpers import BlackholeProxy, SilentServer


async def wait_until(condition, timeout=3.0):
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if condition():
            return True
        await asyncio.sleep(0.01)
    return condition()


def connect_commands(server):
    return [cmd for cmd in server.received if cmd.HasField("connect")]


def make_client(url, **kwargs):
    kwargs.setdefault("min_reconnect_delay", 0.05)
    kwargs.setdefault("max_reconnect_delay", 0.1)
    return Client(url, use_protobuf=True, **kwargs)


class TestDeadLink(unittest.IsolatedAsyncioTestCase):
    """A link that goes dead without closing (no data, no FIN) must not hold
    the client: the transport is closed in the background."""

    async def asyncSetUp(self):
        self.server = FakeCentrifugoServer()
        await self.server.start()
        self.proxy = BlackholeProxy(self.server.port)
        await self.proxy.start()

    async def asyncTearDown(self):
        await self.proxy.stop()
        await self.server.stop()

    async def test_reconnects_soon_after_no_ping(self):
        # The server announces pings every second but never sends one.
        self.server.connect_result.ping = 1
        client = make_client(self.proxy.url, max_server_ping_delay=0.3)
        events = []

        async def on_connected(_ctx):
            events.append(("connected", time.monotonic()))

        client.events.on_connected = on_connected
        await client.connect()
        await client.ready()
        self.proxy.blackhole()
        t0 = time.monotonic()
        # "no ping" fires 1.3 s after connecting; the reconnect follows at once
        # instead of after the 10 s the old socket's close used to take.
        self.assertTrue(await wait_until(lambda: len(events) == 2, timeout=4))
        self.assertLess(events[1][1] - t0, 3)
        await client.disconnect()

    async def test_disconnect_returns_at_once(self):
        client = make_client(self.proxy.url)
        await client.connect()
        await client.ready()
        self.proxy.blackhole()
        t0 = time.monotonic()
        await client.disconnect()
        self.assertLess(time.monotonic() - t0, 0.5)
        self.assertEqual(client.state, ClientState.DISCONNECTED)
        # The dead socket is still closed by the client (aborted after the
        # close timeout).
        self.assertTrue(await wait_until(lambda: self.proxy.open_connections == 0, timeout=3))


class TestHungHandshake(unittest.IsolatedAsyncioTestCase):
    """A server that accepts the TCP connection but never answers the upgrade:
    each attempt fails after the client timeout and the client retries;
    disconnect() closes the socket of the attempt in progress."""

    async def asyncSetUp(self):
        self.server = SilentServer()
        await self.server.start()

    async def asyncTearDown(self):
        await self.server.stop()

    async def test_handshake_limited_by_timeout(self):
        client = make_client(self.server.url, timeout=0.3)
        errors = []

        async def on_error(ctx):
            errors.append(ctx.error)

        client.events.on_error = on_error
        await client.connect()
        self.assertTrue(await wait_until(lambda: self.server.accepted >= 3, timeout=3))
        self.assertTrue(any(isinstance(e, (TimeoutError, asyncio.TimeoutError)) for e in errors))
        self.assertEqual(client.state, ClientState.CONNECTING)

        await client.disconnect()
        self.assertTrue(await wait_until(lambda: self.server.open_connections == 0, timeout=2))
        accepted = self.server.accepted
        await asyncio.sleep(0.5)
        self.assertEqual(self.server.accepted, accepted, "attempts after disconnect()")


class TestTeardownFromHandlers(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.server = FakeCentrifugoServer()
        await self.server.start()
        self.client = make_client(self.server.url)

    async def asyncTearDown(self):
        await self.client.disconnect()
        await self.server.stop()

    async def _subscribed(self, channel="ch"):
        sub = self.client.new_subscription(channel)
        await self.client.connect()
        await sub.subscribe()
        await sub.ready()
        return sub

    async def test_disconnect_from_publication_handler(self):
        sub = await self._subscribed()
        received = []

        async def on_publication(ctx):
            received.append(ctx.pub.data)
            if len(received) == 1:
                await self.client.disconnect()

        sub.events.on_publication = on_publication

        replies = []
        for i in range(3):
            reply = protocol.Reply()
            reply.push.channel = "ch"
            reply.push.pub.data = b"%d" % i
            replies.append(reply)
        await self.server.send_replies(replies)

        self.assertTrue(await wait_until(lambda: self.client.state == ClientState.DISCONNECTED))
        await asyncio.sleep(0.2)
        # The rest of the frame belongs to the connection the handler ended.
        self.assertEqual(received, [b"0"])

        # And the client connects again (connect() used to raise RuntimeError).
        await self.client.connect()
        await self.client.ready()
        await sub.ready()
        self.assertEqual(sub.state, SubscriptionState.SUBSCRIBED)

    async def test_disconnect_during_reconnecting_teardown_wins(self):
        sub = await self._subscribed()

        async def on_subscribing(_ctx):
            # Emitted by the teardown of the lost connection, which is going
            # to reconnect: the app's disconnect() must not be lost.
            await self.client.disconnect()

        sub.events.on_subscribing = on_subscribing
        await self.server.close_connection()
        self.assertTrue(await wait_until(lambda: self.client.state == ClientState.DISCONNECTED))
        await asyncio.sleep(0.3)
        self.assertEqual(self.client.state, ClientState.DISCONNECTED)
        self.assertEqual(len(connect_commands(self.server)), 1)

    async def test_handler_not_cancelled_by_lost_connection(self):
        sub = await self._subscribed()
        finished = []

        async def on_publication(ctx):
            await asyncio.sleep(0.3)
            finished.append(ctx.pub.data)

        sub.events.on_publication = on_publication
        await self.server.publish(b"x", channel="ch")
        await asyncio.sleep(0.05)
        await self.server.close_connection()
        self.assertTrue(await wait_until(lambda: finished == [b"x"]))
        self.assertTrue(await wait_until(lambda: self.client.state == ClientState.CONNECTED))


class TestAbandonedAttempt(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.server = FakeCentrifugoServer()
        await self.server.start()

    async def asyncTearDown(self):
        await self.server.stop()

    async def test_token_of_abandoned_attempt_not_used(self):
        calls = []

        async def get_token():
            future = asyncio.get_running_loop().create_future()
            calls.append(future)
            return await future

        client = make_client(self.server.url, get_token=get_token)
        connect_task = asyncio.ensure_future(client.connect())
        self.assertTrue(await wait_until(lambda: len(calls) == 1))

        # disconnect() + connect() to switch users while the first request is
        # pending; the first one completes while the second attempt is waiting.
        await client.disconnect()
        second = asyncio.ensure_future(client.connect())
        self.assertTrue(await wait_until(lambda: len(calls) == 2))
        calls[0].set_result("user-a")
        await asyncio.sleep(0.1)
        calls[1].set_result("user-b")
        await asyncio.wait_for(asyncio.gather(connect_task, second), 3)
        self.assertTrue(await wait_until(lambda: client.state == ClientState.CONNECTED))

        tokens = [cmd.connect.token for cmd in connect_commands(self.server)]
        self.assertEqual(tokens, ["user-b"])
        await client.disconnect()


class TestTeardownWithRaisingHandlers(unittest.IsolatedAsyncioTestCase):
    """A teardown completes whatever its event handlers do. A handler that raises
    stops the client (bad protocol, no reconnect): it connects again when asked."""

    async def asyncSetUp(self):
        self.server = FakeCentrifugoServer()
        await self.server.start()
        self.client = make_client(self.server.url)

    async def asyncTearDown(self):
        await self.client.disconnect()
        await self.server.stop()

    async def test_raising_connecting_handler(self):
        async def on_connecting(ctx):
            if ctx.code != 0:
                raise RuntimeError("app bug")

        self.client.events.on_connecting = on_connecting
        await self.client.connect()
        await self.client.ready()
        asyncio.get_running_loop().set_exception_handler(lambda _loop, _ctx: None)
        await self.server.close_connection()
        self.assertTrue(await wait_until(lambda: self.client.state == ClientState.DISCONNECTED))
        await asyncio.sleep(0.2)
        self.assertEqual(len(connect_commands(self.server)), 1)

        self.client.events.on_connecting = ConnectingOk.on_connecting
        await self.client.connect()
        self.assertTrue(await wait_until(lambda: self.client.state == ClientState.CONNECTED))

    async def test_raising_subscribing_handler(self):
        sub = self.client.new_subscription("ch")

        async def on_subscribing(ctx):
            if ctx.code != 0:
                raise RuntimeError("app bug")

        sub.events.on_subscribing = on_subscribing
        await self.client.connect()
        await sub.subscribe()
        await sub.ready()
        asyncio.get_running_loop().set_exception_handler(lambda _loop, _ctx: None)
        await self.server.close_connection()
        self.assertTrue(await wait_until(lambda: self.client.state == ClientState.DISCONNECTED))
        self.assertEqual(sub.state, SubscriptionState.SUBSCRIBING)

        sub.events.on_subscribing = SubscribingOk.on_subscribing
        await self.client.connect()
        self.assertTrue(await wait_until(lambda: sub.state == SubscriptionState.SUBSCRIBED))

    async def test_new_subscription_from_subscribing_handler(self):
        sub = self.client.new_subscription("ch")
        created = []

        async def on_subscribing(ctx):
            if ctx.code != 0 and not created:
                other = self.client.new_subscription("other")
                created.append(other)
                await other.subscribe()

        sub.events.on_subscribing = on_subscribing
        await self.client.connect()
        await sub.subscribe()
        await sub.ready()
        await self.server.close_connection()
        # It used to fail with "dictionary changed size during iteration" and
        # leave the client connecting.
        self.assertTrue(await wait_until(lambda: self.client.state == ClientState.CONNECTED))
        self.assertTrue(
            await wait_until(
                lambda: (
                    sub.state == SubscriptionState.SUBSCRIBED
                    and created[0].state == SubscriptionState.SUBSCRIBED
                )
            )
        )


class TestSupersededTeardown(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.server = FakeCentrifugoServer()
        await self.server.start()
        self.client = make_client(self.server.url)

    async def asyncTearDown(self):
        await self.client.disconnect()
        await self.server.stop()

    async def test_slow_teardown_handler_does_not_touch_the_next_connection(self):
        first = self.client.new_subscription("ch0")
        second = self.client.new_subscription("ch1")
        slow = []

        async def on_subscribing(ctx):
            if ctx.code != 0 and not slow:
                slow.append(1)
                await asyncio.sleep(0.2)  # the app reconnects meanwhile

        first.events.on_subscribing = on_subscribing
        await self.client.connect()
        await first.subscribe()
        await second.subscribe()
        await second.ready()

        await self.server.close_connection()
        self.assertTrue(await wait_until(lambda: slow))
        await self.client.disconnect()
        await self.client.connect()
        await asyncio.sleep(0.6)

        # The old teardown, resumed after the handler, used to move the second
        # subscription (subscribed again by then) to subscribing for good.
        self.assertEqual(self.client.state, ClientState.CONNECTED)
        self.assertEqual(first.state, SubscriptionState.SUBSCRIBED)
        self.assertEqual(second.state, SubscriptionState.SUBSCRIBED)


class TestTeardownHandlersSeeConsistentState(unittest.IsolatedAsyncioTestCase):
    """Whatever an on_subscribing handler of a teardown does, no subscription is
    left subscribed without a connection: the next connect resubscribes all."""

    async def asyncSetUp(self):
        self.server = FakeCentrifugoServer()
        await self.server.start()
        self.client = make_client(self.server.url)
        self.first = self.client.new_subscription("a")
        self.second = self.client.new_subscription("b")

    async def asyncTearDown(self):
        await self.client.disconnect()
        await self.server.stop()

    async def _subscribe_both(self):
        await self.client.connect()
        await self.first.subscribe()
        await self.second.subscribe()
        await self.first.ready()
        await self.second.ready()

    def _subscribes_on_connection(self, n):
        # Channels subscribed after the n-th connect command.
        connects = 0
        channels = []
        for cmd in self.server.received:
            if cmd.HasField("connect"):
                connects += 1
            elif cmd.HasField("subscribe") and connects == n:
                channels.append(cmd.subscribe.channel)
        return sorted(channels)

    async def test_disconnect_from_subscribing_handler(self):
        async def on_subscribing(ctx):
            if ctx.code != 0:
                await self.client.disconnect()

        second_events = []

        async def second_subscribing(ctx):
            second_events.append(ctx.code)

        self.first.events.on_subscribing = on_subscribing
        self.second.events.on_subscribing = second_subscribing
        await self._subscribe_both()
        await self.server.close_connection()
        self.assertTrue(await wait_until(lambda: self.client.state == ClientState.DISCONNECTED))
        self.assertEqual(self.second.state, SubscriptionState.SUBSCRIBING)
        # It still gets its event.
        self.assertTrue(await wait_until(lambda: second_events[-1:] == [1]))

        self.first.events.on_subscribing = SubscribingOk.on_subscribing
        await self.client.connect()
        self.assertTrue(await wait_until(lambda: self._subscribes_on_connection(2) == ["a", "b"]))

    async def test_raising_subscribing_handler_of_one_subscription(self):
        async def on_subscribing(ctx):
            if ctx.code != 0:
                raise RuntimeError("app bug")

        self.first.events.on_subscribing = on_subscribing
        await self._subscribe_both()
        asyncio.get_running_loop().set_exception_handler(lambda _loop, _ctx: None)
        await self.server.close_connection()
        # The raise stopped the client; both subscriptions wait for the next connect.
        self.assertTrue(await wait_until(lambda: self.client.state == ClientState.DISCONNECTED))
        self.assertEqual(self.first.state, SubscriptionState.SUBSCRIBING)
        self.assertEqual(self.second.state, SubscriptionState.SUBSCRIBING)

        self.first.events.on_subscribing = SubscribingOk.on_subscribing
        await self.client.connect()
        self.assertTrue(await wait_until(lambda: self._subscribes_on_connection(2) == ["a", "b"]))


class ConnectingOk:
    @staticmethod
    async def on_connecting(_ctx):
        return None


class SubscribingOk:
    @staticmethod
    async def on_subscribing(_ctx):
        return None


class TestCancelledConnect(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.server = FakeCentrifugoServer()
        await self.server.start()

    async def asyncTearDown(self):
        await self.server.stop()

    async def test_cancelled_connect_does_not_wedge_the_client(self):
        async def get_token():
            await asyncio.sleep(0.3)
            return "token"

        client = make_client(self.server.url, get_token=get_token)
        with self.assertRaises(asyncio.TimeoutError):  # noqa: PT027
            await asyncio.wait_for(client.connect(), 0.05)
        # The attempt continues in the background.
        self.assertTrue(await wait_until(lambda: client.state == ClientState.CONNECTED))
        await client.disconnect()

    @unittest.skipIf(sys.version_info < (3, 11), "needs Task.cancelling()")
    async def test_getter_raising_cancelled_error_is_retried(self):
        calls = []

        async def get_token():
            calls.append(1)
            if len(calls) == 1:
                raise asyncio.CancelledError
            return "token"

        client = make_client(self.server.url, get_token=get_token)
        await client.connect()
        self.assertTrue(await wait_until(lambda: client.state == ClientState.CONNECTED))
        self.assertEqual(len(calls), 2)
        await client.disconnect()


class TestLargeMessage(unittest.IsolatedAsyncioTestCase):
    async def test_message_over_1mib_delivered(self):
        # The suite logs the SDK at DEBUG level (see test_client.py), and the SDK
        # logs every message it receives in full: megabytes of log lines per
        # message, which CI log streaming can't keep up with.
        centrifuge_logger = logging.getLogger("centrifuge")
        previous_level = centrifuge_logger.level
        centrifuge_logger.setLevel(logging.INFO)
        self.addCleanup(centrifuge_logger.setLevel, previous_level)

        server = FakeCentrifugoServer()
        await server.start()
        client = make_client(server.url)
        sub = client.new_subscription("big")
        received = []

        async def on_publication(ctx):
            received.append(len(ctx.pub.data))

        sub.events.on_publication = on_publication
        await client.connect()
        await sub.subscribe()
        await sub.ready()

        size = 2 * 1024 * 1024
        await server.publish(b"a" * size, channel="big")
        self.assertTrue(await wait_until(lambda: received == [size]))
        self.assertEqual(len(connect_commands(server)), 1)
        await client.disconnect()
        await server.stop()
