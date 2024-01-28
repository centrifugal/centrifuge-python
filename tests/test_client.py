import asyncio
import unittest
from centrifuge import Client, ClientState, SubscriptionState

# Configure logging.
import logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(message)s'
)
cf_logger = logging.getLogger('centrifuge')
cf_logger.setLevel(logging.DEBUG)


class TestClient(unittest.IsolatedAsyncioTestCase):

    async def test_client_connects_disconnects(self):
        client = Client('ws://localhost:8000/connection/websocket')
        await client.connect()
        await client.ready()
        self.assertTrue(client.state == ClientState.CONNECTED)
        await client.disconnect()
        self.assertTrue(client.state == ClientState.DISCONNECTED)

    async def test_client_subscribe_unsubscribes(self):
        client = Client('ws://localhost:8000/connection/websocket')
        sub = client.new_subscription('channel')
        asyncio.ensure_future(client.connect())
        await sub.subscribe()
        await sub.ready()
        self.assertTrue(sub.state == SubscriptionState.SUBSCRIBED)
        await sub.unsubscribe()
        self.assertTrue(sub.state == SubscriptionState.UNSUBSCRIBED)
        await client.disconnect()
