# Configure logging.
import asyncio
import json
import logging
import unittest
import uuid
import base64
import hmac
import hashlib
from typing import List

import centrifuge.client
from centrifuge import (
    Client,
    ClientState,
    DeltaType,
    SubscriptionState,
    PublicationContext,
    SubscribedContext,
    DisconnectedContext,
    UnsubscribedContext,
    JoinContext,
    LeaveContext,
    ConnectedContext,
    ServerSubscribedContext,
    ServerPublicationContext,
    ServerJoinContext,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(message)s",
)
cf_logger = logging.getLogger("centrifuge")
cf_logger.setLevel(logging.DEBUG)


def base64url_encode(data):
    return base64.urlsafe_b64encode(data).rstrip(b"=")


def generate_jwt(user, channel=""):
    """Note, in tests we generate token on client-side - this is INSECURE
    and should not be used in production. Tokens must be generated on server-side."""
    hmac_secret = "secret"  # noqa: S105 - this is just a secret used in tests.
    header = {"typ": "JWT", "alg": "HS256"}
    payload = {"sub": user}
    if channel:
        # Subscription token
        payload["channel"] = channel
    encoded_header = base64url_encode(json.dumps(header).encode("utf-8"))
    encoded_payload = base64url_encode(json.dumps(payload).encode("utf-8"))
    signature_base = encoded_header + b"." + encoded_payload
    signature = hmac.new(hmac_secret.encode("utf-8"), signature_base, hashlib.sha256).digest()
    encoded_signature = base64url_encode(signature)
    jwt_token = encoded_header + b"." + encoded_payload + b"." + encoded_signature
    return jwt_token.decode("utf-8")


async def test_get_client_token() -> str:
    return generate_jwt("42")


async def test_get_subscription_token(channel) -> str:
    return generate_jwt("42", channel)


class TestClient(unittest.IsolatedAsyncioTestCase):
    async def test_client_connects_disconnects(self) -> None:
        for use_protobuf in (False, True):
            with self.subTest(use_protobuf=use_protobuf):
                client = Client(
                    "ws://localhost:8000/connection/websocket",
                    use_protobuf=use_protobuf,
                    get_token=test_get_client_token,
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
                    get_token=test_get_client_token,
                )
                sub = client.new_subscription("channel", get_token=test_get_subscription_token)
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
                    get_token=test_get_client_token,
                )
                sub = client.new_subscription(
                    "channel" + uuid.uuid4().hex, get_token=test_get_subscription_token
                )
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
            for delta in (None, centrifuge.DeltaType.FOSSIL):
                with self.subTest(use_protobuf=use_protobuf, delta=delta):
                    await self._test_pub_sub(use_protobuf=use_protobuf, delta=delta)

    async def _test_pub_sub(self, use_protobuf=False, delta=None) -> None:
        client = Client(
            "ws://localhost:8000/connection/websocket",
            use_protobuf=use_protobuf,
            get_token=test_get_client_token,
        )

        future1 = asyncio.Future()
        future2 = asyncio.Future()
        future3 = asyncio.Future()

        async def on_publication(ctx: PublicationContext) -> None:
            if not future1.done():
                future1.set_result(ctx.pub.data)
            elif not future2.done():
                future2.set_result(ctx.pub.data)
            else:
                future3.set_result(ctx.pub.data)

        sub = client.new_subscription(
            "pub_sub_channel" + uuid.uuid4().hex,
            get_token=test_get_subscription_token,
            delta=delta,
        )
        sub.events.on_publication = on_publication

        await client.connect()
        await sub.subscribe()
        payload1 = {
            "input": "test message which is long enough for fossil "
            "delta to be applied on the server side."
        }
        if use_protobuf:
            payload1 = json.dumps(payload1).encode()

        await sub.publish(data=payload1)
        result = await future1
        self.assertEqual(result, payload1)

        # let's test fossil delta publishing the same.
        payload2 = payload1
        await sub.publish(data=payload2)
        result = await future2
        self.assertEqual(result, payload2)

        another_payload = {"input": "hello"}
        if use_protobuf:
            another_payload = json.dumps(another_payload).encode()

        await sub.publish(data=another_payload)
        result = await future3
        self.assertEqual(result, another_payload)

        await client.disconnect()


