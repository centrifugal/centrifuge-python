import base64
import json
import random
import asyncio
import logging
from enum import Enum
from typing import Any, Optional, Union, Dict, List, Coroutine
from typing import Callable, Awaitable
from dataclasses import dataclass
import websockets
from google.protobuf.json_format import ParseDict
from google.protobuf.json_format import MessageToDict
import centrifuge.protocol.client_pb2 as protocol
from centrifuge.codes import DisconnectedCode, ConnectingCode, UnsubscribedCode, SubscribingCode

logger = logging.getLogger('centrifuge')

# JSON type represents objects which may be encoded to JSON.
JSON = Union[Dict[str, 'JSON'], List['JSON'], str, int, float, bool, None]
# BytesOrJSON type represents objects which may be encoded to JSON or bytes.
BytesOrJSON = Union[bytes, JSON]


class CentrifugeException(Exception):
    """
    CentrifugeException is a base exception for all other exceptions
    in this library.
    """
    pass


class ClientDisconnected(CentrifugeException):
    """
    ConnectionClosed raised when underlying websocket connection closed.
    """
    pass


class DuplicateSubscription(CentrifugeException):
    pass


class SubscriptionUnsubscribed(CentrifugeException):
    """
    SubscriptionUnsubscribedError raised when an error subscribing on channel occurred.
    """
    pass


class Timeout(CentrifugeException):
    """
    Timeout raised every time operation times out.
    """
    pass


class ReplyError(CentrifugeException):
    """
    ReplyError raised when an error returned from server as result of presence/history/publish call.
    """
    def __init__(self, code: int, message: str, temporary: bool):
        self.code = code
        self.message = message
        self.temporary = temporary
        super().__init__(f"Error {code}: {message} (temporary: {temporary})")


class Unauthorized(CentrifugeException):
    """
    Unauthorized may be raised from user's get_token functions to indicate
    client is not able to connect or subscribe.
    """
    pass


class _JsonCodec:
    """
    _JsonCodec is a default codec for Centrifuge library. It encodes commands using JSON.
    """

    @staticmethod
    def encode_commands(commands):
        return '\n'.join(json.dumps(command) for command in commands)

    @staticmethod
    def decode_replies(data):
        return [json.loads(reply) for reply in data.strip().split('\n')]


class _ProtobufCodec:
    """
    _ProtobufCodec encodes commands using Protobuf protocol.
    """

    @staticmethod
    def encode_commands(commands):
        serialized_commands = []
        for command in commands:
            # noinspection PyUnresolvedReferences
            serialized = ParseDict(command, protocol.Command()).SerializeToString()
            serialized_commands.append(_varint_encode(len(serialized)) + serialized)
        return b''.join(serialized_commands)

    @staticmethod
    def decode_replies(data):
        replies = []
        position = 0
        while position < len(data):
            message_length, position = _varint_decode(data, position)
            message_end = position + message_length
            message_bytes = data[position:message_end]
            position = message_end
            # noinspection PyUnresolvedReferences
            reply = protocol.Reply()
            reply.ParseFromString(message_bytes)
            replies.append(MessageToDict(reply, preserving_proto_field_name=True))
        return replies


def _varint_encode(number):
    """Encode an integer as a varint."""
    buffer = []
    while True:
        towrite = number & 0x7f
        number >>= 7
        if number:
            buffer.append(towrite | 0x80)
        else:
            buffer.append(towrite)
            break
    return bytes(buffer)


def _varint_decode(buffer, position):
    """Decode a varint from buffer starting at position."""
    result = 0
    shift = 0
    while True:
        byte = buffer[position]
        position += 1
        result |= (byte & 0x7f) << shift
        shift += 7
        if not byte & 0x80:
            break
    return result, position


def _backoff(step: int, min_value: float, max_value: float):
    """
    Implements exponential backoff with jitter.
    Using full jitter technique - see https://aws.amazon.com/blogs/architecture/exponential-backoff-and-jitter/
    """
    if step > 31:
        step = 31
    interval = random.uniform(0, min(max_value, min_value * 2 ** step))
    return min(max_value, min_value + interval)


def _code_message(code: Enum):
    return str(code.name).lower().replace("_", " ")


def _code_number(code: Enum):
    return int(code.value)


@dataclass
class StreamPosition:
    """
    StreamPosition represents a position in stream.
    """
    offset: int
    epoch: str


@dataclass
class ClientInfo:
    """
    ClientInfo represents information about client connection.

    Attributes:
        client: client ID.
        user: user ID.
        conn_info: optional connection information (i.e. may be None).
        chan_info: optional channel information (i.e. may be None).
    """
    client: str
    user: str
    conn_info: Optional[BytesOrJSON]
    chan_info: Optional[BytesOrJSON]


