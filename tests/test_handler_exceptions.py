"""An exception escaping an event handler stops the client, as in centrifuge-js:
it is re-raised to the event loop's exception handler, and the client
disconnects with "bad protocol" (code 2) and doesn't reconnect. The SDK's own
work completes as if the handler had returned, and a publication whose handler
raised isn't delivered: the position stays before it."""

import asyncio
import unittest

from centrifuge import Client
from centrifuge.client import ClientState, SubscriptionState
from centrifuge.protocol import client_pb2 as protocol
from tests.fake_server import FakeCentrifugoServer

_BAD_PROTOCOL = 2


async def wait_until(condition, timeout=3.0):
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if condition():
            return True
        await asyncio.sleep(0.01)
    return condition()


class AppError(Exception):
    pass


class TestHandlerExceptions(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.server = FakeCentrifugoServer()
        await self.server.start()
        self.disconnects = []
        # Where the exceptions of handlers are re-raised.
        self.reported = []
        asyncio.get_running_loop().set_exception_handler(
            lambda _loop, ctx: self.reported.append(ctx.get("exception"))
        )

    async def asyncTearDown(self):
        await self.server.stop()

    def _client(self, **kwargs):
        client = Client(self.server.url, use_protobuf=True, min_reconnect_delay=0.05, **kwargs)

        async def on_disconnected(ctx):
            self.disconnects.append((ctx.code, ctx.reason))

        client.events.on_disconnected = on_disconnected
        return client

    def _assert_stopped(self, client):
        self.assertEqual(client.state, ClientState.DISCONNECTED)
        self.assertEqual(len(self.disconnects), 1)
        code, reason = self.disconnects[0]
        self.assertEqual(code, _BAD_PROTOCOL)
        self.assertTrue(reason.startswith("exception in event handler"), reason)
        self.assertEqual([type(e) for e in self.reported], [AppError])

    async def test_raising_publication_handler_stops_and_is_recovered_again(self):
        self.server.on_subscribe = lambda _ch, req: protocol.SubscribeResult(
            recoverable=True,
            epoch="e",
            offset=req.offset if req.recover else 0,
            was_recovering=req.recover,
            recovered=req.recover,
        )
        client = self._client()
        sub = client.new_subscription("ch", recoverable=True)
        calls = []

        async def on_publication(_ctx):
            calls.append(1)
            if len(calls) == 1:
                raise AppError("handler bug")

        sub.events.on_publication = on_publication
        await client.connect()
        await sub.subscribe()
        await sub.ready()

        push = protocol.Push(channel="ch")
        push.pub.data = b"{}"
        push.pub.offset = 1
        await self.server.send_push(push)
        # It used to stop reading silently: connected, but deaf.
        self.assertTrue(await wait_until(lambda: client.state == ClientState.DISCONNECTED))
        self._assert_stopped(client)

        await client.connect()
        await sub.ready()
        # Not delivered: recovered from before it.
        last = self.server.last_subscribe()
        self.assertTrue(last.recover)
        self.assertEqual(last.offset, 0)
        await client.disconnect()

    async def test_raising_error_handler_stops_the_client(self):
        calls = []

        async def get_token():
            calls.append(1)
            if len(calls) == 1:
                raise ValueError("token service down")
            return "token"

        client = self._client(get_token=get_token)
        errors = []

        async def on_error(_ctx):
            errors.append(1)
            if len(errors) == 1:
                raise AppError("error reporting failed")

        client.events.on_error = on_error
        await client.connect()
        self.assertTrue(await wait_until(lambda: client.state == ClientState.DISCONNECTED))
        await asyncio.sleep(0.2)
        self._assert_stopped(client)
        # No retry after the stop.
        self.assertEqual(len(calls), 1)

        await client.connect()
        self.assertTrue(await wait_until(lambda: client.state == ClientState.CONNECTED))
        await client.disconnect()

    async def test_raising_connecting_handler_in_connect(self):
        client = self._client()
        calls = []

        async def on_connecting(_ctx):
            calls.append(1)
            if len(calls) == 1:
                raise AppError("handler bug")

        client.events.on_connecting = on_connecting
        # Not raised to the caller: re-raised to the loop, and the client stops.
        await client.connect()
        self._assert_stopped(client)
        await asyncio.sleep(0.1)
        self.assertFalse(any(cmd.HasField("connect") for cmd in self.server.received))

        await client.connect()
        self.assertTrue(await wait_until(lambda: client.state == ClientState.CONNECTED))
        await client.disconnect()

    async def test_raising_subscribing_handler_in_subscribe(self):
        client = self._client()
        await client.connect()
        await client.ready()
        sub = client.new_subscription("ch")
        calls = []

        async def on_subscribing(_ctx):
            calls.append(1)
            if len(calls) == 1:
                raise AppError("handler bug")

        sub.events.on_subscribing = on_subscribing
        await sub.subscribe()
        self._assert_stopped(client)
        # The subscribe completed as far as it could: it waits for the connection
        # (it used to stay subscribing for good).
        self.assertEqual(sub.state, SubscriptionState.SUBSCRIBING)

        await client.connect()
        self.assertTrue(await wait_until(lambda: sub.state == SubscriptionState.SUBSCRIBED))
        await client.disconnect()

    async def test_raising_unsubscribed_handler_in_unsubscribe(self):
        client = self._client()
        sub = client.new_subscription("ch")

        async def on_unsubscribed(_ctx):
            raise AppError("handler bug")

        sub.events.on_unsubscribed = on_unsubscribed
        await client.connect()
        await sub.subscribe()
        await sub.ready()
        await sub.unsubscribe()
        # Unsubscribed, and the client stopped: the connection, and with it the
        # subscription on the server, is gone.
        self.assertEqual(sub.state, SubscriptionState.UNSUBSCRIBED)
        self._assert_stopped(client)