class TestJoinLeave(unittest.IsolatedAsyncioTestCase):
    async def test_join_leave(self) -> None:
        for use_protobuf in (False, True):
            with self.subTest(use_protobuf=use_protobuf):
                await self._test_join_leave(use_protobuf=use_protobuf)

    async def _test_join_leave(self, use_protobuf=False) -> None:
        client1 = Client(
            "ws://localhost:8000/connection/websocket",
            use_protobuf=use_protobuf,
            get_token=test_get_client_token,
        )

        client2 = Client(
            "ws://localhost:8000/connection/websocket",
            use_protobuf=use_protobuf,
            get_token=test_get_client_token,
        )

        join_future = asyncio.Future()
        leave_future = asyncio.Future()

        client_id = ""

        async def on_connected(ctx: ConnectedContext) -> None:
            nonlocal client_id
            client_id = ctx.client

        client1.events.on_connected = on_connected

        async def on_join(ctx: JoinContext) -> None:
            if ctx.info.client == client_id:
                # Ignore self join event.
                return
            join_future.set_result(ctx.info.client)

        async def on_leave(ctx: LeaveContext) -> None:
            leave_future.set_result(ctx.info.client)

        channel = "join_leave_channel" + uuid.uuid4().hex
        sub1 = client1.new_subscription(channel, get_token=test_get_subscription_token)
        sub2 = client2.new_subscription(channel, get_token=test_get_subscription_token)
        sub1.events.on_join = on_join
        sub1.events.on_leave = on_leave

        await client1.connect()
        await sub1.subscribe()
        await client2.connect()
        await sub2.subscribe()
        self.assertTrue(client_id)
        await join_future
        await sub2.unsubscribe()
        await leave_future
        await client1.disconnect()
        await client2.disconnect()


class TestAutoRecovery(unittest.IsolatedAsyncioTestCase):
    async def test_auto_recovery(self) -> None:
        for use_protobuf in (False, True):
            for delta in (None, DeltaType.FOSSIL):
                with self.subTest(use_protobuf=use_protobuf, delta=delta):
                    await self._test_auto_recovery(use_protobuf=use_protobuf, delta=delta)

    async def _test_auto_recovery(self, use_protobuf=False, delta=None) -> None:
        client1 = Client(
            "ws://localhost:8000/connection/websocket",
            use_protobuf=use_protobuf,
            get_token=test_get_client_token,
        )

        client2 = Client(
            "ws://localhost:8000/connection/websocket",
            use_protobuf=use_protobuf,
            get_token=test_get_client_token,
        )

        # First subscribe both clients to the same channel.
        channel = "recovery_channel" + uuid.uuid4().hex
        sub1 = client1.new_subscription(
            channel, get_token=test_get_subscription_token, delta=delta
        )
        sub2 = client2.new_subscription(
            channel, get_token=test_get_subscription_token, delta=delta
        )

        num_messages = 10

        futures: List[asyncio.Future] = [asyncio.Future() for _ in range(num_messages)]

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

        payloads = []
        for i in range(num_messages):
            if i % 2 == 0:
                payload = {"input": "I just subscribed to channel " + str(i)}
            else:
                payload = {"input": "Hi from Java " + str(i)}
            if use_protobuf:
                payload = json.dumps(payload).encode()
            payloads.append(payload)
            await sub2.publish(data=payload)

        async def on_subscribed_after_recovery(ctx: SubscribedContext) -> None:
            self.assertTrue(ctx.recovered)
            self.assertTrue(ctx.was_recovering)

        sub1.events.on_subscribed = on_subscribed_after_recovery

        # Now reconnect client1 and check that it receives all missed messages.
        await client1.connect()
        results = await asyncio.gather(*futures)
        self.assertEqual(len(results), num_messages)
        for i, result in enumerate(results):
            self.assertEqual(result, payloads[i])
        await client1.disconnect()
        await client2.disconnect()


