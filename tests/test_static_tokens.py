import asyncio
import unittest

import centrifuge.protocol.client_pb2 as protocol
from centrifuge import Client, CentrifugeError
from centrifuge.client import ClientState, SubscriptionState
from tests.fake_server import FakeCentrifugoServer

# Clients and subscriptions with a static token (no get_token) keep it when the
# server invalidates the state, and stop as unauthorized when it expires;
# set_token() replaces the connection token.

_BAD_CONFIGURATION = 12
_UNAUTHORIZED = 1


async def wait_until(condition, timeout=3.0):
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if condition():
            return True
        await asyncio.sleep(0.01)
    return condition()


def tokens(server, field):
    return [getattr(cmd, field).token for cmd in server.received if cmd.HasField(field)]


def error_reply(cmd, code, message):
    reply = protocol.Reply(id=cmd.id)
    reply.error.code = code
    reply.error.message = message
    return reply


class TestStaticTokens(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.server = FakeCentrifugoServer()
        await self.server.start()
        self.events = []

    async def asyncTearDown(self):
        await self.server.stop()

    def _client(self, **kwargs):
        client = Client(self.server.url, use_protobuf=True, min_reconnect_delay=0.05, **kwargs)

        async def on_error(ctx):
            self.events.append(("error", ctx.code, ctx.error))

        async def on_disconnected(ctx):
            self.events.append(("disconnected", ctx.code))

        client.events.on_error = on_error
        client.events.on_disconnected = on_disconnected
        return client

    async def test_3014_keeps_static_connection_token(self):
        client = self._client(token="static")  # noqa: S106
        await client.connect()
        await client.ready()
        await self.server.disconnect_close(3014, "state invalidated")
        self.assertTrue(await wait_until(lambda: len(tokens(self.server, "connect")) == 2))
        await client.ready()
        self.assertEqual(tokens(self.server, "connect"), ["static", "static"])
        await client.disconnect()

    async def test_expired_static_connection_token_ends_unauthorized(self):
        expired = [True]

        def on_command(cmd):
            if cmd.HasField("connect") and expired[0]:
                return error_reply(cmd, 109, "token expired")
            return None

        self.server.on_command = on_command
        client = self._client(token="static")  # noqa: S106
        await client.connect()
        self.assertTrue(await wait_until(lambda: client.state == ClientState.DISCONNECTED))
        self.assertIn(("disconnected", _UNAUTHORIZED), self.events)
        self.assertTrue(
            any(e[0] == "error" and e[1] == _BAD_CONFIGURATION for e in self.events),
            self.events,
        )
        self.assertEqual(tokens(self.server, "connect"), ["static"])

        # set_token() + connect() continues with the new token.
        expired[0] = False
        client.set_token("new")
        await client.connect()
        await client.ready()
        self.assertEqual(tokens(self.server, "connect"), ["static", "new"])
        await client.disconnect()

    async def test_set_token_empty_makes_next_connect_use_getter(self):
        calls = []

        async def get_token():
            calls.append(1)
            return f"user-{len(calls)}"

        client = self._client(get_token=get_token)
        await client.connect()
        await client.ready()
        await client.disconnect()

        # Switch users: reset the cached token, the next connect gets a new one.
        client.set_token("")
        await client.connect()
        await client.ready()
        self.assertEqual(tokens(self.server, "connect"), ["user-1", "user-2"])
        await client.disconnect()

    async def test_expired_static_subscription_token_ends_unauthorized(self):
        self.server.on_command = lambda cmd: (
            error_reply(cmd, 109, "token expired") if cmd.HasField("subscribe") else None
        )
        client = self._client()
        sub = client.new_subscription("ch", token="sub-static")  # noqa: S106
        sub_events = []

        async def on_error(ctx):
            sub_events.append(("error", ctx.code, ctx.error))

        async def on_unsubscribed(ctx):
            sub_events.append(("unsubscribed", ctx.code))

        sub.events.on_error = on_error
        sub.events.on_unsubscribed = on_unsubscribed
        await client.connect()
        await sub.subscribe()
        self.assertTrue(await wait_until(lambda: sub.state == SubscriptionState.UNSUBSCRIBED))
        self.assertIn(("unsubscribed", _UNAUTHORIZED), sub_events)
        self.assertTrue(
            any(
                e[0] == "error"
                and e[1] == _BAD_CONFIGURATION
                and isinstance(e[2], CentrifugeError)
                for e in sub_events
            ),
            sub_events,
        )
        await asyncio.sleep(0.2)
        self.assertEqual(tokens(self.server, "subscribe"), ["sub-static"])
        await client.disconnect()


class TestEmptyTokens(unittest.IsolatedAsyncioTestCase):
    """An empty token from get_token means unauthorized, except at connect,
    where it is a valid (anonymous) token - as in centrifuge-js."""

    async def asyncSetUp(self):
        self.server = FakeCentrifugoServer()
        await self.server.start()

    async def asyncTearDown(self):
        await self.server.stop()

    async def test_empty_subscription_token_unsubscribes(self):
        async def get_token(_channel):
            return ""

        client = Client(self.server.url, use_protobuf=True)
        sub = client.new_subscription("ch", get_token=get_token)
        codes = []

        async def on_unsubscribed(ctx):
            codes.append(ctx.code)

        sub.events.on_unsubscribed = on_unsubscribed
        await client.connect()
        await sub.subscribe()
        self.assertTrue(await wait_until(lambda: sub.state == SubscriptionState.UNSUBSCRIBED))
        self.assertEqual(codes, [_UNAUTHORIZED])
        self.assertEqual(tokens(self.server, "subscribe"), [])
        await client.disconnect()

    async def test_empty_refresh_token_disconnects(self):
        self.server.connect_result.expires = True
        self.server.connect_result.ttl = 1
        calls = []

        async def get_token():
            calls.append(1)
            return "token" if len(calls) == 1 else ""

        client = Client(self.server.url, use_protobuf=True, get_token=get_token)
        codes = []

        async def on_disconnected(ctx):
            codes.append(ctx.code)

        client.events.on_disconnected = on_disconnected
        await client.connect()
        self.assertTrue(
            await wait_until(lambda: client.state == ClientState.DISCONNECTED, timeout=4)
        )
        self.assertEqual(codes, [_UNAUTHORIZED])
        self.assertEqual(tokens(self.server, "refresh"), [])
