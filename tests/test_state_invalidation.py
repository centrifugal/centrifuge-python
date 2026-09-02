import asyncio
import unittest

import centrifuge.client as centrifuge_client
import centrifuge.protocol.client_pb2 as protocol
from centrifuge import Client
from tests.fake_server import FakeCentrifugoServer

# Tests for "state invalidated" handling: unsubscribe code 2502 (per-subscription)
# and disconnect code 3014 (connection-wide). On these the client drops cached
# tokens (and recovery position / delta base) so a fresh token is obtained and
# the subscription re-syncs. Exercised against the in-process FakeCentrifugoServer.

_STATE_INVALIDATED_UNSUBSCRIBE = 2502
_STATE_INVALIDATED_DISCONNECT = 3014


class TestInvalidateStateUnit(unittest.IsolatedAsyncioTestCase):
    async def test_invalidate_state_resets_cached_state(self):
        client = Client("ws://localhost:0/connection/websocket")
        sub = client.new_subscription("ch", token="sub-token")  # noqa: S106
        sub._offset = 10
        sub._epoch = "e1"
        sub._recover = True
        sub._prev_data = b"stale-delta-base"

        sub._invalidate_state()

        self.assertEqual(sub._token, "")
        self.assertEqual(sub._offset, 0)
        # Recovery position reset to the sentinel epoch so a recoverable
        # subscription resubscribes with was_recovering=True, recovered=False.
        self.assertEqual(sub._epoch, "_")
        # The recover flag is left untouched (here it stays True; for a
        # non-recoverable subscription it would stay False).
        self.assertTrue(sub._recover)
        self.assertIsNone(sub._prev_data)

    async def test_invalidate_state_resets_server_sub_recovery_position(self):
        client = Client("ws://localhost:0/connection/websocket")
        client._server_subs["ch"] = centrifuge_client._ServerSubscription(
            offset=10, epoch="e1", recoverable=True
        )

        client._invalidate_connection_state()

        server_sub = client._server_subs["ch"]
        self.assertEqual(server_sub.offset, 0)
        # Recovery position reset to the sentinel epoch, mirroring client-side
        # subscriptions, so the next connect can't recover from stale state.
        self.assertEqual(server_sub.epoch, "_")
        # The recoverable flag is left untouched.
        self.assertTrue(server_sub.recoverable)