class TestClientTokenInvalid(unittest.IsolatedAsyncioTestCase):
    async def test_client_token(self) -> None:
        for use_protobuf in (False, True):
            with self.subTest(use_protobuf=use_protobuf):
                await self._test_client_token(use_protobuf=use_protobuf)

    async def _test_client_token(self, use_protobuf=False) -> None:
        future = asyncio.Future()

        async def invalid_get_client_token() -> str:
            return "invalid_token"

        client = Client(
            "ws://localhost:8000/connection/websocket",
            use_protobuf=use_protobuf,
            get_token=invalid_get_client_token,
        )

        async def on_disconnected(ctx: DisconnectedContext) -> None:
            future.set_result(ctx.code)

        client.events.on_disconnected = on_disconnected

        await client.connect()
        res = await future
        self.assertTrue(res == 3500)
        self.assertTrue(client.state == ClientState.DISCONNECTED)
        await client.disconnect()


class TestSubscriptionTokenInvalid(unittest.IsolatedAsyncioTestCase):
    async def test_client_token(self) -> None:
        for use_protobuf in (False, True):
            with self.subTest(use_protobuf=use_protobuf):
                await self._test_subscription_token(use_protobuf=use_protobuf)

    async def _test_subscription_token(self, use_protobuf=False) -> None:
        future = asyncio.Future()

        async def invalid_get_subscription_token(channel: str) -> str:
            self.assertEqual(channel, "channel")
            return "invalid_token"

        client = Client(
            "ws://localhost:8000/connection/websocket",
            use_protobuf=use_protobuf,
            get_token=test_get_client_token,
        )

        sub = client.new_subscription("channel", get_token=invalid_get_subscription_token)

        async def on_unsubscribed(ctx: UnsubscribedContext) -> None:
            future.set_result(ctx.code)

        sub.events.on_unsubscribed = on_unsubscribed

        await client.connect()
        await sub.subscribe()
        res = await future
        self.assertTrue(res == 103, res)
        self.assertTrue(client.state == ClientState.CONNECTED)
        await client.disconnect()


class TestServerSideSubscriptions(unittest.IsolatedAsyncioTestCase):
    async def test_server_side_subs(self) -> None:
        for use_protobuf in (False, True):
            with self.subTest(use_protobuf=use_protobuf):
                await self._test_server_side_subs(use_protobuf=use_protobuf)

    async def _test_server_side_subs(self, use_protobuf=False) -> None:
        client = Client(
            "ws://localhost:8000/connection/websocket",
            use_protobuf=use_protobuf,
            get_token=test_get_client_token,
        )

        # First subscribe both clients to the same channel.
        channel = "#42"

        payload = {"input": "test"}
        if use_protobuf:
            payload = json.dumps(payload).encode()

        subscribed_future = asyncio.Future()
        join_future = asyncio.Future()
        publication_future = asyncio.Future()

        async def on_subscribed(ctx: ServerSubscribedContext) -> None:
            subscribed_future.set_result(ctx.channel)

        async def on_join(ctx: ServerJoinContext) -> None:
            self.assertTrue(ctx.info.client)
            join_future.set_result(True)

        async def on_publication(ctx: ServerPublicationContext) -> None:
            self.assertTrue(ctx.pub.data == payload)
            publication_future.set_result(True)

        client.events.on_subscribed = on_subscribed
        client.events.on_join = on_join
        client.events.on_publication = on_publication

        await client.connect()
        ch = await subscribed_future
        self.assertEqual(ch, channel)
        await join_future
        await client.publish(channel, payload)
        await publication_future
        await client.disconnect()
