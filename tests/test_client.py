# Configure logging.
import logging
import unittest

from centrifuge import Client, ClientState, SubscriptionState

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(message)s",
)
cf_logger = logging.getLogger("centrifuge")
cf_logger.setLevel(logging.DEBUG)


class TestClient(unittest.IsolatedAsyncioTestCase):
    async def test_client_connects_disconnects(self) -> None:
        for use_protobuf in (False, True):
            with self.subTest(use_protobuf=use_protobuf):
                client = Client(
                    "ws://localhost:8000/connection/websocket",
                    use_protobuf=use_protobuf,
                )
                await client.connect()
                await client.ready()
                self.assertTrue(client.state == ClientState.CONNECTED)
                await client.disconnect()
                self.assertTrue(client.state == ClientState.DISCONNECTED)

    async def test_client_subscribe_unsubscribes(self) -> None:
        for use_protobuf in (False, True):
            with self.subTest(use_protobuf=use_protobuf):
                client = Client(
                    "ws://localhost:8000/connection/websocket",
                    use_protobuf=use_protobuf,
                )
                sub = client.new_subscription("channel")
                await client.connect()
                await sub.subscribe()
                await sub.ready()
                self.assertTrue(sub.state == SubscriptionState.SUBSCRIBED)
                await sub.unsubscribe()
                self.assertTrue(sub.state == SubscriptionState.UNSUBSCRIBED)
                await client.disconnect()
