# Configure logging.
import asyncio
import json
import logging
import unittest
import uuid
from typing import List

from centrifuge import (
    Client,
    ClientState,
    SubscriptionState,
    PublicationContext,
    SubscribedContext,
    ClientTokenContext,
    SubscriptionTokenContext,
    DisconnectedContext,
    UnsubscribedContext,
)

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


class TestAutoRecovery(unittest.IsolatedAsyncioTestCase):
    async def test_auto_recovery(self) -> None:
        for use_protobuf in (False, True):
            with self.subTest(use_protobuf=use_protobuf):
                await self._test_auto_recovery(use_protobuf=use_protobuf)

    async def _test_auto_recovery(self, use_protobuf=False) -> None:
        client1 = Client(
            "ws://localhost:8000/connection/websocket",
            use_protobuf=use_protobuf,
        )

        client2 = Client(
            "ws://localhost:8000/connection/websocket",
            use_protobuf=use_protobuf,
        )

        # First subscribe both clients to the same channel.
        channel = "recovery_channel" + uuid.uuid4().hex
        sub1 = client1.new_subscription(channel)
        sub2 = client2.new_subscription(channel)

        futures: List[asyncio.Future] = [asyncio.Future() for _ in range(5)]

        async def on_publication(ctx: PublicationContext) -> None:
            futures[ctx.pub.offset - 1].set_result(ctx.pub.data)

        async def on_subscribed(ctx: SubscribedContext) -> None:
            self.assertFalse(ctx.recovered)
            self.assertFalse(ctx.was_recovering)

        sub1.events.on_publication = on_publication
        sub1.events.on_subscribed = on_subscribed

        await client1.connect()
        await sub1.subscribe()
        await client2.connect()
        await sub2.subscribe()

        # Now disconnect client1 and publish some messages using client2.
        await client1.disconnect()

        for _ in range(10):
            payload = {"input": "test"}
            if use_protobuf:
                payload = json.dumps(payload).encode()
            await sub2.publish(data=payload)

        async def on_subscribed_after_recovery(ctx: SubscribedContext) -> None:
            self.assertTrue(ctx.recovered)
            self.assertTrue(ctx.was_recovering)

        sub1.events.on_subscribed = on_subscribed_after_recovery

        # Now reconnect client1 and check that it receives all missed messages.
        await client1.connect()
        results = await asyncio.gather(*futures)
        self.assertEqual(len(results), 5)
        await client1.disconnect()
        await client2.disconnect()


class TestClientToken(unittest.IsolatedAsyncioTestCase):
    async def test_client_token(self) -> None:
        for use_protobuf in (False, True):
            with self.subTest(use_protobuf=use_protobuf):
                await self._test_client_token(use_protobuf=use_protobuf)

    async def _test_client_token(self, use_protobuf=False) -> None:
        future = asyncio.Future()

        async def test_get_client_token(ctx: ClientTokenContext) -> str:
            self.assertEqual(ctx, ClientTokenContext())
            return "invalid_token"

        client = Client(
            "ws://localhost:8000/connection/websocket",
            use_protobuf=use_protobuf,
            get_token=test_get_client_token,
        )

        async def on_disconnected(ctx: DisconnectedContext) -> None:
            future.set_result(ctx.code)

        client.events.on_disconnected = on_disconnected

        await client.connect()
        res = await future
        self.assertTrue(res == 3500)
        self.assertTrue(client.state == ClientState.DISCONNECTED)
        await client.disconnect()


class TestSubscriptionToken(unittest.IsolatedAsyncioTestCase):
    async def test_client_token(self) -> None:
        for use_protobuf in (False, True):
            with self.subTest(use_protobuf=use_protobuf):
                await self._test_subscription_token(use_protobuf=use_protobuf)

    async def _test_subscription_token(self, use_protobuf=False) -> None:
        future = asyncio.Future()

        async def test_get_subscription_token(ctx: SubscriptionTokenContext) -> str:
            self.assertEqual(ctx, SubscriptionTokenContext(channel="channel"))
            return "invalid_token"

        client = Client(
            "ws://localhost:8000/connection/websocket",
            use_protobuf=use_protobuf,
        )

        sub = client.new_subscription("channel", get_token=test_get_subscription_token)

        async def on_unsubscribed(ctx: UnsubscribedContext) -> None:
            future.set_result(ctx.code)

        sub.events.on_unsubscribed = on_unsubscribed

        await client.connect()
        await sub.subscribe()
        res = await future
        self.assertTrue(res == 103, res)
        self.assertTrue(client.state == ClientState.CONNECTED)
        await client.disconnect()
