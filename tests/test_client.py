# Configure logging.
import asyncio
import json
import logging
import unittest
import uuid

from centrifuge import Client, ClientState, SubscriptionState, PublicationContext

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


class TestSubscription(unittest.IsolatedAsyncioTestCase):
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


class TestSubscriptionOperations(unittest.IsolatedAsyncioTestCase):
    async def test_subscription_operations(self) -> None:
        for use_protobuf in (False, True):
            with self.subTest(use_protobuf=use_protobuf):
                client = Client(
                    "ws://localhost:8000/connection/websocket",
                    use_protobuf=use_protobuf,
                )
                sub = client.new_subscription("channel" + uuid.uuid4().hex)
                await client.connect()
                await sub.subscribe()
                payload = {"input": "test"}
                if use_protobuf:
                    payload = json.dumps(payload).encode()
                await sub.publish(data=payload)
                result = await sub.history(limit=-1)
                self.assertTrue(result.offset > 0)
                self.assertTrue(result.epoch)
                self.assertTrue(len(result.publications) == 1)
                result = await sub.presence()
                self.assertTrue(len(result.clients) == 1)
                result = await sub.presence_stats()
                self.assertTrue(result.num_clients == 1)
                self.assertTrue(result.num_users == 1)
                await client.disconnect()


class TestPubSub(unittest.IsolatedAsyncioTestCase):
    async def test_pub_sub(self) -> None:
        for use_protobuf in (False, True):
            with self.subTest(use_protobuf=use_protobuf):
                await self._test_pub_sub(use_protobuf=use_protobuf)

    async def _test_pub_sub(self, use_protobuf=False) -> None:
        client = Client(
            "ws://localhost:8000/connection/websocket",
            use_protobuf=use_protobuf,
        )

        future = asyncio.Future()

        async def on_publication(ctx: PublicationContext) -> None:
            future.set_result(ctx.pub.data)

        sub = client.new_subscription("pub_sub_channel" + uuid.uuid4().hex)
        sub.events.on_publication = on_publication

        await client.connect()
        await sub.subscribe()
        payload = {"input": "test"}
        if use_protobuf:
            payload = json.dumps(payload).encode()
        await sub.publish(data=payload)
        result = await future
        self.assertEqual(result, payload)
        await client.disconnect()
