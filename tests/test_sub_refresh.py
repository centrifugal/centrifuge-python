import asyncio
import unittest

from centrifuge import Client
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


if __name__ == "__main__":
    unittest.main()