@dataclass
class ConnectedContext:
    """
    ConnectedContext is a context passed to on_connected callback.

    Attributes:
        client: client ID.
        version: server version.
        data: data returned from server on connect.
    """

    client: str
    version: str
    data: Optional[BytesOrJSON]


@dataclass
class ConnectingContext:
    """
    ConnectingContext is a context passed to on_connecting callback.

    Attributes:
        code: code of state transition.
        reason: reason of state transition.
    """

    code: int
    reason: str


@dataclass
class DisconnectedContext:
    """
    DisconnectedContext is a context passed to on_disconnected callback.

    Attributes:
        code: code of state transition.
        reason: reason of state transition.
    """
    code: int
    reason: str


@dataclass
class ErrorContext:
    """ErrorContext is a context passed to on_error callback."""
    error: Exception


@dataclass
class ServerSubscribingContext:
    """ServerSubscribingContext is a context passed to on_subscribing callback for server-side subscriptions."""
    channel: str


@dataclass
class ServerSubscribedContext:
    """ServerSubscribedContext is a context passed to on_subscribed callback for server-side subscriptions."""
    channel: str


@dataclass
class ServerUnsubscribedContext:
    """ServerUnsubscribedContext is a context passed to on_unsubscribed callback for server-side subscriptions."""
    channel: str


@dataclass
class ServerPublicationContext:
    """ServerPublicationContext is a context passed to on_publication callback for server-side subscriptions."""
    channel: str
    data: BytesOrJSON
    info: Optional[ClientInfo]


@dataclass
class ServerJoinContext:
    """ServerJoinContext is a context passed to on_join callback for server-side subscriptions."""
    channel: str
    info: ClientInfo


@dataclass
class ServerLeaveContext:
    """ServerLeaveContext is a context passed to on_leave callback for server-side subscriptions."""
    channel: str
    info: ClientInfo


@dataclass
class SubscribingContext:
    """SubscribingContext is a context passed to on_subscribing callback."""
    code: int
    reason: str


@dataclass
class SubscribedContext:
    """SubscribedContext is a context passed to on_subscribed callback."""
    channel: str
    recoverable: bool
    positioned: bool
    stream_position: Optional[StreamPosition]
    was_recovering: bool
    recovered: bool
    data: Optional[BytesOrJSON]


@dataclass
class UnsubscribedContext:
    """UnsubscribedContext is a context passed to on_unsubscribed callback."""
    code: int
    reason: str


@dataclass
class PublicationContext:
    """PublicationContext is a context passed to on_publication callback."""
    offset: Optional[int]
    data: BytesOrJSON
    info: Optional[ClientInfo]


@dataclass
class JoinContext:
    """JoinContext is a context passed to on_join callback."""
    info: ClientInfo


@dataclass
class LeaveContext:
    """LeaveContext is a context passed to on_leave callback."""
    info: ClientInfo


@dataclass
class SubscriptionErrorContext:
    """SubscriptionErrorContext is a context passed to on_error callback of subscription."""
    error: Exception


@dataclass
class PublishResult:
    """PublishResult is a result of publish operation."""
    pass


@dataclass
class RpcResult:
    """RpcResult is a result of RPC operation."""
    data: BytesOrJSON


@dataclass
class PresenceResult:
    """PresenceResult is a result of presence operation."""
    clients: Dict[str, ClientInfo]


@dataclass
class PresenceStatsResult:
    """PresenceStatsResult is a result of presence stats operation."""
    num_clients: int
    num_users: int


@dataclass
class HistoryResult:
    """HistoryResult is a result of history operation."""
    publications: List[PublicationContext]
    offset: int
    epoch: str


@dataclass
class HistoryOptions:
    """HistoryOptions is a set of options to use in history operation."""
    limit: Optional[int]
    since: Optional[StreamPosition]
    reverse: Optional[bool]


class _ConnectionEventHandler:
    """_ConnectionEventHandler is a set of callbacks called on various client events."""
    async def on_connecting(self, ctx: ConnectingContext):
        """Called when connecting. This may be initial connecting, or
        temporary loss of connection with automatic reconnect"""
        pass

    async def on_connected(self,  ctx: ConnectedContext):
        """Called when connected."""
        pass

    async def on_disconnected(self, ctx: DisconnectedContext):
        """Called when disconnected."""
        pass

    async def on_error(self, ctx: ErrorContext):
        """Called when there's an error."""
        pass

    async def on_subscribed(self, ctx: ServerSubscribedContext):
        """Called when subscribed on server-side subscription."""
        pass

    async def on_subscribing(self, ctx: ServerSubscribingContext):
        """Called when subscribing to server-side subscription."""
        pass

    async def on_unsubscribed(self, ctx: ServerUnsubscribedContext):
        """Called when unsubscribed from server-side subscription."""
        pass

    async def on_publication(self, ctx: ServerPublicationContext):
        """Called when there's a publication coming from a server-side subscription."""
        pass

    async def on_join(self, ctx: ServerJoinContext):
        """Called when some client joined channel in server-side subscription."""
        pass

    async def on_leave(self, ctx: ServerLeaveContext):
        """Called when some client left channel in server-side subscription."""
        pass


