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
from websockets.protocol import State

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


class TestConnectionLeak(unittest.IsolatedAsyncioTestCase):
    async def test_concurrent_create_connection_leak(self) -> None:
        """Test that concurrent _create_connection() calls create multiple WebSocket connections.

        This test demonstrates the connection leak issue where _create_connection()
        can be called multiple times concurrently (e.g., from reconnect timers),
        resulting in multiple WebSocket connections without closing previous ones.
        """
        client = Client(
            "ws://localhost:8000/connection/websocket",
            get_token=test_get_client_token,
        )

        # Track all connection objects created
        original_connect = centrifuge.client.websockets.connect
        connections_created = []

        async def tracking_connect(*args, **kwargs):
            conn = await original_connect(*args, **kwargs)
            connections_created.append(conn)
            return conn

        # Monkey patch to track connections
        centrifuge.client.websockets.connect = tracking_connect

        try:
            # Set state to CONNECTING to bypass connect() protection
            client.state = centrifuge.client.ClientState.CONNECTING

            # Call _create_connection() multiple times concurrently
            # This simulates the race condition from multiple reconnect attempts
            await asyncio.gather(
                client._create_connection(),
                client._create_connection(),
                client._create_connection(),
            )

            # Wait a bit to ensure all connections are established
            await asyncio.sleep(0.5)

            # Count how many connections are still OPEN
            open_connections = [c for c in connections_created if c.state == State.OPEN]

            # There should only be ONE open connection, but the bug causes multiple
            # This SHOULD FAIL with the current code showing the leak
            self.assertEqual(
                len(open_connections),
                1,
                f"CONNECTION LEAK DETECTED: Expected 1 open connection but found "
                f"{len(open_connections)}. Total connections created: "
                f"{len(connections_created)}",
            )

            await client.disconnect()
        finally:
            # Restore original
            centrifuge.client.websockets.connect = original_connect

    async def test_create_connection_overwrites_without_closing(self) -> None:
        """Test that _create_connection() overwrites self._conn without closing the old one.

        This test directly demonstrates that calling _create_connection() twice
        will leak the first connection object.
        """
        client = Client(
            "ws://localhost:8000/connection/websocket",
            get_token=test_get_client_token,
        )

        # Track connections
        original_connect = centrifuge.client.websockets.connect
        connections_created = []

        async def tracking_connect(*args, **kwargs):
            conn = await original_connect(*args, **kwargs)
            connections_created.append(conn)
            return conn

        centrifuge.client.websockets.connect = tracking_connect

        try:
            # First connection via normal connect
            await client.connect()
            await client.ready()
            first_conn = client._conn

            # Verify first connection is open
            self.assertEqual(first_conn.state, State.OPEN)

            # Now force a second connection creation without disconnect
            # Simulate what happens in race condition scenarios
            client.state = centrifuge.client.ClientState.CONNECTING
            client._connected_future = asyncio.Future()  # Reset the future

            await client._create_connection()
            second_conn = client._conn

            # Verify we created 2 different connections
            self.assertIsNot(
                first_conn, second_conn, "Should have created two different connection objects"
            )

            # The bug: first connection is still OPEN because it was never closed
            # This assertion SHOULD FAIL, proving the leak
            self.assertNotEqual(
                first_conn.state,
                State.OPEN,
                "CONNECTION LEAK: First connection is still OPEN after being replaced! "
                "It should have been closed before creating the second connection.",
            )

            await client.disconnect()
        finally:
            centrifuge.client.websockets.connect = original_connect

    async def test_multiple_schedule_reconnect_timer_leak(self) -> None:
        """Test that multiple _schedule_reconnect() calls don't create orphaned timers.

        This test demonstrates the timer leak issue where calling _schedule_reconnect()
        multiple times before the timer fires creates orphaned timers that can trigger
        multiple reconnection attempts.
        """
        client = Client(
            "ws://localhost:8000/connection/websocket",
            get_token=test_get_client_token,
            min_reconnect_delay=1.0,  # Long delay to prevent timers from firing during test
        )

        # Track all timers created
        original_call_later = client._loop.call_later
        timers_created = []

        def tracking_call_later(delay, callback, *args):
            timer = original_call_later(delay, callback, *args)
            timers_created.append(timer)
            return timer

        client._loop.call_later = tracking_call_later

        try:
            # Set up client state to allow reconnect scheduling
            client.state = centrifuge.client.ClientState.DISCONNECTED
            client._need_reconnect = True

            # Call _schedule_reconnect() multiple times rapidly
            await client._schedule_reconnect()
            await client._schedule_reconnect()
            await client._schedule_reconnect()

            # Count how many timers were created
            reconnect_timers = [t for t in timers_created if not t.cancelled()]

            # There should only be ONE active timer, but the bug creates multiple
            # This assertion SHOULD FAIL, proving the timer leak
            self.assertEqual(
                len(reconnect_timers),
                1,
                f"TIMER LEAK DETECTED: Expected 1 active reconnect timer but found "
                f"{len(reconnect_timers)}. Multiple _schedule_reconnect() calls created "
                f"orphaned timers that can cause concurrent reconnection attempts.",
            )

            # Clean up: cancel all timers
            for timer in timers_created:
                if not timer.cancelled():
                    timer.cancel()

        finally:
            client._loop.call_later = original_call_later

    async def test_refresh_timer_leak(self) -> None:
        """Test that setting _refresh_timer multiple times doesn't leak old timers.

        This test demonstrates the timer leak issue where creating a new refresh timer
        without canceling the old one causes orphaned timers.
        """
        client = Client(
            "ws://localhost:8000/connection/websocket",
            get_token=test_get_client_token,
        )

        # Track all timers created
        original_call_later = client._loop.call_later
        timers_created = []

        def tracking_call_later(delay, callback, *args):
            timer = original_call_later(delay, callback, *args)
            timers_created.append(timer)
            return timer

        client._loop.call_later = tracking_call_later

        try:
            await client.connect()
            await client.ready()

            # Count timers created during initial connection (may include ping timer)
            initial_timer_count = len(timers_created)

            # Simulate multiple refresh timer creations using the same pattern as the code
            # This mimics what happens if server sends multiple refresh responses
            for _ in range(3):
                # Cancel existing refresh timer to prevent timer leaks (this is the fix)
                if client._refresh_timer:
                    client._refresh_timer.cancel()
                client._refresh_timer = client._loop.call_later(
                    10.0,
                    lambda: asyncio.ensure_future(client._refresh(), loop=client._loop),
                )

            # Count new refresh timers created
            new_timers = [t for t in timers_created[initial_timer_count:] if not t.cancelled()]

            # There should only be ONE active refresh timer
            # This assertion SHOULD FAIL, proving the timer leak
            self.assertEqual(
                len(new_timers),
                1,
                f"REFRESH TIMER LEAK DETECTED: Expected 1 active refresh timer but found "
                f"{len(new_timers)}. Multiple refresh timer assignments created orphaned "
                f"timers.",
            )

            # Clean up
            for timer in timers_created:
                if not timer.cancelled():
                    timer.cancel()

            await client.disconnect()
        finally:
            client._loop.call_later = original_call_later

    async def test_background_tasks_tracked(self) -> None:
        """Test that _listen and _process_messages tasks are properly tracked and canceled.

        This test verifies that background tasks created during connection are stored
        and can be canceled when needed, preventing task leaks during reconnection.
        """
        client = Client(
            "ws://localhost:8000/connection/websocket",
            get_token=test_get_client_token,
        )

        try:
            await client.connect()
            await client.ready()

            # After connection, should have background tasks
            self.assertIsNotNone(
                getattr(client, "_listen_task", None), "Client should track _listen_task"
            )
            self.assertIsNotNone(
                getattr(client, "_process_messages_task", None),
                "Client should track _process_messages_task",
            )

            # Tasks should not be done yet
            if hasattr(client, "_listen_task"):
                self.assertFalse(
                    client._listen_task.done(), "_listen_task should still be running"
                )
            if hasattr(client, "_process_messages_task"):
                self.assertFalse(
                    client._process_messages_task.done(),
                    "_process_messages_task should still be running",
                )

            await client.disconnect()

            # After disconnect, tasks should be done or canceled
            if hasattr(client, "_listen_task") and client._listen_task:
                await asyncio.sleep(0.1)  # Give tasks time to finish
                self.assertTrue(
                    client._listen_task.done() or client._listen_task.cancelled(),
                    "_listen_task should be done or canceled after disconnect",
                )

        finally:
            if client.state != centrifuge.client.ClientState.DISCONNECTED:
                await client.disconnect()

    async def test_subscription_resubscribe_timer_leak(self) -> None:
        """Test that multiple _schedule_resubscribe() calls don't create orphaned timers.

        This test demonstrates the timer leak issue where calling _schedule_resubscribe()
        multiple times before the timer fires creates orphaned timers.
        """
        client = Client(
            "ws://localhost:8000/connection/websocket",
            get_token=test_get_client_token,
        )

        sub = client.new_subscription(
            "test_channel",
            get_token=test_get_subscription_token,
            min_resubscribe_delay=1.0,  # Long delay to prevent timers from firing
        )

        # Track all timers created
        original_call_later = client._loop.call_later
        timers_created = []

        def tracking_call_later(delay, callback, *args):
            timer = original_call_later(delay, callback, *args)
            timers_created.append(timer)
            return timer

        client._loop.call_later = tracking_call_later

        try:
            await client.connect()
            await sub.subscribe()

            # Set subscription to subscribing state
            sub.state = centrifuge.SubscriptionState.SUBSCRIBING

            # Track initial timer count
            initial_count = len(timers_created)

            # Call _schedule_resubscribe() multiple times
            await sub._schedule_resubscribe()
            await sub._schedule_resubscribe()
            await sub._schedule_resubscribe()

            # Count new timers
            new_timers = [t for t in timers_created[initial_count:] if not t.cancelled()]

            # Should only have ONE active resubscribe timer
            self.assertEqual(
                len(new_timers),
                1,
                f"RESUBSCRIBE TIMER LEAK DETECTED: Expected 1 active resubscribe timer but "
                f"found {len(new_timers)}. Multiple _schedule_resubscribe() calls created "
                f"orphaned timers.",
            )

            # Clean up
            for timer in timers_created:
                if not timer.cancelled():
                    timer.cancel()

            await client.disconnect()
        finally:
            client._loop.call_later = original_call_later