class TestStateInvalidationWire(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.server = FakeCentrifugoServer()
        await self.server.start()

    async def asyncTearDown(self):
        await self.server.stop()

    @staticmethod
    def _subscribed_future(sub):
        fut = asyncio.Future()

        async def on_subscribed(ctx):
            if not fut.done():
                fut.set_result(ctx)

        sub.events.on_subscribed = on_subscribed
        return fut

    async def test_unsubscribe_2502_clears_sub_token_and_resubscribes(self):
        client = Client(self.server.url, use_protobuf=True, min_reconnect_delay=0.05)
        sub = client.new_subscription("ch", token="sub-token-0")  # noqa: S106
        subscribed = self._subscribed_future(sub)
        await client.connect()
        await sub.subscribe()
        await asyncio.wait_for(subscribed, timeout=5)
        self.assertEqual(self.server.last_subscribe().token, "sub-token-0")

        # Re-arm the future for the resubscribe and send "state invalidated".
        resubscribed = self._subscribed_future(sub)
        await self.server.unsubscribe("ch", _STATE_INVALIDATED_UNSUBSCRIBE, "state invalidated")
        await asyncio.wait_for(resubscribed, timeout=5)

        self.assertEqual(sub._token, "")
        self.assertEqual(self.server.last_subscribe().token, "")
        await client.disconnect()

    async def test_disconnect_3014_clears_conn_token_and_invalidates_subs(self):
        calls = []

        async def get_token():
            calls.append(1)
            return "conn-token-1"

        client = Client(
            self.server.url,
            use_protobuf=True,
            token="conn-token-0",  # noqa: S106
            get_token=get_token,
            min_reconnect_delay=0.05,
            max_reconnect_delay=0.2,
        )
        sub = client.new_subscription("ch", token="sub-token-0")  # noqa: S106
        subscribed = self._subscribed_future(sub)

        connected = asyncio.Future()

        async def on_connected(ctx):
            if not connected.done():
                connected.set_result(ctx)

        client.events.on_connected = on_connected
        await client.connect()
        await asyncio.wait_for(connected, timeout=5)
        await sub.subscribe()
        await asyncio.wait_for(subscribed, timeout=5)

        # Re-arm for the post-reconnect connected + resubscribed.
        reconnected = asyncio.Future()

        async def on_reconnected(ctx):
            if not reconnected.done():
                reconnected.set_result(ctx)

        client.events.on_connected = on_reconnected
        resubscribed = self._subscribed_future(sub)

        # Server delivers "state invalidated" as a close frame (as Centrifugo does).
        await self.server.disconnect_close(_STATE_INVALIDATED_DISCONNECT, "state invalidated")

        await asyncio.wait_for(reconnected, timeout=5)
        await asyncio.wait_for(resubscribed, timeout=5)

        self.assertGreaterEqual(len(calls), 1, "get_token must be called after 3014")
        # Reconnect used the freshly fetched connection token.
        last_connect = None
        for cmd in self.server.received:
            if cmd.HasField("connect"):
                last_connect = cmd.connect
        self.assertEqual(last_connect.token, "conn-token-1")
        # Subscription was invalidated — resubscribe carries no token.
        self.assertEqual(self.server.last_subscribe().token, "")
        await client.disconnect()

    async def test_disconnect_3014_resets_server_sub_recovery_position(self):
        # Server pushes a recoverable server-side subscription in the connect
        # reply, the way Centrifugo does for subs configured server-side.
        self.server.connect_result = protocol.ConnectResult(
            client="fake-client",
            version="0.0.0",
            ping=25,
            subs={"ch": protocol.SubscribeResult(recoverable=True, offset=5, epoch="e1")},
        )

        calls = []

        async def get_token():
            calls.append(1)
            return "conn-token-1"

        client = Client(
            self.server.url,
            use_protobuf=True,
            token="conn-token-0",  # noqa: S106
            get_token=get_token,
            min_reconnect_delay=0.05,
            max_reconnect_delay=0.2,
        )

        connected = asyncio.Future()

        async def on_connected(ctx):
            if not connected.done():
                connected.set_result(ctx)

        client.events.on_connected = on_connected
        await client.connect()
        await asyncio.wait_for(connected, timeout=5)

        server_sub = client._server_subs["ch"]
        # Offset arrives as "5" (a string): protobuf's default JSON mapping
        # renders int64 fields as strings. _process_server_subs() must cast it
        # to int, matching the type of every other stored/emitted offset.
        self.assertEqual(server_sub.offset, 5)
        self.assertEqual(server_sub.epoch, "e1")
        self.assertTrue(server_sub.recoverable)

        # Re-arm for the post-reconnect connected event.
        reconnected = asyncio.Future()

        async def on_reconnected(ctx):
            if not reconnected.done():
                reconnected.set_result(ctx)

        client.events.on_connected = on_reconnected

        # Server delivers "state invalidated" as a close frame (as Centrifugo does).
        await self.server.disconnect_close(_STATE_INVALIDATED_DISCONNECT, "state invalidated")
        await asyncio.wait_for(reconnected, timeout=5)

        self.assertGreaterEqual(len(calls), 1, "get_token must be called after 3014")

        # The reconnect's connect command must not request recovery from the
        # stale pre-invalidation position: offset/epoch reset to the sentinel,
        # while recover is still requested (recoverable left untouched).
        connect_cmds = [cmd.connect for cmd in self.server.received if cmd.HasField("connect")]
        self.assertGreaterEqual(len(connect_cmds), 2)
        last_connect = connect_cmds[-1]
        self.assertIn("ch", last_connect.subs)
        sent_sub = last_connect.subs["ch"]
        self.assertTrue(sent_sub.recover)
        self.assertEqual(sent_sub.offset, 0)
        self.assertEqual(sent_sub.epoch, "_")

        await client.disconnect()


if __name__ == "__main__":
    unittest.main()
