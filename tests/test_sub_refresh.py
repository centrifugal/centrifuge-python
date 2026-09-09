import asyncio
import unittest
from unittest import mock

import centrifuge.client as client_module
from centrifuge import Client, SubscriptionState, codes
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


class TestSubRefreshReplyError(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.server = FakeCentrifugoServer()
        await self.server.start()
        # Subscribe reply advertises a short-lived token so the client schedules
        # a sub_refresh almost immediately.
        self.server.on_subscribe = lambda _ch, _req: protocol.SubscribeResult(expires=True, ttl=1)
        self.sub_refresh_commands = 0
        self.unsubscribe_sent = asyncio.Future()

    async def asyncTearDown(self):
        await self.server.stop()

    def _reply_to_sub_refresh_with(self, error):
        """Answer every sub_refresh command with the given error, counting them."""

        def on_command(cmd):
            if cmd.HasField("unsubscribe") and not self.unsubscribe_sent.done():
                self.unsubscribe_sent.set_result(cmd.unsubscribe)
            if not cmd.HasField("sub_refresh"):
                return None
            self.sub_refresh_commands += 1
            return protocol.Reply(id=cmd.id, error=error)

        self.server.on_command = on_command

    @staticmethod
    async def _get_token(_channel):
        return "some-sub-token"

    async def test_temporary_error_is_retried(self):
        # A temporary server error means the refresh may succeed later, so it is
        # reported and re-attempted. Without a retry the subscription would be
        # left with no refresh scheduled at all until the server expires it.
        self._reply_to_sub_refresh_with(
            protocol.Error(code=109, message="token expired", temporary=True),
        )

        client = Client(self.server.url, use_protobuf=True)
        sub = client.new_subscription("restaurant:42:in", get_token=self._get_token)

        retried = asyncio.Event()
        errors = []

        async def on_error(ctx):
            errors.append(ctx)
            if self.sub_refresh_commands >= 2:
                retried.set()

        sub.events.on_error = on_error

        with mock.patch.multiple(
            client_module,
            _SUB_REFRESH_RETRY_MIN_DELAY=0.05,
            _SUB_REFRESH_RETRY_MAX_DELAY=0.1,
        ):
            await client.connect()
            await sub.subscribe()
            await asyncio.wait_for(retried.wait(), timeout=5)

        self.assertEqual(errors[0].code, codes._ErrorCode.SUBSCRIPTION_REFRESH_TOKEN.value)
        self.assertEqual(errors[0].error.code, 109)
        self.assertEqual(sub.state, SubscriptionState.SUBSCRIBED)

        await client.disconnect()

    async def test_terminal_error_unsubscribes(self):
        # A non-temporary server error can not be fixed by retrying: the
        # subscription must move to the unsubscribed state with the code the
        # server returned.
        self._reply_to_sub_refresh_with(
            protocol.Error(code=103, message="permission denied", temporary=False),
        )

        client = Client(self.server.url, use_protobuf=True)
        sub = client.new_subscription("restaurant:42:in", get_token=self._get_token)

        unsubscribed = asyncio.Future()

        async def on_unsubscribed(ctx):
            if not unsubscribed.done():
                unsubscribed.set_result(ctx)

        sub.events.on_unsubscribed = on_unsubscribed

        await client.connect()
        await sub.subscribe()

        ctx = await asyncio.wait_for(unsubscribed, timeout=5)
        self.assertEqual(ctx.code, 103)
        self.assertEqual(ctx.reason, "permission denied")
        self.assertEqual(sub.state, SubscriptionState.UNSUBSCRIBED)
        self.assertEqual(self.sub_refresh_commands, 1)

        # The server is also told about it, like on an explicit unsubscribe. The
        # hook sees the command before the server replies to it, so give the
        # reply a moment to arrive - disconnecting mid-command would cancel it.
        req = await asyncio.wait_for(self.unsubscribe_sent, timeout=5)
        self.assertEqual(req.channel, "restaurant:42:in")
        await asyncio.sleep(0.1)

        await client.disconnect()


if __name__ == "__main__":
    unittest.main()
