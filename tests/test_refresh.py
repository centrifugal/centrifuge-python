import asyncio
import unittest
from unittest import mock

import centrifuge.client as client_module
from centrifuge import Client, ClientState, codes
from tests.fake_server import FakeCentrifugoServer

import centrifuge.protocol.client_pb2 as protocol


class TestConnectionRefreshRetry(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.server = FakeCentrifugoServer()
        await self.server.start()

    async def asyncTearDown(self):
        await self.server.stop()

    async def test_get_token_error_is_retried(self):
        # The SDK spec promises that a failing get_token callback is retried
        # after some jittered time. Without a retry a single transient failure
        # leaves the connection without refreshes until the server expires it.
        self.server.connect_result = protocol.ConnectResult(
            client="fake-client",
            version="0.0.0",
            ping=25,
            expires=True,
            ttl=1,
        )

        calls = 0
        retried = asyncio.Event()

        async def get_token():
            nonlocal calls
            calls += 1
            if calls == 1:
                return "initial-token"
            if calls >= 3:
                retried.set()
            raise RuntimeError("token service unavailable")

        client = Client(self.server.url, use_protobuf=True, get_token=get_token)

        async def on_error(_ctx):
            pass

        client.events.on_error = on_error

        with mock.patch.multiple(
            client_module,
            _REFRESH_RETRY_MIN_DELAY=0.05,
            _REFRESH_RETRY_MAX_DELAY=0.1,
        ):
            await client.connect()
            await asyncio.wait_for(retried.wait(), timeout=5)

        await client.disconnect()


class TestConnectionRefreshReplyError(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.server = FakeCentrifugoServer()
        await self.server.start()
        self.server.connect_result = protocol.ConnectResult(
            client="fake-client",
            version="0.0.0",
            ping=25,
            expires=True,
            ttl=1,
        )
        self.refresh_commands = 0

    async def asyncTearDown(self):
        await self.server.stop()

    def _reply_to_refresh_with(self, error):
        """Answer every refresh command with the given error, counting them."""

        def on_command(cmd):
            if not cmd.HasField("refresh"):
                return None
            self.refresh_commands += 1
            return protocol.Reply(id=cmd.id, error=error)

        self.server.on_command = on_command

    def _client(self):
        async def get_token():
            return "some-token"

        return Client(self.server.url, use_protobuf=True, get_token=get_token)

    async def test_temporary_error_is_retried(self):
        # A temporary server error means the refresh may succeed later, so it is
        # reported and re-attempted. Without a retry the connection would be left
        # with no refresh scheduled at all until the server expires it.
        self._reply_to_refresh_with(
            protocol.Error(code=109, message="token expired", temporary=True),
        )

        client = self._client()

        retried = asyncio.Event()
        errors = []

        async def on_error(ctx):
            errors.append(ctx)
            if self.refresh_commands >= 2:
                retried.set()

        client.events.on_error = on_error

        with mock.patch.multiple(
            client_module,
            _REFRESH_RETRY_MIN_DELAY=0.05,
            _REFRESH_RETRY_MAX_DELAY=0.1,
        ):
            await client.connect()
            await asyncio.wait_for(retried.wait(), timeout=5)

        self.assertEqual(errors[0].code, codes._ErrorCode.CLIENT_REFRESH_TOKEN.value)
        self.assertEqual(errors[0].error.code, 109)
        self.assertEqual(client.state, ClientState.CONNECTED)

        await client.disconnect()

    async def test_terminal_error_disconnects(self):
        # A non-temporary server error can not be fixed by retrying: the client
        # must go to the disconnected state with the code the server returned.
        self._reply_to_refresh_with(
            protocol.Error(code=103, message="permission denied", temporary=False),
        )

        client = self._client()

        disconnected = asyncio.Future()

        async def on_disconnected(ctx):
            if not disconnected.done():
                disconnected.set_result(ctx)

        client.events.on_disconnected = on_disconnected

        await client.connect()
        ctx = await asyncio.wait_for(disconnected, timeout=5)

        self.assertEqual(ctx.code, 103)
        self.assertEqual(ctx.reason, "permission denied")
        self.assertEqual(client.state, ClientState.DISCONNECTED)
        self.assertEqual(self.refresh_commands, 1)

        await client.disconnect()


if __name__ == "__main__":
    unittest.main()
