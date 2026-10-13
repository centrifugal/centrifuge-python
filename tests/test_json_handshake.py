import asyncio
import unittest

from centrifuge import Client, ClientState, PublicationContext
from tests.fake_server import FakeCentrifugoServer

# The JSON transport offers no subprotocol, so it must not send a
# Sec-WebSocket-Protocol header: RFC 6455 allows the header only with at least
# one subprotocol in it, and servers such as the websockets one reject it empty.


class TestJsonHandshake(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.server = FakeCentrifugoServer(use_protobuf=False)
        await self.server.start()
        self.client = Client(self.server.url, min_reconnect_delay=3600, max_reconnect_delay=3600)
        self.addAsyncCleanup(self.server.stop)
        self.addAsyncCleanup(self.client.disconnect)

    async def test_json_client_connects_without_a_subprotocol_header(self):
        await self.client.connect()

        self.assertEqual(self.client.state, ClientState.CONNECTED)
        self.assertEqual(len(self.server.request_headers), 1)
        self.assertEqual(self.server.request_headers[0].get_all("Sec-WebSocket-Protocol"), [])

    async def test_json_client_receives_publication_data_as_json(self):
        publications = asyncio.Queue()

        async def on_publication(ctx: PublicationContext) -> None:
            await publications.put(ctx.pub.data)

        sub = self.client.new_subscription("news")
        sub.events.on_publication = on_publication
        await self.client.connect()
        await sub.subscribe()
        await sub.ready()

        await self.server.publish(b'{"x": 1}', channel="news")

        self.assertEqual(await asyncio.wait_for(publications.get(), timeout=1), {"x": 1})


if __name__ == "__main__":
    unittest.main()