class _SubscriptionEventHandler:
    """_SubscriptionEventHandler is a set of callbacks called on various subscription events."""
    async def on_subscribing(self, ctx: SubscribingContext):
        """Called when subscribing. This may be initial subscribing attempt,
        or temporary loss with automatic resubscribe"""
        pass

    async def on_subscribed(self, ctx: SubscribedContext):
        """Called when subscribed."""
        pass

    async def on_unsubscribed(self, ctx: UnsubscribedContext):
        """Called when unsubscribed. No auto re-subscribing will happen after this"""
        pass

    async def on_publication(self, ctx: PublicationContext):
        """Called when there's a publication coming from a channel"""
        pass

    async def on_join(self, ctx: JoinContext):
        """Called when some client joined channel (join/leave must be enabled on server side)."""
        pass

    async def on_leave(self, ctx: LeaveContext):
        """Called when some client left channel (join/leave must be enabled on server side)"""
        pass

    async def on_error(self, ctx: SubscriptionErrorContext):
        """Called when various subscription async errors happen. In most cases this is only for logging purposes"""
        pass


class ClientState(Enum):
    """
    ClientState represents possible states of client connection.
    """
    DISCONNECTED = 'disconnected'
    CONNECTING = 'connecting'
    CONNECTED = 'connected'


