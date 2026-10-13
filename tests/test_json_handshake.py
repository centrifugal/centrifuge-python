import json
import unittest

import websockets

from centrifuge import Client, ClientState

# The JSON transport offers no subprotocol, so it must not send a
# Sec-WebSocket-Protocol header: RFC 6455 allows the header only with at least
# one subprotocol in it, and servers such as the websockets one reject it empty.


class TestJsonHandshake(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.subprotocol_headers = []
        self.server = await websockets.serve(self._handler, "localhost", 0)
        port = self.server.sockets[0].getsockname()[1]
        self.url = f"ws://localhost:{port}/connection/websocket"

    async def asyncTearDown(self):
        self.server.close()
        await self.server.wait_closed()

    async def _handler(self, websocket):
        headers = websocket.request.headers.get_all("Sec-WebSocket-Protocol")
        self.subprotocol_headers.append(headers)
        async for message in websocket:
            for line in message.split("\n"):
                command = json.loads(line)
                if "connect" in command:
                    result = {"client": "fake-client", "version": "0.0.0"}
                    await websocket.send(json.dumps({"id": command["id"], "connect": result}))

    async def test_json_client_connects_without_a_subprotocol_header(self):
        client = Client(self.url, min_reconnect_delay=3600, max_reconnect_delay=3600)
        self.addAsyncCleanup(client.disconnect)

        await client.connect()

        self.assertEqual(client.state, ClientState.CONNECTED)
        self.assertEqual(self.subprotocol_headers, [[]])
