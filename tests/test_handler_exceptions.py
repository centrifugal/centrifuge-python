import asyncio
import unittest

from centrifuge import Client
from centrifuge.client import ClientState, SubscriptionState
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

    async def asyncTearDown(self):
        await self.server.stop()

    def _client(self, **kwargs):
        client = Client(self.server.url, use_protobuf=True, min_reconnect_delay=0.05, **kwargs)

        async def on_disconnected(ctx):
            self.disconnects.append(ctx.code)

        client.events.on_disconnected = on_disconnected
        return client

    async def test_raising_publication_handler_disconnects_visibly(self):
        client = self._client()
        sub = client.new_subscription("ch")

        async def on_publication(_ctx):
            raise AppError("handler bug")

        sub.events.on_publication = on_publication
        await client.connect()
        await sub.subscribe()
        await sub.ready()

        with self.assertLogs("centrifuge", level="ERROR"):
            await self.server.publish(b"1", channel="ch")
            # It used to stop reading silently: connected, but deaf.
            self.assertTrue(await wait_until(lambda: client.state == ClientState.DISCONNECTED))
        self.assertEqual(self.disconnects, [_BAD_PROTOCOL])

        sub.events.on_publication = SubscriptionHandlerOk.on_publication
        await client.connect()
        await sub.ready()
        self.assertEqual(sub.state, SubscriptionState.SUBSCRIBED)
        await client.disconnect()

    async def test_raising_error_handler_does_not_stop_retries(self):
        calls = []

        async def get_token():
            calls.append(1)
            if len(calls) == 1:
                raise ValueError("token service down")
            return "token"

        client = self._client(get_token=get_token)

        async def on_error(_ctx):
            raise AppError("error reporting failed")

        client.events.on_error = on_error
        with self.assertLogs("centrifuge", level="ERROR"):
            await client.connect()
            # It used to stay connecting with no reconnect scheduled.
            self.assertTrue(await wait_until(lambda: client.state == ClientState.CONNECTED))
        self.assertEqual(len(calls), 2)
        await client.disconnect()

    async def test_raising_connecting_handler_in_connect(self):
        client = self._client()

        async def on_connecting(_ctx):
            raise AppError("handler bug")

        client.events.on_connecting = on_connecting
        # The exception reaches the caller, but the client connects all the same
        # (it used to stay connecting for good).
        with self.assertRaises(AppError):  # noqa: PT027
            await client.connect()
        self.assertTrue(await wait_until(lambda: client.state == ClientState.CONNECTED))
        await client.disconnect()

    async def test_raising_subscribing_handler_in_subscribe(self):
        client = self._client()
        await client.connect()
        await client.ready()
        sub = client.new_subscription("ch")

        async def on_subscribing(_ctx):
            raise AppError("handler bug")

        sub.events.on_subscribing = on_subscribing
        # Raises, and subscribes all the same (it used to stay subscribing).
        with self.assertRaises(AppError):  # noqa: PT027
            await sub.subscribe()
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
        with self.assertRaises(AppError):  # noqa: PT027
            await sub.unsubscribe()
        # The server is told all the same (it would keep the subscription otherwise).
        self.assertTrue(
            await wait_until(
                lambda: any(cmd.HasField("unsubscribe") for cmd in self.server.received)
            )
        )
        await client.disconnect()


class SubscriptionHandlerOk:
    @staticmethod
    async def on_publication(_ctx):
        return None