class Client:
    """
    Client is a websocket client to Centrifuge/Centrifugo server.
    """
    _reconnect_backoff_factor = 2
    _reconnect_backoff_jitter = 0.5

    def __init__(
            self,
            address,
            token: str = '',
            get_token: Optional[Callable[..., Awaitable[None]]] = None,
            use_protobuf: bool = False,
            timeout: float = 5.0,
            max_server_ping_delay: float = 10.0,
            name: str = 'python',
            version: str = '',
            data: BytesOrJSON = None,
            min_reconnect_delay: float = 0.1,
            max_reconnect_delay: float = 20.0,
            loop: Any = None
    ):
        self.state: ClientState = ClientState.DISCONNECTED
        self._address = address
        self._events = _ConnectionEventHandler()
        self._use_protobuf = use_protobuf
        self._codec = _ProtobufCodec if use_protobuf else _JsonCodec
        self._conn = None
        self._id: int = 0
        self._subs: Dict[str, Subscription] = {}
        self._messages = asyncio.Queue()
        self._delay = 0
        self._reconnect = True
        self._name = name
        self._version = version
        self._data = data
        self._timeout = timeout
        self._min_reconnect_delay = min_reconnect_delay
        self._max_reconnect_delay = max_reconnect_delay
        self._send_pong = True
        self._ping_interval = 0
        self._max_server_ping_delay = max_server_ping_delay
        self._ping_timer = None
        self._future = None
        self._token = token
        self._get_token = get_token
        self._loop = loop or asyncio.get_event_loop()
        self._futures = {}
        self._reconnect_attempts = 0

    def on_connecting(self, handler: Callable[[ConnectingContext], Coroutine[None, None, None]]):
        """Allows setting a callback for connecting event."""
        self._events.on_connecting = handler

    def on_connected(self, handler: Callable[[ConnectedContext], Coroutine[None, None, None]]):
        """Allows setting a callback for connected event."""
        self._events.on_connected = handler

    def on_disconnected(self, handler: Callable[[DisconnectedContext], Coroutine[None, None, None]]):
        """Allows setting a callback for disconnected event."""
        self._events.on_disconnected = handler

    def on_error(self, handler: Callable[[ErrorContext], Coroutine[None, None, None]]):
        """Allows setting a callback for error event."""
        self._events.on_error = handler

    def on_subscribing(self, handler: Callable[[ServerSubscribingContext], Coroutine[None, None, None]]):
        """Allows setting a callback for server-side subscriptions subscribing event."""
        self._events.on_subscribing = handler

    def on_subscribed(self, handler: Callable[[ServerSubscribedContext], Coroutine[None, None, None]]):
        """Allows setting a callback for server-side subscriptions subscribed event."""
        self._events.on_subscribed = handler

    def on_unsubscribed(self, handler: Callable[[ServerUnsubscribedContext], Coroutine[None, None, None]]):
        """Allows setting a callback for server-side subscriptions unsubscribed event."""
        self._events.on_unsubscribed = handler

    def on_publication(self, handler: Callable[[ServerPublicationContext], Coroutine[None, None, None]]):
        """Allows setting a callback for publications coming from server-side subscriptions."""
        self._events.on_publication = handler

    def on_join(self, handler: Callable[[ServerJoinContext], Coroutine[None, None, None]]):
        """Allows setting a callback for join messages coming from server-side subscriptions."""
        self._events.on_join = handler

    def on_leave(self, handler: Callable[[ServerLeaveContext], Coroutine[None, None, None]]):
        """Allows setting a callback for leave messages coming from server-side subscriptions."""
        self._events.on_leave = handler

    def subscriptions(self) -> Dict[str, 'Subscription']:
        """Returns a copy of subscriptions dict."""
        return self._subs.copy()

    def new_subscription(
            self,
            channel: str,
            token: str = '',
            get_token: Optional[Callable[..., Awaitable[None]]] = None,
            min_resubscribe_delay=0.1,
            max_resubscribe_delay=10.0
    ):
        """
        Creates new subscription to channel. If subscription already exists then
        DuplicateSubscription exception will be raised.
        """
        if self.get_subscription(channel):
            raise DuplicateSubscription('subscription to channel "' + channel + '" is already registered')
        sub = Subscription(
            self,
            channel,
            token=token,
            get_token=get_token,
            min_resubscribe_delay=min_resubscribe_delay,
            max_resubscribe_delay=max_resubscribe_delay,
        )
        self._subs[channel] = sub
        return sub

    def get_subscription(self, channel: str) -> Optional['Subscription']:
        """
        Returns subscription by channel name. If subscription does not exist then None returned.
        """
        return self._subs.get(channel)

    def remove_subscription(self, sub: 'Subscription'):
        """
        Removes subscription from client internal registry.
        """
        if not sub:
            return
        del self._subs[sub.channel]

    async def _close(self):
        if not self._conn:
            return
        try:
            await self._conn.close()
        except websockets.ConnectionClosed:
            pass

    async def _schedule_reconnect(self):
        if self.state == ClientState.CONNECTED:
            return

        if self._conn and self._conn.open:
            return

        if not self._reconnect:
            logger.debug("won't reconnect")
            return

        self.status = ClientState.CONNECTING

        delay = _backoff(self._reconnect_attempts, self._min_reconnect_delay, self._max_reconnect_delay)
        self._reconnect_attempts += 1
        logger.debug("start reconnecting in %f", delay)
        await asyncio.sleep(delay)
        if self.state != ClientState.CONNECTING:
            return
        await self._create_connection()

    def _next_command_id(self):
        self._id += 1
        return self._id

    async def _create_connection(self) -> bool:
        subprotocols = []
        if self._use_protobuf:
            subprotocols = ["centrifuge-protobuf"]
        try:
            self._conn = await websockets.connect(self._address, subprotocols=subprotocols)
        except OSError as e:
            handler = self._events.on_error
            await handler(ErrorContext(error=e))
            asyncio.ensure_future(self._schedule_reconnect())
            return False

        self._delay = self._min_reconnect_delay
        connect = {}
        if self._token:
            connect['token'] = self._token

        cmd_id = self._next_command_id()

        command = {
            'id': cmd_id,
            'connect': connect
        }
        future = self._register_future(cmd_id, self._timeout)

        asyncio.ensure_future(self._listen())
        asyncio.ensure_future(self._process_messages())

        ok = await self._send_commands([command])
        if not ok:
            return False

        reply = await future
        logger.info(reply)
        if reply.get('error'):
            logger.debug("connect reply has error: %s", reply.get('error'))
            if self._future:
                self._future.set_result(False)
            await self._close()
            handler = self._events.on_error
            await handler(ErrorContext(error=CentrifugeException()))
        else:
            connect = reply['connect']
            self.client_id = connect['client']
            self.state = ClientState.CONNECTED
            self._send_pong = connect.get('pong', False)
            self._ping_interval = connect.get('ping', 0)
            if self._ping_interval > 0:
                self._restart_ping_wait()

            handler = self._events.on_connected
            await handler(ConnectedContext(
                client=connect.get('client'),
                version=connect.get('version', ''),
                data=self._decode_data(connect.get('data', None))
            ))
            if self._future:
                self._future.set_result(True)

            self._reconnect_attempts = 0

            channels = self._subs.keys()
            for channel in channels:
                asyncio.ensure_future(self._subscribe(channel))

    async def connect(self):
        """
        Initiate connection to server.
        """
        if self.state == ClientState.CONNECTING:
            return

        self.state = ClientState.CONNECTING

        handler = self._events.on_connecting
        code = ConnectingCode.CONNECT_CALLED
        await handler(ConnectingContext(code=_code_number(code), reason=_code_message(code)))
        await self._create_connection()

    async def disconnect(self):
        """
        Disconnect from server.
        """
        if self.state == ClientState.DISCONNECTED:
            return

        code = DisconnectedCode.DISCONNECT_CALLED
        await self._disconnect(_code_number(code), _code_message(code), False)

    @staticmethod
    def _extract_error_details(reply):
        error = reply['error']
        return error['code'], error['message'], error.get('temporary', False)

    async def _subscribe(self, channel):
        if self.state != ClientState.CONNECTED:
            logger.debug('skip subscribe to %s until connected', channel)
            return

        logger.debug('subscribe to channel %s', channel)

        subscribe = {'channel': channel}

        cmd_id = self._next_command_id()
        command = {
            'id': cmd_id,
            'subscribe': subscribe
        }
        future = self._register_future(cmd_id, self._timeout)

        ok = await self._send_commands([command])
        if not ok:
            return

        sub = self._subs.get(channel)
        if not sub:
            return

        # TODO: handle exception here.
        reply = await future

        if reply.get('error'):
            logger.debug("subscribe reply has error: %s", reply.get('error'))
            code, message, temporary = self._extract_error_details(reply)
            if temporary:
                # noinspection PyProtectedMember
                await sub._schedule_resubscribe()
            else:
                # noinspection PyProtectedMember
                await sub._move_unsubscribed(code, message)
        else:
            # noinspection PyProtectedMember
            await sub._move_subscribed(reply['subscribe'])

    async def _resubscribe(self, sub: 'Subscription'):
        self._subs[sub.channel] = sub
        asyncio.ensure_future(self._subscribe(sub.channel))

    async def _unsubscribe(self, channel: str):
        sub = self._subs.get(channel, None)
        if not sub:
            return

        unsubscribe = {'channel': sub.channel}

        cmd_id = self._next_command_id()
        command = {
            'id': cmd_id,
            'unsubscribe': unsubscribe
        }
        future = self._register_future(cmd_id, self._timeout)

        ok = await self._send_commands([command])
        if not ok:
            return

        # TODO: handle exception and reply error here.
        await future

    def _register_future(self, cmd_id: int, timeout: float):
        future = asyncio.Future()
        self._futures[cmd_id] = future

        if timeout:
            def cb():
                if not future.done():
                    future.set_exception(Timeout)
                    del self._futures[cmd_id]

            self._loop.call_later(timeout, cb)

        return future

    def _future_error(self, cmd_id: int, error: Exception):
        future = self._futures.get(cmd_id)
        if not future:
            return
        future.set_exception(CentrifugeException(error))
        del self._futures[cmd_id]

    def _future_success(self, cmd_id: int, reply):
        future = self._futures.get(cmd_id)
        if not future:
            return
        future.set_result(reply)
        del self._futures[cmd_id]

    def _decode_data(self, data: BytesOrJSON):
        if data is None:
            return None
        if self._use_protobuf:
            if isinstance(data, str):
                return base64.b64decode(data)
            return data
        else:
            return data

    def _encode_data(self, data: BytesOrJSON):
        if self._use_protobuf:
            if not isinstance(data, bytes):
                raise CentrifugeException('when using Protobuf protocol you must encode payloads to bytes')
            return base64.b64encode(data)
        else:
            if isinstance(data, bytes):
                raise CentrifugeException('when using JSON protocol you can not encode payloads to bytes')
            return data

    @staticmethod
    def _check_reply_error(reply):
        if reply.get('error'):
            error = reply['error']
            raise ReplyError(error['code'], error['message'], error.get('temporary', False))

    async def publish(self, channel: str, data: BytesOrJSON, timeout=None) -> PublishResult:
        cmd_id = self._next_command_id()
        command = {
            'id': cmd_id,
            'publish': {
                'channel': channel,
                'data': self._encode_data(data)
            }
        }
        future = self._register_future(cmd_id, timeout or self._timeout)
        ok = await self._send_commands([command])
        if not ok:
            raise ClientDisconnected()
        reply = await future
        self._check_reply_error(reply)
        return PublishResult()

    async def history(self, channel: str, timeout=None) -> HistoryResult:
        cmd_id = self._next_command_id()
        command = {
            'id': cmd_id,
            'history': {
                'channel': channel,
            }
        }
        future = self._register_future(cmd_id, timeout or self._timeout)
        ok = await self._send_commands([command])
        if not ok:
            raise ClientDisconnected()
        reply = await future
        self._check_reply_error(reply)
        return HistoryResult(
            epoch='',
            offset=0,
            publications=[]
        )

    async def presence(self, channel: str, timeout=None) -> PresenceResult:
        cmd_id = self._next_command_id()
        command = {
            'id': cmd_id,
            'presence': {
                'channel': channel,
            }
        }
        future = self._register_future(cmd_id, timeout or self._timeout)
        ok = await self._send_commands([command])
        if not ok:
            raise ClientDisconnected()
        reply = await future
        self._check_reply_error(reply)
        return PresenceResult(
            clients={}
        )

    async def presence_stats(self, channel: str, timeout=None) -> PresenceStatsResult:
        cmd_id = self._next_command_id()
        command = {
            'id': cmd_id,
            'presence_stats': {
                'channel': channel,
            }
        }
        future = self._register_future(cmd_id, timeout or self._timeout)
        ok = await self._send_commands([command])
        if not ok:
            raise ClientDisconnected()
        reply = await future
        self._check_reply_error(reply)
        return PresenceStatsResult(
            num_clients=0,
            num_users=0,
        )

    async def rpc(self, method: str, data: BytesOrJSON, timeout=None) -> RpcResult:
        cmd_id = self._next_command_id()
        command = {
            'id': cmd_id,
            'rpc': {
                'method': method,
                'data': self._encode_data(data)
            }
        }
        future = self._register_future(cmd_id, timeout or self._timeout)
        ok = await self._send_commands([command])
        if not ok:
            raise ClientDisconnected()
        reply = await future
        self._check_reply_error(reply)
        return RpcResult(
            data=self._decode_data(reply['rpc'].get('data'))
        )

    async def _disconnect(self, code: int, reason: str, reconnect: bool):
        if self._ping_timer:
            logger.debug('canceling ping timer')
            self._ping_timer.cancel()
            self._ping_timer = None

        if not reconnect:
            self._reconnect = False

        if self.state == ClientState.DISCONNECTED:
            return

        if reconnect:
            self.state = ClientState.CONNECTING
        else:
            self.state = ClientState.DISCONNECTED

        if self._conn and self._conn.state != 3:
            await self._close()

        for ch, sub in self._subs.items():
            sub._future = asyncio.Future()
            # noinspection PyProtectedMember
            handler = sub._events.on_unsubscribed
            if sub.state != SubscriptionState.UNSUBSCRIBED and handler:
                await handler(UnsubscribedContext(code=code, reason=reason))

        handler = self._events.on_disconnected
        if handler:
            await handler(DisconnectedContext(
                code=0,
                reason=reason
            ))

        if reconnect:
            asyncio.ensure_future(self._schedule_reconnect())

    async def _no_ping(self):
        code = ConnectingCode.NO_PING
        await self._disconnect(_code_number(code), _code_message(code), True)

    def _restart_ping_wait(self):
        if self._ping_timer:
            self._ping_timer.cancel()
        self._ping_timer = self._loop.call_later(
            self._ping_interval + self._max_server_ping_delay,
            lambda: asyncio.ensure_future(self._no_ping(), loop=self._loop)
        )

    async def _handle_ping(self):
        logger.debug("received ping from server")
        if self._send_pong:
            logger.debug("respond with pong")
            await self._send_commands([{}])
        self._restart_ping_wait()

    async def _send_commands(self, commands):
        logger.debug("send commands: %s", str(commands))
        commands = self._codec.encode_commands(commands)
        try:
            await self._conn.send(commands)
            return True
        except websockets.ConnectionClosed:
            await self._close()
            return False

    async def _process_unsubscribe(self, channel: str, unsubscribe: dict):
        sub = self._subs.get(channel, None)
        if not sub:
            return

        code = unsubscribe['code']
        if code < 2500:
            asyncio.ensure_future(sub._move_unsubscribed(code, unsubscribe['reason']))
        else:
            asyncio.ensure_future(sub._move_subscribing(code, unsubscribe['reason']))

    async def _process_disconnect(self, disconnect):
        logger.debug("disconnect push received")
        code = disconnect['code']
        reconnect = (3500 <= code < 4000) or (4500 <= code < 5000)
        await self._disconnect(code, disconnect['reason'], reconnect)

    async def _process_reply(self, reply):
        if reply.get('id', 0) > 0:
            self._future_success(reply['id'], reply)
        elif reply.get('push'):
            logger.debug("received push reply %s", str(reply))
            push = reply['push']
            if 'pub' in push:
                await self._process_publication(push['channel'], push['pub'])
            elif 'join' in push:
                await self._process_join(push['channel'], push['join'])
            elif 'leave' in push:
                await self._process_leave(push['channel'], push['leave'])
            elif 'unsubscribe' in push:
                await self._process_unsubscribe(push['channel'], push['unsubscribe'])
            elif 'disconnect' in push:
                await self._process_disconnect(push['disconnect'])
        else:
            await self._handle_ping()

    async def _process_publication(self, channel: str, pub: Any):
        sub = self._subs.get(channel, None)
        if not sub:
            return

        info = pub.get('info', None)
        client_info = self._extract_client_info(info) if info else None

        await sub._events.on_publication(PublicationContext(
            offset=pub.get('offset', 0),
            data=self._decode_data(pub.get('data')),
            info=client_info
        ))

    async def _process_join(self, channel: str, join: Any):
        sub = self._subs.get(channel, None)
        if not sub:
            return

        client_info = self._extract_client_info(join['info'])
        await sub._events.on_join(JoinContext(info=client_info))

    async def _process_leave(self, channel: str, leave: Any):
        sub = self._subs.get(channel, None)
        if not sub:
            return

        client_info = self._extract_client_info(leave['info'])
        await sub._events.on_leave(LeaveContext(info=client_info))

    def _extract_client_info(self, info: Any):
        return ClientInfo(
            client=info.get('client', ''),
            user=info.get('user', ''),
            conn_info=self._decode_data(info.get('conn_info', None)),
            chan_info=self._decode_data(info.get('chan_info', None))
        )

    async def _process_incoming_data(self, message):
        logger.debug("start parsing message: %s", str(message))
        replies = self._codec.decode_replies(message)
        logger.debug("got %d replies", len(replies))
        for reply in replies:
            logger.debug("got reply %s", str(reply))
            await self._process_reply(reply)

    async def _process_messages(self):
        logger.debug("start message processing routine")
        while True:
            if self._messages:
                message = await self._messages.get()
                logger.debug("start processing message: %s", str(message))
                await self._process_incoming_data(message)

    async def _listen(self):
        logger.debug("start reading connection")
        while self._conn.open:
            try:
                result = await self._conn.recv()
                if result:
                    logger.debug("data received, {}".format(result))
                    await self._messages.put(result)
            except websockets.ConnectionClosed:
                break

        logger.debug("stop reading connection")

        # TODO: handle close code/reason.
        ws_code = self._conn.close_code
        ws_reason = self._conn.close_reason
        logger.debug("connection closed, code: %d, reason: %s", ws_code, ws_reason)

        default_code = ConnectingCode.TRANSPORT_CLOSED
        disconnect_code = _code_number(default_code)
        disconnect_reason = _code_message(default_code)
        reconnect = True

        if ws_code < 3000:
            if ws_code == 1009:
                code = DisconnectedCode.MESSAGE_SIZE_LIMIT
                disconnect_code = _code_number(code)
                disconnect_reason = _code_message(code)
        else:
            disconnect_code = ws_code
            disconnect_reason = ws_reason
            if (3500 <= ws_code < 4000) or (4500 <= ws_code < 5000):
                reconnect = False

        await self._disconnect(disconnect_code, disconnect_reason, reconnect)


