import asyncio
import unittest

from centrifuge import Client, DeltaType, Filter
from centrifuge.exceptions import CentrifugeError
from tests.fake_server import FakeCentrifugoServer

# Tests for publication filtering (server-side filtering by publication tags).
# The Filter builders construct a FilterNode dict tree; the subscribe request
# carries it in the `tf` field. The feature requires Centrifugo PRO / namespace
# config, so wire-level behavior is exercised against the in-process
# FakeCentrifugoServer.


class TestFilterBuilder(unittest.TestCase):
    def test_comparison_leaf_nodes(self):
        self.assertEqual(Filter.eq("t", "AAPL").node, {"key": "t", "cmp": "eq", "val": "AAPL"})
        self.assertEqual(Filter.neq("a", "b").node, {"key": "a", "cmp": "neq", "val": "b"})
        self.assertEqual(Filter.exists("price").node, {"key": "price", "cmp": "ex"})
        self.assertEqual(Filter.not_exists("id").node, {"key": "id", "cmp": "nex"})
        self.assertEqual(Filter.starts_with("t", "A").node, {"key": "t", "cmp": "sw", "val": "A"})
        self.assertEqual(Filter.ends_with("s", "Q").node, {"key": "s", "cmp": "ew", "val": "Q"})
        self.assertEqual(Filter.contains("c", "ec").node, {"key": "c", "cmp": "ct", "val": "ec"})
        self.assertEqual(Filter.gt("price", "100").node["cmp"], "gt")
        self.assertEqual(Filter.gte("volume", "1000").node["cmp"], "gte")
        self.assertEqual(Filter.lt("price", "200").node["cmp"], "lt")
        self.assertEqual(Filter.lte("volume", "1000").node["cmp"], "lte")

    def test_set_membership_nodes(self):
        self.assertEqual(
            Filter.is_in("category", ["tech", "finance"]).node,
            {"key": "category", "cmp": "in", "vals": ["tech", "finance"]},
        )
        self.assertEqual(
            Filter.not_in("ticker", ["MSFT", "GOOGL"]).node,
            {"key": "ticker", "cmp": "nin", "vals": ["MSFT", "GOOGL"]},
        )

    def test_logical_nodes(self):
        and_node = Filter.all(
            Filter.eq("ticker", "AAPL"),
            Filter.gte("price", "100"),
            Filter.is_in("source", ["NASDAQ", "NYSE"]),
        ).node
        self.assertEqual(and_node["op"], "and")
        self.assertEqual(len(and_node["nodes"]), 3)
        self.assertEqual(and_node["nodes"][0]["key"], "ticker")
        self.assertEqual(and_node["nodes"][2]["cmp"], "in")

        or_node = Filter.any(Filter.eq("ticker", "MSFT"), Filter.eq("category", "tech")).node
        self.assertEqual(or_node["op"], "or")
        self.assertEqual(len(or_node["nodes"]), 2)

        not_node = Filter.negate(Filter.eq("source", "NYSE")).node
        self.assertEqual(not_node["op"], "not")
        self.assertEqual(len(not_node["nodes"]), 1)
        self.assertEqual(not_node["nodes"][0]["cmp"], "eq")

    def test_nested_logical_nodes(self):
        # NOT ( (ticker == AAPL) AND (category in [tech, finance]) )
        node = Filter.negate(
            Filter.all(
                Filter.eq("ticker", "AAPL"),
                Filter.is_in("category", ["tech", "finance"]),
            )
        ).node
        self.assertEqual(node["op"], "not")
        self.assertEqual(len(node["nodes"]), 1)
        self.assertEqual(node["nodes"][0]["op"], "and")
        self.assertEqual(len(node["nodes"][0]["nodes"]), 2)


class TestTagsFilterWire(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.server = FakeCentrifugoServer()
        await self.server.start()

    async def asyncTearDown(self):
        await self.server.stop()

    def _make_client(self):
        return Client(self.server.url, use_protobuf=True)

    async def test_subscribe_request_carries_tags_filter(self):
        client = self._make_client()
        sub = client.new_subscription(
            "market:stocks",
            tags_filter=Filter.all(
                Filter.eq("ticker", "AAPL"),
                Filter.gte("price", "100"),
            ),
        )
        subscribed = asyncio.Future()

        async def on_subscribed(ctx):
            if not subscribed.done():
                subscribed.set_result(ctx)

        sub.events.on_subscribed = on_subscribed
        await client.connect()
        await sub.subscribe()
        await asyncio.wait_for(subscribed, timeout=5)

        tf = self.server.last_subscribe().tf
        self.assertEqual(tf.op, "and")
        self.assertEqual(len(tf.nodes), 2)
        self.assertEqual(tf.nodes[0].key, "ticker")
        self.assertEqual(tf.nodes[0].cmp, "eq")
        self.assertEqual(tf.nodes[0].val, "AAPL")
        self.assertEqual(tf.nodes[1].cmp, "gte")
        await client.disconnect()

    async def test_set_tags_filter_applies_on_subscribe(self):
        client = self._make_client()
        sub = client.new_subscription("market:stocks")
        sub.set_tags_filter(Filter.eq("ticker", "BTC"))
        subscribed = asyncio.Future()

        async def on_subscribed(ctx):
            if not subscribed.done():
                subscribed.set_result(ctx)

        sub.events.on_subscribed = on_subscribed
        await client.connect()
        await sub.subscribe()
        await asyncio.wait_for(subscribed, timeout=5)

        tf = self.server.last_subscribe().tf
        self.assertEqual(tf.key, "ticker")
        self.assertEqual(tf.val, "BTC")
        await client.disconnect()

    async def test_subscribe_without_filter_sends_no_tf(self):
        client = self._make_client()
        sub = client.new_subscription("market:stocks")
        subscribed = asyncio.Future()

        async def on_subscribed(ctx):
            if not subscribed.done():
                subscribed.set_result(ctx)

        sub.events.on_subscribed = on_subscribed
        await client.connect()
        await sub.subscribe()
        await asyncio.wait_for(subscribed, timeout=5)

        self.assertFalse(self.server.last_subscribe().HasField("tf"))
        await client.disconnect()


class TestTagsFilterValidation(unittest.IsolatedAsyncioTestCase):
    async def test_new_subscription_rejects_delta_with_tags_filter(self):
        client = Client("ws://localhost:0/connection/websocket")
        err = None
        try:
            client.new_subscription(
                "market:stocks",
                delta=DeltaType.FOSSIL,
                tags_filter=Filter.eq("ticker", "AAPL"),
            )
        except CentrifugeError as e:
            err = e
        self.assertIsNotNone(err, "expected CentrifugeError for delta + tags filter")
        self.assertIn("tags filter", str(err))

    async def test_set_tags_filter_rejects_delta_combination(self):
        client = Client("ws://localhost:0/connection/websocket")
        sub = client.new_subscription("market:stocks", delta=DeltaType.FOSSIL)
        err = None
        try:
            sub.set_tags_filter(Filter.eq("ticker", "AAPL"))
        except CentrifugeError as e:
            err = e
        self.assertIsNotNone(err, "expected CentrifugeError for delta + tags filter")
        self.assertIn("tags filter", str(err))


if __name__ == "__main__":
    unittest.main()
