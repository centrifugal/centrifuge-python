import asyncio
import time
import unittest

import centrifuge.protocol.client_pb2 as protocol
from centrifuge import Client
from centrifuge.client import ClientState
from tests.fake_server import FakeCentrifugoServer

# A timeout that fires late, because the process was suspended, lets what
# arrived meanwhile be handled first. The suspension is simulated by an event
# handler that blocks the event loop; the server sends the awaited frame right
# after it.


async def wait_until(condition, timeout=3.0):
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if condition():
            return True
        await asyncio.sleep(0.01)
    return condition()


class TestLateTimers(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.server = FakeCentrifugoServer()
        await self.server.start()
        self.connecting = []

    async def asyncTearDown(self):
        await self.server.stop()

    async def _subscribed_client(self, freeze, **kwargs):
        client = Client(self.server.url, use_protobuf=True, **kwargs)

        async def on_connecting(ctx):
            self.connecting.append(ctx.code)

        client.events.on_connecting = on_connecting
        sub = client.new_subscription("ch")

        async def on_publication(_ctx):
            time.sleep(freeze)  # blocks the loop on purpose

        sub.events.on_publication = on_publication
        await client.connect()
        await sub.subscribe()
        await sub.ready()
        return client

    async def test_reply_arriving_right_after_freeze_wins(self):
        loop = asyncio.get_running_loop()
        tasks = []

        def drop_rpc(cmd):
            if not cmd.HasField("rpc"):
                return False
            # Freeze the client for 1.5 s and reply right after that.
            tasks.append(loop.create_task(self.server.publish(b"freeze", channel="ch")))
            reply = protocol.Reply(id=cmd.id)
            reply.rpc.SetInParent()
            loop.call_later(
                1.7, lambda: tasks.append(loop.create_task(self.server.send_reply(reply)))
            )
            return True

        self.server.drop_command = drop_rpc
        client = await self._subscribed_client(freeze=1.5, timeout=0.3)

        await client.rpc("method", b"")  # used to fail with a timeout
        await client.disconnect()
        await asyncio.gather(*tasks)

    async def test_ping_arriving_right_after_freeze_keeps_connection(self):
        self.server.connect_result.ping = 1
        client = await self._subscribed_client(freeze=2.5, max_server_ping_delay=0.3)
        loop = asyncio.get_running_loop()

        # The ping timer is due 1.3 s after connecting; the client is frozen for
        # 2.5 s, and the server pings right after that.
        await self.server.publish(b"freeze", channel="ch")
        ping = []
        loop.call_later(
            0.2, lambda: ping.append(loop.create_task(self.server.send_reply(protocol.Reply())))
        )
        for _ in range(3):
            await asyncio.sleep(0.5)
            await self.server.send_reply(protocol.Reply())
        self.assertEqual(self.connecting, [0], "the client reconnected")
        self.assertEqual(client.state, ClientState.CONNECTED)
        await client.disconnect()
