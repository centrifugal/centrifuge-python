import asyncio
import unittest
from unittest import mock

import centrifuge.client as client_module
from centrifuge import Client, codes
from tests.fake_server import FakeCentrifugoServer

import centrifuge.protocol.client_pb2 as protocol

# Regression test for https://github.com/centrifugal/centrifuge-python/issues/50:
# the sub_refresh command must carry the "channel" field. Centrifugo requires it
# in SubRefreshRequest and closes the whole connection with 3501 "bad request"
# when it is missing.


class TestSubRefreshWire(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.server = FakeCentrifugoServer()
        await self.server.start()

    async def asyncTearDown(self):
        await self.server.stop()

    async def test_sub_refresh_includes_channel(self):
        # Subscribe reply advertises a short-lived token so the client schedules
        # a sub_refresh almost immediately.
        self.server.on_subscribe = lambda _ch, _req: protocol.SubscribeResult(expires=True, ttl=1)

        sub_refresh_cmd = asyncio.Future()

        def on_command(cmd):
            if cmd.HasField("sub_refresh") and not sub_refresh_cmd.done():
                sub_refresh_cmd.set_result(cmd.sub_refresh)
            # Returning None falls through to the server's default handling.

        self.server.on_command = on_command

        async def get_token(_channel):
            return "refreshed-sub-token"

        client = Client(self.server.url, use_protobuf=True)
        sub = client.new_subscription("restaurant:42:in", get_token=get_token)

        subscribed = asyncio.Future()

        async def on_subscribed(ctx):
            if not subscribed.done():
                subscribed.set_result(ctx)

        sub.events.on_subscribed = on_subscribed

        await client.connect()
        await sub.subscribe()
        await asyncio.wait_for(subscribed, timeout=5)

        req = await asyncio.wait_for(sub_refresh_cmd, timeout=5)
        self.assertEqual(req.channel, "restaurant:42:in")
        self.assertEqual(req.token, "refreshed-sub-token")

        await client.disconnect()


class TestSubRefreshTokenFetchError(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.server = FakeCentrifugoServer()
        await self.server.start()

    async def asyncTearDown(self):
        await self.server.stop()

    async def test_get_token_error_reports_refresh_code(self):
        # A failure to obtain a fresh token during a scheduled sub_refresh must
        # be reported with SUBSCRIPTION_REFRESH_TOKEN, matching the other error
        # paths in this same refresh flow (timeout / reply error below).
        self.server.on_subscribe = lambda _ch, _req: protocol.SubscribeResult(expires=True, ttl=1)

        calls = 0

        async def get_token(_channel):
            nonlocal calls
            calls += 1
            if calls == 1:
                return "initial-sub-token"
            raise RuntimeError("token service unavailable")

        client = Client(self.server.url, use_protobuf=True)
        sub = client.new_subscription("restaurant:42:in", get_token=get_token)

        error_ctx = asyncio.Future()

        async def on_error(ctx):
            if not error_ctx.done():
                error_ctx.set_result(ctx)

        sub.events.on_error = on_error

        await client.connect()
        await sub.subscribe()

        ctx = await asyncio.wait_for(error_ctx, timeout=5)
        self.assertEqual(ctx.code, codes._ErrorCode.SUBSCRIPTION_REFRESH_TOKEN.value)
        self.assertIsInstance(ctx.error, RuntimeError)

        await client.disconnect()


class TestSubRefreshRetry(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.server = FakeCentrifugoServer()
        await self.server.start()

    async def asyncTearDown(self):
        await self.server.stop()

    async def test_get_token_error_is_retried(self):
        # The SDK spec promises that a failing get_token callback is retried
        # after some jittered time. Without a retry a single transient failure
        # leaves the subscription without refreshes until the server drops it.
        self.server.on_subscribe = lambda _ch, _req: protocol.SubscribeResult(expires=True, ttl=1)

        calls = 0
        retried = asyncio.Event()

        async def get_token(_channel):
            nonlocal calls
            calls += 1
            if calls == 1:
                return "initial-sub-token"
            if calls >= 3:
                retried.set()
            raise RuntimeError("token service unavailable")

        client = Client(self.server.url, use_protobuf=True)
        sub = client.new_subscription("restaurant:42:in", get_token=get_token)

        async def on_error(_ctx):
            pass

        sub.events.on_error = on_error

        with mock.patch.multiple(
            client_module,
            _SUB_REFRESH_RETRY_MIN_DELAY=0.05,
            _SUB_REFRESH_RETRY_MAX_DELAY=0.1,
        ):
            await client.connect()
            await sub.subscribe()
            await asyncio.wait_for(retried.wait(), timeout=5)

        await client.disconnect()


if __name__ == "__main__":
    unittest.main()
