"""In-process Centrifugo fake server for tests.

Speaks the protobuf protocol (varint length-delimited) over a WebSocket. It is
intentionally protocol-level and generic: it provides sensible defaults for the
connect/subscribe/unsubscribe handshake, captures received commands for
assertions, and exposes hooks + raw push senders so new scenarios can be added
WITHOUT touching the client under test or this helper.

This exists because some features (channel compaction, and in future others) are
Centrifugo PRO only and can't be exercised against the OSS docker-compose server
the other suites use; and because a fake gives deterministic control of timing,
errors and reconnects.

Protobuf (not JSON) is used because the JSON client transport sends an empty
``Sec-WebSocket-Protocol`` header that the ``websockets`` server rejects; with
protobuf the client sends a valid ``centrifuge-protobuf`` subprotocol. Create the
client under test with ``use_protobuf=True``.

How to extend (most→least common):
  - Customize a subscribe reply:
      server.on_subscribe = lambda ch, req: protocol.SubscribeResult(recoverable=True)
  - Negotiate channel compaction (assign a numeric id when the client offers it):
      server.on_subscribe = lambda ch, req: protocol.SubscribeResult(
          id=42 if req.flag & 1 else 0)
  - Push to a subscription:
      await server.publish(b"...", channel_id=42)   # by numeric id (compaction)
      await server.publish(b"...", channel="news")  # by channel name
  - Fully control any command reply:
      server.on_command = lambda cmd: build_reply(...) or None  # None falls through
  - Send anything the protocol allows:
      await server.send_push(some_protocol_push)
  - Drive a reconnect:                await server.close_connection()
  - Assert on what the client sent:   server.received / server.last_subscribe()
"""

import websockets

import centrifuge.protocol.client_pb2 as protocol


def _varint_encode(number):
    buffer = bytearray()
    while True:
        towrite = number & 0x7F
        number >>= 7
        if number:
            buffer.append(towrite | 0x80)
        else:
            buffer.append(towrite)
            return bytes(buffer)


def _varint_decode(buffer, position):
    result = 0
    shift = 0
    while True:
        byte = buffer[position]
        position += 1
        result |= (byte & 0x7F) << shift
        shift += 7
        if not byte & 0x80:
            return result, position


class FakeCentrifugoServer:
    def __init__(self):
        self._server = None
        self.port = 0
        self._current_ws = None
        # All commands received from the client, in order.
        self.received = []
        # connect reply fields; override before connecting if needed.
        self.connect_result = protocol.ConnectResult(
            client="fake-client", version="0.0.0", ping=25
        )
        # Full override for any command: (command) -> Reply or None to fall through.
        self.on_command = None
        # Customize the subscribe result per channel: (channel, req) -> SubscribeResult.
        self.on_subscribe = None

    async def start(self):
        self._server = await websockets.serve(
            self._handler, "localhost", 0, subprotocols=["centrifuge-protobuf"]
        )
        self.port = self._server.sockets[0].getsockname()[1]

    async def stop(self):
        if self._server:
            self._server.close()
            await self._server.wait_closed()

    @property
    def url(self):
        return f"ws://localhost:{self.port}/connection/websocket"

    def last_subscribe(self):
        """The most recent subscribe request, or None."""
        for cmd in reversed(self.received):
            if cmd.HasField("subscribe"):
                return cmd.subscribe
        return None

    async def close_connection(self):
        """Close the active connection from the server side, triggering the
        client's automatic reconnect."""
        if self._current_ws is not None:
            await self._current_ws.close()

    async def disconnect_close(self, code, reason):
        """Close the active connection with a specific code, the way Centrifugo
        delivers a server disconnect (e.g. 3014 state invalidated) — as a
        WebSocket close frame rather than a Disconnect push."""
        if self._current_ws is not None:
            await self._current_ws.close(code=code, reason=reason)

    async def _handler(self, websocket):
        self._current_ws = websocket
        try:
            async for message in websocket:
                position = 0
                while position < len(message):
                    length, position = _varint_decode(message, position)
                    end = position + length
                    cmd = protocol.Command()
                    cmd.ParseFromString(message[position:end])
                    position = end
                    await self._dispatch(websocket, cmd)
        except websockets.ConnectionClosed:
            pass

    async def _dispatch(self, websocket, cmd):
        self.received.append(cmd)

        if self.on_command is not None:
            reply = self.on_command(cmd)
            if reply is not None:
                await self._send(websocket, reply)
                return

        if cmd.HasField("connect"):
            reply = protocol.Reply(id=cmd.id, connect=self.connect_result)
            await self._send(websocket, reply)
        elif cmd.HasField("subscribe"):
            if self.on_subscribe is not None:
                result = self.on_subscribe(cmd.subscribe.channel, cmd.subscribe)
            else:
                result = protocol.SubscribeResult()
            reply = protocol.Reply(id=cmd.id)
            reply.subscribe.CopyFrom(result)
            await self._send(websocket, reply)
        elif cmd.HasField("unsubscribe"):
            reply = protocol.Reply(id=cmd.id)
            reply.unsubscribe.SetInParent()
            await self._send(websocket, reply)
        elif cmd.id != 0:
            # Reply to anything else with an empty result to avoid client timeouts.
            await self._send(websocket, protocol.Reply(id=cmd.id))

    # --- raw escape hatches -------------------------------------------------

    @staticmethod
    async def _send(websocket, reply):
        raw = reply.SerializeToString()
        await websocket.send(_varint_encode(len(raw)) + raw)

    async def send_reply(self, reply):
        """Send a raw reply to the active connection."""
        await self._send(self._current_ws, reply)

    async def send_push(self, push):
        """Send a raw push (wrapped in a reply) to the active connection."""
        reply = protocol.Reply()
        reply.push.CopyFrom(push)
        await self.send_reply(reply)

    # --- typed push senders -------------------------------------------------
    #
    # Channel compaction pushes carry a numeric ``id`` and no channel; otherwise
    # the ``channel`` name is used. Pass exactly one of id / channel.

    @staticmethod
    def _push(channel_id=0, channel=""):
        push = protocol.Push()
        if channel_id:
            push.id = channel_id
        elif channel:
            push.channel = channel
        return push

    async def publish(self, data, channel_id=0, channel=""):
        push = self._push(channel_id, channel)
        push.pub.data = data
        await self.send_push(push)

    async def join(self, client, channel_id=0, channel=""):
        push = self._push(channel_id, channel)
        push.join.info.client = client
        await self.send_push(push)

    async def leave(self, client, channel_id=0, channel=""):
        push = self._push(channel_id, channel)
        push.leave.info.client = client
        await self.send_push(push)

    async def unsubscribe(self, channel, code, reason):
        push = protocol.Push(channel=channel)
        push.unsubscribe.code = code
        push.unsubscribe.reason = reason
        await self.send_push(push)

    async def disconnect(self, code, reason):
        push = protocol.Push()
        push.disconnect.code = code
        push.disconnect.reason = reason
        await self.send_push(push)
