"""In-process Centrifugo fake server for tests.

Speaks the protobuf protocol (varint length-delimited) over a WebSocket, or the
JSON one (newline-delimited) with ``FakeCentrifugoServer(use_protobuf=False)``.
Either way commands, replies and hooks are protobuf messages; in JSON mode bytes
fields such as ``Publication.data`` hold raw JSON (``b'{"x": 1}'``). It is
intentionally protocol-level and generic: it provides sensible defaults for the
connect/subscribe/unsubscribe handshake, captures received commands for
assertions, and exposes hooks + raw push senders so new scenarios can be added
WITHOUT touching the client under test or this helper.

This exists because some features (channel compaction, and in future others) are
Centrifugo PRO only and can't be exercised against the OSS docker-compose server
the other suites use; and because a fake gives deterministic control of timing,
errors and reconnects.

Create the client under test with the same ``use_protobuf`` as the server.

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
  - Never reply, leaving the request pending:
      server.drop_command = lambda cmd: cmd.HasField("subscribe")
  - Send anything the protocol allows:
      await server.send_push(some_protocol_push)
  - Drive a reconnect:                await server.close_connection()
  - Assert on what the client sent:   server.received / server.last_subscribe()
  - Assert on handshake headers:      server.request_headers
"""

import base64
import json

import websockets
from google.protobuf.descriptor import FieldDescriptor
from google.protobuf.json_format import MessageToDict, ParseDict

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


def _map_bytes_fields(descriptor, obj, fn):
    """Apply fn to every bytes field value in obj, the dict form of a message
    of the given descriptor. The JSON protocol embeds those values as raw JSON,
    where the protobuf JSON mapping has base64 strings."""
    for field in descriptor.fields:
        if field.name not in obj:
            continue
        value = obj[field.name]
        # Repeated fields are lists in the dict form; FieldDescriptor.label is
        # gone in protobuf 7 and is_repeated is missing in 5.x.
        repeated = isinstance(value, list)
        if field.message_type is not None and field.message_type.GetOptions().map_entry:
            value_field = field.message_type.fields_by_name["value"]
            if value_field.message_type is not None:
                for item in value.values():
                    _map_bytes_fields(value_field.message_type, item, fn)
            elif value_field.type == FieldDescriptor.TYPE_BYTES:
                obj[field.name] = {key: fn(item) for key, item in value.items()}
        elif field.message_type is not None:
            for item in value if repeated else [value]:
                _map_bytes_fields(field.message_type, item, fn)
        elif field.type == FieldDescriptor.TYPE_BYTES:
            obj[field.name] = [fn(item) for item in value] if repeated else fn(value)


def _json_to_base64(value):
    return base64.b64encode(json.dumps(value).encode()).decode()


def _base64_to_json(value):
    return json.loads(base64.b64decode(value))


class FakeCentrifugoServer:
    def __init__(self, use_protobuf=True):
        self._use_protobuf = use_protobuf
        self._server = None
        self.port = 0
        self._secure = False
        self._current_ws = None
        # All commands received from the client, in order.
        self.received = []
        # Handshake request headers of every connection, in order.
        self.request_headers = []
        # connect reply fields; override before connecting if needed.
        self.connect_result = protocol.ConnectResult(
            client="fake-client", version="0.0.0", ping=25
        )
        # Full override for any command: (command) -> Reply or None to fall through.
        self.on_command = None
        # Leave a command without a reply: (command) -> True to drop it. The
        # command is still recorded in received.
        self.drop_command = None
        # Customize the subscribe result per channel: (channel, req) -> SubscribeResult.
        self.on_subscribe = None

    async def start(self, ssl_context=None):
        """Start the server. Pass ssl_context to serve wss:// instead of ws://."""
        self._secure = ssl_context is not None
        self._server = await websockets.serve(
            self._handler,
            "localhost",
            0,
            subprotocols=["centrifuge-protobuf"] if self._use_protobuf else None,
            ssl=ssl_context,
        )
        self.port = self._server.sockets[0].getsockname()[1]

    async def stop(self):
        if self._server:
            self._server.close()
            await self._server.wait_closed()

    @property
    def url(self):
        scheme = "wss" if self._secure else "ws"
        return f"{scheme}://localhost:{self.port}/connection/websocket"

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
        self.request_headers.append(websocket.request.headers)
        try:
            async for message in websocket:
                for cmd in self._decode_commands(message):
                    await self._dispatch(websocket, cmd)
        except websockets.ConnectionClosed:
            pass

    def _decode_commands(self, message):
        if not self._use_protobuf:
            commands = []
            for line in message.strip().split("\n"):
                obj = json.loads(line)
                _map_bytes_fields(protocol.Command.DESCRIPTOR, obj, _json_to_base64)
                commands.append(ParseDict(obj, protocol.Command()))
            return commands
        commands = []
        position = 0
        while position < len(message):
            length, position = _varint_decode(message, position)
            end = position + length
            cmd = protocol.Command()
            cmd.ParseFromString(message[position:end])
            position = end
            commands.append(cmd)
        return commands

    async def _dispatch(self, websocket, cmd):
        self.received.append(cmd)

        if self.drop_command is not None and self.drop_command(cmd):
            return

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

    async def _send(self, websocket, reply):
        if not self._use_protobuf:
            obj = MessageToDict(reply, preserving_proto_field_name=True)
            _map_bytes_fields(protocol.Reply.DESCRIPTOR, obj, _base64_to_json)
            await websocket.send(json.dumps(obj))
            return
        raw = reply.SerializeToString()
        await websocket.send(_varint_encode(len(raw)) + raw)

    async def send_reply(self, reply):
        """Send a raw reply to the active connection."""
        await self._send(self._current_ws, reply)

    async def send_replies(self, replies):
        """Send several raw replies to the active connection in one WebSocket
        message (a frame the client processes reply by reply)."""
        if not self._use_protobuf:
            lines = []
            for reply in replies:
                obj = MessageToDict(reply, preserving_proto_field_name=True)
                _map_bytes_fields(protocol.Reply.DESCRIPTOR, obj, _base64_to_json)
                lines.append(json.dumps(obj))
            await self._current_ws.send("\n".join(lines))
            return
        data = b""
        for reply in replies:
            raw = reply.SerializeToString()
            data += _varint_encode(len(raw)) + raw
        await self._current_ws.send(data)

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
