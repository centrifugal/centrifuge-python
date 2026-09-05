import asyncio
import unittest
from unittest import mock

import centrifuge.client as client_module
from centrifuge import Client
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


if __name__ == "__main__":
    unittest.main()