class SubscriptionState(Enum):
    """
    SubscriptionState represents possible states of subscription.
    """
    UNSUBSCRIBED = 'unsubscribed'
    SUBSCRIBING = 'subscribing'
    SUBSCRIBED = 'subscribed'


class Subscription:
    """
    Subscription describes client subscription to a channel.
    """

    _resubscribe_backoff_factor = 2
    _resubscribe_backoff_jitter = 0.5

    def __init__(
            self,
            client: Client,
            channel: str,
            token: str = '',
            get_token: Optional[Callable[..., Awaitable[None]]] = None,
            min_resubscribe_delay=0.1,
            max_resubscribe_delay=10.0,
    ):
        self.channel = channel
        self._future = asyncio.Future()
        self.state: SubscriptionState = SubscriptionState.UNSUBSCRIBED
        self._subscribed = False
        self._client = client
        self._events = _SubscriptionEventHandler()
        self._min_resubscribe_delay = min_resubscribe_delay
        self._max_resubscribe_delay = max_resubscribe_delay
        self._resubscribe_attempts = 0

    def on_subscribing(self, handler: Callable[[SubscribingContext], Coroutine[None, None, None]]):
        self._events.on_subscribing = handler

    def on_subscribed(self, handler: Callable[[SubscribedContext], Coroutine[None, None, None]]):
        self._events.on_subscribed = handler

    def on_unsubscribed(self, handler: Callable[[UnsubscribedContext], Coroutine[None, None, None]]):
        self._events.on_unsubscribed = handler

    def on_publication(self, handler: Callable[[PublicationContext], Coroutine[None, None, None]]):
        self._events.on_publication = handler

    def on_join(self, handler: Callable[[JoinContext], Coroutine[None, None, None]]):
        self._events.on_join = handler

    def on_leave(self, handler: Callable[[LeaveContext], Coroutine[None, None, None]]):
        self._events.on_leave = handler

    def on_error(self, handler: Callable[[SubscriptionErrorContext], Coroutine[None, None, None]]):
        self._events.on_error = handler

    async def history(self, timeout=None) -> HistoryResult:
        await self._future
        # noinspection PyProtectedMember
        return await self._client.history(self.channel, timeout=timeout)

    async def presence(self, timeout=None) -> PresenceResult:
        await self._future
        # noinspection PyProtectedMember
        return await self._client.presence(self.channel, timeout=timeout)

    async def presence_stats(self, timeout=None) -> PresenceStatsResult:
        await self._future
        # noinspection PyProtectedMember
        return await self._client.presence_stats(self.channel, timeout=timeout)

    async def publish(self, data: BytesOrJSON, timeout=None) -> PublishResult:
        await self._future
        # noinspection PyProtectedMember
        return await self._client.publish(self.channel, data, timeout=timeout)

    async def subscribe(self):
        if self.state == SubscriptionState.SUBSCRIBING:
            return
        self.state = SubscriptionState.SUBSCRIBING

        handler = self._events.on_subscribing
        code = SubscribingCode.SUBSCRIBE_CALLED
        await handler(SubscribingContext(code=_code_number(code), reason=_code_message(code)))

        # noinspection PyProtectedMember
        await self._client._resubscribe(self)

    async def unsubscribe(self):
        if self.state == SubscriptionState.UNSUBSCRIBED:
            return
        self.state = SubscriptionState.UNSUBSCRIBED

        handler = self._events.on_unsubscribed
        code = UnsubscribedCode.UNSUBSCRIBE_CALLED
        await handler(UnsubscribedContext(code=_code_number(code), reason=_code_message(code)))

        # noinspection PyProtectedMember
        await self._client._unsubscribe(self.channel)

    async def _schedule_resubscribe(self):
        if self.state != SubscriptionState.SUBSCRIBING:
            return

        delay = _backoff(self._resubscribe_attempts, self._min_resubscribe_delay, self._max_resubscribe_delay)
        self._resubscribe_attempts += 1
        logger.debug("start resubscribing in %f", delay)
        await asyncio.sleep(delay)
        if self.state != SubscriptionState.SUBSCRIBING:
            return
        # noinspection PyProtectedMember
        await self._client._resubscribe(self)

    async def _move_unsubscribed(self, code: int, message: str):
        if self.state == SubscriptionState.UNSUBSCRIBED:
            return

        self.state = SubscriptionState.UNSUBSCRIBED

        handler = self._events.on_unsubscribed
        await handler(UnsubscribedContext(
            code=code,
            reason=message
        ))

    async def _move_subscribing(self, code: int, message: str):
        if self.state == SubscriptionState.SUBSCRIBING:
            return

        if self.state == SubscriptionState.SUBSCRIBED:
            # TODO: implement.
            # self._clear_subscribed_state()
            pass

        self.state = SubscriptionState.SUBSCRIBING

        handler = self._events.on_subscribing
        await handler(SubscribingContext(
            code=code,
            reason=message
        ))

        asyncio.ensure_future(self._client._resubscribe(self))

    async def _move_subscribed(self, subscribe):
        handler = self._events.on_subscribed
        recoverable = subscribe.get('recoverable', False)
        positioned = subscribe.get('positioned', False)
        stream_position = None
        if positioned or recoverable:
            stream_position = StreamPosition(
                offset=subscribe.get('offset', 0),
                epoch=subscribe.get('epoch', '')
            )
        await handler(SubscribedContext(
            channel=self.channel,
            recoverable=recoverable,
            positioned=positioned,
            stream_position=stream_position,
            was_recovering=subscribe.get('was_recovering', False),
            recovered=subscribe.get('recovered', False),
            data=self._client._decode_data(subscribe.get('data', None))
        ))

        publications = subscribe.get("publications", [])
        if publications:
            handler = self._events.on_publication
            for pub in publications:
                info = pub.get('info', None)
                client_info = self._client._extract_client_info(info) if info else None
                await handler(PublicationContext(
                    offset=pub.get('offset', 0),
                    data=self._client._decode_data(pub.get('data')),
                    info=client_info,
                ))

        self._resubscribe_attempts = 0
