import asyncio
import unittest

import centrifuge.protocol.client_pb2 as protocol
from centrifuge import Client, ClientState
from tests.fake_server import FakeCentrifugoServer

# Cancelling a coroutine awaiting a command reply - e.g. with asyncio.wait_for()
# - cancels the future registered for that command. The client must cope with
# the reply (or the disconnect) coming for such a command afterwards.


class TestCancelledCommand(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.server = FakeCentrifugoServer()
        await self.server.start()
        self.held_rpc_ids = []

        def on_command(cmd):
            if not cmd.HasField("rpc"):
                return None
            if cmd.rpc.method == "slow":
                # Do not reply now, the test sends the reply itself later. A
                # reply with an id the client never used is sent instead, since
                # returning None would fall through to the default reply.
                self.held_rpc_ids.append(cmd.id)
                return protocol.Reply(id=0xFFFFFF)
            return protocol.Reply(id=cmd.id, rpc=protocol.RPCResult())

        self.server.on_command = on_command
        self.client = Client(self.server.url, use_protobuf=True, timeout=1)
        await self.client.connect()
        await self.client.ready()

    async def asyncTearDown(self):
        await self.client.disconnect()
        await self.server.stop()

    async def _cancel_slow_rpc(self):
        with self.assertRaises(asyncio.TimeoutError):  # noqa: PT027
            await asyncio.wait_for(self.client.rpc("slow", b""), timeout=0.1)
        self.assertEqual(len(self.held_rpc_ids), 1)

    async def test_late_reply_to_cancelled_command(self):
        await self._cancel_slow_rpc()

        await self.server.send_reply(protocol.Reply(id=self.held_rpc_ids[0]))

        # Used to fail with OperationTimeoutError: setting the result of the
        # cancelled future raised InvalidStateError, which killed the task
        # processing incoming messages, so no further reply was ever handled.
        await self.client.rpc("fast", b"")
        self.assertEqual(self.client.state, ClientState.CONNECTED)

    async def test_disconnect_with_cancelled_command_in_flight(self):
        await self._cancel_slow_rpc()

        # Used to raise InvalidStateError from failing the cancelled future,
        # leaving the client stuck in the connected state.
        await self.client.disconnect()

        self.assertEqual(self.client.state, ClientState.DISCONNECTED)
        self.assertEqual(self.client._inflight_commands, {})


if __name__ == "__main__":
    unittest.main()
