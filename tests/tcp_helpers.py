"""Raw TCP helpers for transport tests.

- BlackholeProxy: a TCP proxy in front of a test server that can turn the link
  into a dead one - after blackhole() it drops every byte in both directions and
  closes nothing, like a network path that silently went away. Only a raw TCP
  proxy can do this: a WebSocket server, even a fake one, still answers close
  frames.
- SilentServer: accepts TCP connections and never answers - a server (or proxy)
  that hangs in the WebSocket handshake.
"""

import asyncio


class _Link:
    def __init__(self):
        self.dead = False
        self.client_open = True


class BlackholeProxy:
    def __init__(self, upstream_port):
        self._upstream_port = upstream_port
        self._server = None
        self._links = []
        self._writers = []
        self.port = 0

    async def start(self):
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        self.port = self._server.sockets[0].getsockname()[1]

    async def stop(self):
        for writer in self._writers:
            writer.close()
        self._server.close()
        await self._server.wait_closed()

    @property
    def url(self):
        return f"ws://127.0.0.1:{self.port}/connection/websocket"

    @property
    def accepted(self):
        return len(self._links)

    @property
    def open_connections(self):
        return sum(1 for link in self._links if link.client_open)

    def blackhole(self):
        """Kill the links open now: their bytes are dropped from here on, in both
        directions, and they are never closed. New connections work normally."""
        for link in self._links:
            link.dead = True

    async def _handle(self, client_reader, client_writer):
        link = _Link()
        self._links.append(link)
        upstream_reader, upstream_writer = await asyncio.open_connection(
            "localhost", self._upstream_port
        )
        self._writers += [client_writer, upstream_writer]
        await asyncio.gather(
            self._pipe(link, client_reader, upstream_writer, from_client=True),
            self._pipe(link, upstream_reader, client_writer, from_client=False),
            return_exceptions=True,
        )

    @staticmethod
    async def _pipe(link, reader, writer, from_client):
        while True:
            data = await reader.read(65536)
            if not data:
                if from_client:
                    link.client_open = False
                # A dead link never closes.
                if not link.dead:
                    writer.close()
                return
            if not link.dead:
                writer.write(data)


class SilentServer:
    def __init__(self):
        self._server = None
        self._writers = []
        self.accepted = 0
        self.open_connections = 0
        self.port = 0

    async def start(self):
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        self.port = self._server.sockets[0].getsockname()[1]

    async def stop(self):
        for writer in self._writers:
            writer.close()
        self._server.close()
        await self._server.wait_closed()

    @property
    def url(self):
        return f"ws://127.0.0.1:{self.port}/connection/websocket"

    async def _handle(self, reader, writer):
        self.accepted += 1
        self.open_connections += 1
        self._writers.append(writer)
        while await reader.read(65536):
            pass
        self.open_connections -= 1
