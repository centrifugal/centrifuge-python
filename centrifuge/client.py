import asyncio
import base64
import contextlib
import logging
from asyncio import TimerHandle
from contextlib import asynccontextmanager
from dataclasses import dataclass
from enum import Enum
from typing import (
    TYPE_CHECKING,
    Any,
    Awaitable,
    Dict,
    Optional,
    Union,
    List,
    Callable,
)

import websockets
from websockets import exceptions
from websockets.protocol import State

from centrifuge.codecs import _JsonCodec, _ProtobufCodec
from centrifuge.codes import (
    _ConnectingCode,
    _DisconnectedCode,
    _ErrorCode,
    _SubscribingCode,
    _UnsubscribedCode,
)
from centrifuge.contexts import (
    ConnectedContext,
    ConnectingContext,
    DisconnectedContext,
    ErrorContext,
    JoinContext,
    LeaveContext,
    PublicationContext,
    SubscribedContext,
    SubscribingContext,
    SubscriptionErrorContext,
    UnsubscribedContext,
    ServerSubscribedContext,
    ServerPublicationContext,
    ServerUnsubscribedContext,
    ServerSubscribingContext,
    ServerJoinContext,
    ServerLeaveContext,
)
from centrifuge.exceptions import (
    CentrifugeError,
    ClientDisconnectedError,
    OperationTimeoutError,
    DuplicateSubscriptionError,
    ReplyError,
    SubscriptionUnsubscribedError,
    UnauthorizedError,
)
from centrifuge.handlers import (
    ClientEventHandler,
    SubscriptionEventHandler,
)
from centrifuge.types import (
    ClientInfo,
    HistoryResult,
    PresenceResult,
    PresenceStatsResult,
    Publication,
    PublishResult,
    RpcResult,
    StreamPosition,
)
from centrifuge.utils import (
    _backoff,
    _code_message,
    _code_number,
    _is_token_expired,
    _wait_for_future,
)

if TYPE_CHECKING:
    # Turned out legacy is not really legacy in websockets.
    # See more in https://websockets.readthedocs.io/en/stable/faq/ (grep "legacy").
    from websockets.legacy.client import WebSocketClientProtocol
    from asyncio import AbstractEventLoop

logger = logging.getLogger("centrifuge")


class ClientState(Enum):
    """ClientState represents possible states of Client connection."""

    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    CONNECTED = "connected"


class SubscriptionState(Enum):
    """SubscriptionState represents possible states of Subscription."""

    UNSUBSCRIBED = "unsubscribed"
    SUBSCRIBING = "subscribing"
    SUBSCRIBED = "subscribed"


@dataclass
class _Callback:
    future: asyncio.Future
    done: Optional[asyncio.Future]
    timeout: Optional[asyncio.TimerHandle]


@dataclass
class _ServerSubscription:
    offset: int
    epoch: str
    recoverable: bool


class DeltaType(Enum):
    FOSSIL = "fossil"

    def __str__(self) -> str:
        return self.value


class Client:
    """Client is a websocket client to Centrifuge/Centrifugo server."""

    _reconnect_backoff_factor = 2
    _reconnect_backoff_jitter = 0.5

    def __init__(
        self,
        address: str,
        events: Optional[ClientEventHandler] = None,
        token: str = "",
        get_token: Optional[Callable[[], Awaitable[str]]] = None,
        use_protobuf: bool = False,
        timeout: float = 5.0,
        max_server_ping_delay: float = 10.0,
        name: str = "python",
        version: str = "",
        data: Any = None,
        min_reconnect_delay: float = 0.1,
        max_reconnect_delay: float = 20.0,
        headers: Optional[Dict[str, str]] = None,
        loop: Optional["AbstractEventLoop"] = None,
    ):
        """Initializes new Client instance.

        Client can be in one of 3 states:
            - disconnected (initial state, or after disconnect called, or terminal disconnect code
                            received from server)
            - connecting (when connect called, or when automatic reconnection is in progress)
            - connected (after successful connect)
        See more details about SDK behaviour in https://centrifugal.dev/docs/transports/client_api.
        """
        self.state: ClientState = ClientState.DISCONNECTED
        self.events: ClientEventHandler = events or ClientEventHandler()
        self._address: str = address
        self._use_protobuf: bool = use_protobuf
        self._codec: Union[
            _ProtobufCodec,
            _JsonCodec,
        ] = _ProtobufCodec() if use_protobuf else _JsonCodec()
        self._conn: Optional["WebSocketClientProtocol"] = None
        self._id: int = 0
        self._subs: Dict[str, Subscription] = {}
        self._messages: asyncio.Queue = asyncio.Queue()
        self._delay: float = 0
        self._need_reconnect: bool = True
        self._name: str = name
        self._version: str = version
        self._data: Any = data
        self._timeout: float = timeout
        self._min_reconnect_delay: float = min_reconnect_delay
        self._max_reconnect_delay: float = max_reconnect_delay
        self._send_pong: bool = True
        self._ping_interval: int = 0
        self._max_server_ping_delay: float = max_server_ping_delay
        self._ping_timer = None
        self._refresh_timer = None
        self._connected_future = asyncio.Future()
        self._token = token
        self._get_token = get_token
        self._loop = loop or asyncio.get_event_loop()
        self._inflight_commands: Dict[int, _Callback] = {}
        self._reconnect_attempts = 0
        self._reconnect_timer = None
        self._headers = headers or {}
        self._server_subs: Dict[str, _ServerSubscription] = {}

    def subscriptions(self) -> Dict[str, "Subscription"]:
        """Returns a copy of subscriptions dict."""
        return self._subs.copy()

    def new_subscription(
        self,
        channel: str,
        events: Optional[SubscriptionEventHandler] = None,
        token: str = "",
        get_token: Optional[Callable[[str], Awaitable[str]]] = None,
        data: Optional[Any] = None,
        min_resubscribe_delay=0.1,
        max_resubscribe_delay=10.0,
        positioned: bool = False,
        recoverable: bool = False,
        join_leave: bool = False,
        delta: Optional[DeltaType] = None,
    ) -> "Subscription":
        """Creates new subscription to channel. If subscription already exists then
        DuplicateSubscriptionError exception will be raised.
        """
        if self.get_subscription(channel):
            raise DuplicateSubscriptionError(
                'subscription to channel "' + channel + '" is already registered',
            )
        sub = Subscription._create_instance(
            self,
            channel,
            events=events,
            token=token,
            get_token=get_token,
            data=data,
            min_resubscribe_delay=min_resubscribe_delay,
            max_resubscribe_delay=max_resubscribe_delay,
            positioned=positioned,
            recoverable=recoverable,
            join_leave=join_leave,
            delta=delta,
        )
        self._subs[channel] = sub
        return sub

    def get_subscription(self, channel: str) -> Optional["Subscription"]:
        """Returns subscription by channel name.

        If subscription does not exist, then None returned.
        """
        return self._subs.get(channel)

    def remove_subscription(self, sub: "Subscription"):
        """Removes subscription from client internal registry.

        Subscription is not usable after this call.
        """
        if not sub:
            return

        if sub.state != SubscriptionState.UNSUBSCRIBED:
            raise CentrifugeError("can not remove subscription in non-unsubscribed state")

        sub._client = None
        del self._subs[sub.channel]

    async def _close_transport_conn(self) -> None:
        if not self._conn:
            return
        with contextlib.suppress(websockets.ConnectionClosed):
            await self._conn.close()

    async def _schedule_reconnect(self) -> None:
        if self.state == ClientState.CONNECTED:
            return

        if self._conn and self._conn.open:
            return

        if not self._need_reconnect:
            logger.debug("won't reconnect")
            return

        self.state = ClientState.CONNECTING

        delay = _backoff(
            self._reconnect_attempts,
            self._min_reconnect_delay,
            self._max_reconnect_delay,
        )
        self._reconnect_attempts += 1
        logger.debug("start reconnecting in %f", delay)
        self._reconnect_timer = self._loop.call_later(
            delay,
            lambda: asyncio.ensure_future(self._reconnect()),
        )

    async def _reconnect(self) -> None:
        if self.state != ClientState.CONNECTING:
            return
        await self._create_connection()

    def _next_command_id(self) -> int:
        self._id += 1
        return self._id

    async def _create_connection(self) -> bool:
        if self.state != ClientState.CONNECTING:
            return False
        subprotocols = []
        if self._use_protobuf:
            subprotocols = ["centrifuge-protobuf"]
        try:
            self._conn = await websockets.connect(
                self._address,
                subprotocols=subprotocols,
                extra_headers=self._headers,
            )
        except (OSError, exceptions.WebSocketException) as e:
            handler = self.events.on_error
            await handler(ErrorContext(code=_code_number(_ErrorCode.TRANSPORT_CLOSED), error=e))
            asyncio.ensure_future(self._schedule_reconnect())
            return False

        if not self._token and self._get_token:
            try:
                token = await self._get_token()
            except Exception as e:
                if isinstance(e, UnauthorizedError):
                    code = _DisconnectedCode.UNAUTHORIZED
                    await self._disconnect(_code_number(code), _code_message(code), False)
                    return False
                await self._close_transport_conn()
                handler = self.events.on_error
                await handler(
                    ErrorContext(code=_code_number(_ErrorCode.CLIENT_CONNECT_TOKEN), error=e),
                )
                asyncio.ensure_future(self._schedule_reconnect())
                return False

            self._token = token

        if self.state != ClientState.CONNECTING:
            return False

        asyncio.ensure_future(self._listen())
        asyncio.ensure_future(self._process_messages())

        self._delay = self._min_reconnect_delay

        cmd_id = self._next_command_id()
        command = self._construct_connect_command(cmd_id)
        async with self._register_future_with_done(cmd_id) as future:
            await self._send_commands([command])

            try:
                reply = await future
            except OperationTimeoutError as e:
                await self._close_transport_conn()
                handler = self.events.on_error
                await handler(ErrorContext(code=_code_number(_ErrorCode.TIMEOUT), error=e))
                await self._schedule_reconnect()
                return False
            except Exception as e:
                if self.state != ClientState.CONNECTING:
                    return False
                await self._close_transport_conn()
                handler = self.events.on_error
                await handler(
                    ErrorContext(code=_code_number(_ErrorCode.CONNECT_ERROR), error=e),
                )
                await self._schedule_reconnect()
                return False

            if self.state != ClientState.CONNECTING:
                return False

            if reply.get("error"):
                logger.debug("connect reply has error: %s", reply.get("error"))
                code, message, temporary = self._extract_error_details(reply)
                if _is_token_expired(code):
                    temporary = True
                    self._token = ""
                if temporary:
                    await self._close_transport_conn()
                    handler = self.events.on_error
                    await handler(
                        ErrorContext(
                            code=_code_number(_ErrorCode.CONNECT_ERROR),
                            error=ReplyError(code, message, temporary),
                        ),
                    )
                    await self._schedule_reconnect()
                    return False
                else:
                    await self._disconnect(code, message, False)
                    return False
            else:
                connect = reply["connect"]
                self.client_id = connect["client"]
                self.state = ClientState.CONNECTED
                self._send_pong = connect.get("pong", False)
                self._ping_interval = connect.get("ping", 0)
                if self._ping_interval > 0:
                    self._restart_ping_wait()

                expires = connect.get("expires", False)
                if expires:
                    ttl = connect["ttl"]
                    self._refresh_timer = self._loop.call_later(
                        ttl,
                        lambda: asyncio.ensure_future(self._refresh(), loop=self._loop),
                    )

                self._connected_future.set_result(True)

                handler = self.events.on_connected
                await handler(
                    ConnectedContext(
                        client=connect.get("client"),
                        version=connect.get("version", ""),
                        data=self._decode_data(connect.get("data")),
                    ),
                )

                self._clear_connecting_state()

                channels = self._subs.keys()
                for channel in channels:
                    sub = self._subs[channel]
                    if not sub or sub.state != SubscriptionState.SUBSCRIBING:
                        continue
                    asyncio.ensure_future(self._subscribe(channel))

                await self._process_server_subs(connect.get("subs", {}))

    def _construct_connect_command(self, cmd_id: int) -> Dict[str, Any]:
        connect = {}

        if self._token:
            connect["token"] = self._token

        if self._data:
            connect["data"] = self._encode_data(self._data)

        if self._name:
            connect["name"] = self._name

        if self._version:
            connect["version"] = self._version

        subs = {}
        for channel, sub in self._server_subs.items():
            if sub.recoverable:
                subs[channel] = {
                    "recover": True,
                    "offset": sub.offset,
                    "epoch": sub.epoch,
                }
        if subs:
            connect["subs"] = subs

        command = {
            "id": cmd_id,
            "connect": connect,
        }
        return command

    async def _process_server_subs(self, subs: Dict[str, Dict[str, Any]]):
        logger.debug("process server subs: %s", subs)
        for channel, subscribe in subs.items():
            self._server_subs[channel] = _ServerSubscription(
                offset=subscribe.get("offset", 0),
                epoch=subscribe.get("epoch", ""),
                recoverable=subscribe.get("recoverable", False),
            )
            recoverable = subscribe.get("recoverable", False)
            positioned = subscribe.get("positioned", False)
            stream_position = None
            if positioned or recoverable:
                stream_position = StreamPosition(
                    offset=int(subscribe.get("offset", 0)),
                    epoch=subscribe.get("epoch", ""),
                )

            handler = self.events.on_subscribed
            await handler(
                ServerSubscribedContext(
                    channel=channel,
                    recoverable=recoverable,
                    positioned=positioned,
                    stream_position=stream_position,
                    was_recovering=subscribe.get("was_recovering", False),
                    recovered=subscribe.get("recovered", False),
                    data=self._decode_data(subscribe.get("data")),
                ),
            )

        for channel, subscribe in subs.items():
            if subscribe.get("recovered", False):
                pubs = subscribe.get("publications", [])
                for pub in pubs:
                    await self._process_server_publication(channel, pub)

        channels_to_delete = []
        for channel in self._server_subs:
            if subs.get(channel) is None:  # using None is important here to deal with {} too.
                channels_to_delete.append(channel)
                handler = self.events.on_unsubscribed
                await handler(ServerUnsubscribedContext(channel=channel))

        for channel in channels_to_delete:
            del self._server_subs[channel]

    async def _process_server_publication(self, channel: str, pub: Any):
        publication = self._publication_from_proto(pub)
        await self.events.on_publication(
            ServerPublicationContext(channel=channel, pub=publication)
        )
        if publication.offset > 0:
            self._server_subs[channel].offset = publication.offset

    def _clear_connecting_state(self) -> None:
        self._reconnect_attempts = 0
        if self._reconnect_timer:
            logger.debug("clear reconnect timer")
            self._reconnect_timer.cancel()
            self._reconnect_timer = None

    async def _clear_connected_state(self) -> None:
        for sub in self._subs.values():
            if sub.state == SubscriptionState.SUBSCRIBED:
                unsubscribe_code = _SubscribingCode.TRANSPORT_CLOSED
                await sub._move_subscribing(
                    code=_code_number(unsubscribe_code),
                    reason=_code_message(unsubscribe_code),
                    skip_schedule_resubscribe=True,
                )

        for channel in self._server_subs:
            handler = self.events.on_subscribing
            await handler(ServerSubscribingContext(channel=channel))

    async def connect(self) -> None:
        """Initiate connection to server."""
        if self.state in {ClientState.CONNECTING, ClientState.CONNECTED}:
            return

        self.state = ClientState.CONNECTING
        if self._connected_future.done():
            self._connected_future = asyncio.Future()

        handler = self.events.on_connecting
        code = _ConnectingCode.CONNECT_CALLED
        await handler(ConnectingContext(code=_code_number(code), reason=_code_message(code)))
        await self._create_connection()

    async def disconnect(self) -> None:
        """Disconnect from server."""
        if self.state == ClientState.DISCONNECTED:
            return

        code = _DisconnectedCode.DISCONNECT_CALLED
        await self._disconnect(_code_number(code), _code_message(code), False)

    async def _refresh(self) -> None:
        cmd_id = self._next_command_id()

        try:
            token = await self._get_token()
        except Exception as e:
            if isinstance(e, UnauthorizedError):
                code = _DisconnectedCode.UNAUTHORIZED
                await self._disconnect(_code_number(code), _code_message(code), False)
                return
            handler = self.events.on_error
            await handler(
                ErrorContext(code=_code_number(_ErrorCode.CLIENT_REFRESH_TOKEN), error=e),
            )
            return

        self._token = token
        command = {
            "id": cmd_id,
            "refresh": {
                "token": token,
            },
        }
        future = self._register_future(cmd_id, self._timeout)
        await self._send_commands([command])

        try:
            reply = await future
        except Exception as e:
            handler = self.events.on_error
            await handler(
                ErrorContext(code=_code_number(_ErrorCode.CLIENT_REFRESH_TOKEN), error=e),
            )
            return

        if reply.get("error"):
            code, message, temporary = self._extract_error_details(reply)
            handler = self.events.on_error
            await handler(
                ErrorContext(
                    code=_code_number(_ErrorCode.CLIENT_REFRESH_TOKEN),
                    error=ReplyError(code, message, temporary),
                ),
            )
            return

        refresh = reply["refresh"]
        expires = refresh.get("expires", False)
        if expires:
            ttl = refresh["ttl"]
            self._refresh_timer = self._loop.call_later(
                ttl,
                lambda: asyncio.ensure_future(self._refresh(), loop=self._loop),
            )

    async def _sub_refresh(self, channel: str):
        sub = self._subs.get(channel)
        if not sub:
            return

        try:
            token = await sub._get_token(channel)
        except Exception as e:
            if isinstance(e, UnauthorizedError):
                code = _UnsubscribedCode.UNAUTHORIZED
                await sub._move_unsubscribed(
                    _code_number(code),
                    _code_message(code),
                    send_unsubscribe_command=True,
                )
                return
            handler = sub.events.on_error
            await handler(
                SubscriptionErrorContext(
                    code=_code_number(_ErrorCode.SUBSCRIPTION_SUBSCRIBE_TOKEN),
                    error=e,
                ),
            )
            asyncio.ensure_future(sub._schedule_resubscribe())
            return

        cmd_id = self._next_command_id()
        sub._token = token
        command = {
            "id": cmd_id,
            "sub_refresh": {
                "token": token,
            },
        }

        future = self._register_future(cmd_id, self._timeout)
        await self._send_commands([command])

        try:
            reply = await future
        except Exception as e:
            handler = sub.events.on_error
            await handler(
                SubscriptionErrorContext(
                    code=_code_number(_ErrorCode.SUBSCRIPTION_REFRESH_TOKEN),
                    error=e,
                ),
            )
            return

        if reply.get("error"):
            code, message, temporary = self._extract_error_details(reply)
            handler = sub.events.on_error
            await handler(
                SubscriptionErrorContext(
                    code=_code_number(_ErrorCode.SUBSCRIPTION_REFRESH_TOKEN),
                    error=ReplyError(code, message, temporary),
                ),
            )
            return

        sub_refresh = reply["sub_refresh"]
        expires = sub_refresh.get("expires", False)
        if expires:
            ttl = sub_refresh["ttl"]
            sub._refresh_timer = self._loop.call_later(
                ttl,
                lambda: asyncio.ensure_future(sub._refresh(), loop=self._loop),
            )

    @staticmethod
    def _extract_error_details(reply):
        error = reply["error"]
        return error["code"], error["message"], error.get("temporary", False)

    async def _subscribe(self, channel):
        sub = self._subs.get(channel)
        if not sub:
            return None

        if self.state != ClientState.CONNECTED:
            logger.debug("skip subscribe to %s until connected", channel)
            return None

        logger.debug("subscribe to channel %s", channel)

        if not sub._token and sub._get_token:
            try:
                token = await sub._get_token(channel)
            except Exception as e:
                if isinstance(e, UnauthorizedError):
                    code = _UnsubscribedCode.UNAUTHORIZED
                    await sub._move_unsubscribed(_code_number(code), _code_message(code))
                    return False
                handler = sub.events.on_error
                await handler(
                    SubscriptionErrorContext(
                        code=_code_number(_ErrorCode.SUBSCRIPTION_SUBSCRIBE_TOKEN),
                        error=e,
                    ),
                )
                asyncio.ensure_future(sub._schedule_resubscribe())
                return False

            sub._token = token

        cmd_id = self._next_command_id()
        command = self._construct_subscribe_command(sub, cmd_id)
        async with self._register_future_with_done(cmd_id) as future:
            await self._send_commands([command])
            try:
                reply = await future
            except OperationTimeoutError as e:
                if sub.state != SubscriptionState.SUBSCRIBING:
                    return None
                handler = sub.events.on_error
                await handler(
                    SubscriptionErrorContext(code=_code_number(_ErrorCode.TIMEOUT), error=e),
                )
                await sub._schedule_resubscribe()
                return None
            except Exception as e:
                if sub.state != SubscriptionState.SUBSCRIBING:
                    return None
                handler = sub.events.on_error
                await handler(
                    SubscriptionErrorContext(
                        code=_code_number(_ErrorCode.SUBSCRIBE_ERROR),
                        error=e,
                    ),
                )
                await sub._schedule_resubscribe()
                return None

            if sub.state != SubscriptionState.SUBSCRIBING:
                return None

            if reply.get("error"):
                logger.debug("subscribe reply has error: %s", reply.get("error"))
                code, message, temporary = self._extract_error_details(reply)
                if _is_token_expired(code):
                    temporary = True
                    sub._token = ""
                if temporary:
                    handler = sub.events.on_error
                    await handler(
                        SubscriptionErrorContext(
                            code=_code_number(_ErrorCode.SUBSCRIBE_ERROR),
                            error=ReplyError(code, message, temporary),
                        ),
                    )
                    await sub._schedule_resubscribe()
                else:
                    await sub._move_unsubscribed(code, message)
            else:
                await sub._move_subscribed(reply["subscribe"])

    def _construct_subscribe_command(self, sub: "Subscription", cmd_id: int) -> Dict[str, Any]:
        subscribe = {
            "channel": sub.channel,
        }

        if sub._token:
            subscribe["token"] = sub._token

        if sub._data:
            subscribe["data"] = self._encode_data(sub._data)

        if sub._positioned:
            subscribe["positioned"] = True

        if sub._recoverable:
            subscribe["recoverable"] = True

        if sub._join_leave:
            subscribe["join_leave"] = True

        if sub._need_recover():
            subscribe["recover"] = True
            subscribe["epoch"] = sub._epoch
            subscribe["offset"] = sub._offset

        if sub._delta:
            subscribe["delta"] = sub._delta.value

        command = {
            "id": cmd_id,
            "subscribe": subscribe,
        }
        return command

    async def _resubscribe(self, sub: "Subscription"):
        self._subs[sub.channel] = sub
        asyncio.ensure_future(self._subscribe(sub.channel))

    async def _unsubscribe(self, channel: str):
        sub = self._subs.get(channel)
        if not sub:
            return

        unsubscribe = {"channel": sub.channel}

        cmd_id = self._next_command_id()
        command = {
            "id": cmd_id,
            "unsubscribe": unsubscribe,
        }
        future = self._register_future(cmd_id, self._timeout)

        await self._send_commands([command])

        try:
            await future
        except OperationTimeoutError:
            code = _ConnectingCode.UNSUBSCRIBE_ERROR
            await self._disconnect(_code_number(code), _code_message(code), True)
            return

    def _register_future(self, cmd_id: int, timeout: float) -> asyncio.Future:
        future = asyncio.Future()

        th = None
        if timeout:

            def cb():
                if not future.done():
                    self._future_error(cmd_id, OperationTimeoutError())

            th = self._loop.call_later(timeout, cb)

        self._inflight_commands[cmd_id] = _Callback(
            future=future,
            done=None,
            timeout=th,
        )

        return future

    @asynccontextmanager
    async def _register_future_with_done(self, cmd_id: int):
        """This is an asynccontextmanager version of _register_future. It's required to
        wait for the full processing of connect/subscribe replies before applying any
        async pushes coming from the server. This way we can be sure that publications
        processed in order by the application.
        """
        future = asyncio.Future()
        done = asyncio.Future()

        th = None
        if self._timeout:

            def cb():
                if not future.done():
                    self._future_error(cmd_id, OperationTimeoutError())

            th = self._loop.call_later(self._timeout, cb)

        cb = _Callback(
            future=future,
            done=done,
            timeout=th,
        )

        self._inflight_commands[cmd_id] = cb

        try:
            yield future
        finally:
            if not done.done():
                done.set_result(True)

    def _future_error(self, cmd_id: int, exc: Exception):
        cb = self._inflight_commands.get(cmd_id)
        if not cb:
            return
        cb.future.set_exception(exc)
        if cb.done:
            cb.done.set_result(True)
        del self._inflight_commands[cmd_id]

    async def _future_success(self, cmd_id: int, reply):
        cb = self._inflight_commands.get(cmd_id)
        if not cb:
            return
        future = cb.future
        future.set_result(reply)
        del self._inflight_commands[cmd_id]
        if cb.timeout:
            cb.timeout.cancel()
        if cb.done:
            await cb.done

    def _decode_data(self, data: Any):
        if data is None:
            return None
        if self._use_protobuf:
            if isinstance(data, str):
                return base64.b64decode(data)
            return data
        else:
            return data

    def _encode_data(self, data: Any):
        if self._use_protobuf:
            if not isinstance(data, bytes):
                raise CentrifugeError(
                    "when using Protobuf protocol you must encode payloads to bytes",
                )
            return base64.b64encode(data)
        else:
            if isinstance(data, bytes):
                raise CentrifugeError(
                    "when using JSON protocol you can not encode payloads to bytes",
                )
            return data

    @staticmethod
    def _check_reply_error(reply) -> None:
        if reply.get("error"):
            error = reply["error"]
            raise ReplyError(error["code"], error["message"], error.get("temporary", False))

    def _check_state(self):
        if self.state != ClientState.CONNECTED:
            raise ClientDisconnectedError("client disconnected")

    async def ready(self, timeout: Optional[float] = None) -> None:
        self._check_state()
        completed = await _wait_for_future(
            self._connected_future,
            timeout=timeout or self._timeout,
        )
        if not completed:
            raise OperationTimeoutError("timeout waiting for connection to be ready")

    async def publish(
        self,
        channel: str,
        data: Any,
        timeout: Optional[float] = None,
    ) -> PublishResult:
        await self.ready()
        cmd_id = self._next_command_id()
        command = {
            "id": cmd_id,
            "publish": {
                "channel": channel,
                "data": self._encode_data(data),
            },
        }
        future = self._register_future(cmd_id, timeout or self._timeout)
        await self._send_commands([command])
        reply = await future
        self._check_reply_error(reply)
        return PublishResult()

    async def history(
        self,
        channel: str,
        limit: int = 0,
        since: Optional[StreamPosition] = None,
        reverse: bool = False,
        timeout: Optional[float] = None,
    ) -> HistoryResult:
        await self.ready()
        history = {
            "channel": channel,
            "limit": limit,
            "reverse": reverse,
        }
        if since:
            history["since"] = {
                "offset": since.offset,
                "epoch": since.epoch,
            }

        cmd_id = self._next_command_id()
        command = {
            "id": cmd_id,
            "history": history,
        }
        future = self._register_future(cmd_id, timeout or self._timeout)
        await self._send_commands([command])
        reply = await future
        self._check_reply_error(reply)

        publications = []
        for pub in reply["history"].get("publications", []):
            publication = self._publication_from_proto(pub)
            publications.append(publication)

        return HistoryResult(
            epoch=reply["history"].get("epoch", ""),
            offset=int(reply["history"].get("offset", 0)),
            publications=publications,
        )

    async def presence(self, channel: str, timeout: Optional[float] = None) -> PresenceResult:
        await self.ready()
        cmd_id = self._next_command_id()
        command = {
            "id": cmd_id,
            "presence": {
                "channel": channel,
            },
        }
        future = self._register_future(cmd_id, timeout or self._timeout)
        await self._send_commands([command])
        reply = await future
        self._check_reply_error(reply)

        clients = {}
        for k, v in reply["presence"].get("presence", {}).items():
            clients[k] = ClientInfo(
                client=v.get("client", ""),
                user=v.get("user", ""),
                conn_info=self._decode_data(v.get("conn_info")),
                chan_info=self._decode_data(v.get("chan_info")),
            )

        return PresenceResult(
            clients=clients,
        )

    async def presence_stats(
        self,
        channel: str,
        timeout: Optional[float] = None,
    ) -> PresenceStatsResult:
        await self.ready()
        cmd_id = self._next_command_id()
        command = {
            "id": cmd_id,
            "presence_stats": {
                "channel": channel,
            },
        }
        future = self._register_future(cmd_id, timeout or self._timeout)
        await self._send_commands([command])
        reply = await future
        self._check_reply_error(reply)
        return PresenceStatsResult(
            num_clients=reply["presence_stats"].get("num_clients", 0),
            num_users=reply["presence_stats"].get("num_users", 0),
        )

    async def rpc(
        self,
        method: str,
        data: Any,
        timeout: Optional[float] = None,
    ) -> RpcResult:
        await self.ready()
        cmd_id = self._next_command_id()
        command = {
            "id": cmd_id,
            "rpc": {
                "method": method,
                "data": self._encode_data(data),
            },
        }
        future = self._register_future(cmd_id, timeout or self._timeout)
        await self._send_commands([command])
        reply = await future
        self._check_reply_error(reply)
        return RpcResult(
            data=self._decode_data(reply["rpc"].get("data")),
        )

    def _clear_outgoing_futures(self) -> None:
        command_ids = []
        for cmd_id in self._inflight_commands:
            command_ids.append(cmd_id)
        for cmd_id in command_ids:
            self._future_error(
                cmd_id,
                ClientDisconnectedError(f"command {cmd_id} canceled due to disconnect"),
            )

    async def _disconnect(self, code: int, reason: str, reconnect: bool) -> None:
        if self._ping_timer:
            logger.debug("canceling ping timer")
            self._ping_timer.cancel()
            self._ping_timer = None

        if self._refresh_timer:
            logger.debug("canceling refresh timer")
            self._refresh_timer.cancel()
            self._refresh_timer = None

        self._clear_connecting_state()

        transition_from_connected = self.state == ClientState.CONNECTED

        await self._messages.put(None)
        self._messages = asyncio.Queue()

        if not reconnect:
            self._need_reconnect = False

        if self.state == ClientState.DISCONNECTED:
            return

        self._clear_outgoing_futures()

        if self._connected_future.done():
            self._connected_future = asyncio.Future()

        if reconnect:
            self.state = ClientState.CONNECTING
        else:
            self._connected_future.set_exception(
                ClientDisconnectedError(f"client disconnected: {code} ({reason})"),
            )
            await self._consume_connected_future()
            self.state = ClientState.DISCONNECTED

        if self._conn and self._conn.state != State.CLOSED:
            await self._close_transport_conn()

        if transition_from_connected:
            await self._clear_connected_state()

        handler = self.events.on_disconnected
        await handler(DisconnectedContext(code=code, reason=reason))

        if reconnect:
            asyncio.ensure_future(self._schedule_reconnect())

    async def _consume_connected_future(self) -> None:
        with contextlib.suppress(CentrifugeError):
            await self._connected_future

    async def _no_ping(self) -> None:
        code = _ConnectingCode.NO_PING
        await self._disconnect(_code_number(code), _code_message(code), True)

    def _restart_ping_wait(self) -> None:
        if self._ping_timer:
            self._ping_timer.cancel()
        self._ping_timer = self._loop.call_later(
            self._ping_interval + self._max_server_ping_delay,
            lambda: asyncio.ensure_future(self._no_ping(), loop=self._loop),
        )

    async def _handle_ping(self) -> None:
        logger.debug("received ping from server")
        if self._send_pong:
            logger.debug("respond with pong")
            await self._send_commands([{}])
        self._restart_ping_wait()

    async def _send_commands(
        self,
        commands: List[Dict[str, Any]],
    ) -> None:
        if self._conn is None:
            raise CentrifugeError("connection is not initialized")
        logger.debug("send commands: %s", str(commands))
        commands = self._codec.encode_commands(commands)
        try:
            await self._conn.send(commands)
        except exceptions.ConnectionClosed:
            code = _ConnectingCode.TRANSPORT_CLOSED
            await self._disconnect(_code_number(code), _code_message(code), True)

    async def _process_unsubscribe(self, channel: str, unsubscribe: Dict[str, Any]) -> None:
        sub = self._subs.get(channel)
        code = unsubscribe["code"]
        if sub:
            if code < 2500:
                asyncio.ensure_future(sub._move_unsubscribed(code, unsubscribe["reason"]))
            else:
                asyncio.ensure_future(sub._move_subscribing(code, unsubscribe["reason"]))
        else:
            server_sub = self._server_subs.get(channel)
            if server_sub:
                del self._server_subs[channel]
                handler = self.events.on_unsubscribed
                await handler(ServerUnsubscribedContext(channel=channel))

    async def _process_disconnect(self, disconnect: Dict[str, Any]) -> None:
        logger.debug("disconnect push received")
        code = disconnect["code"]
        reconnect = (3500 <= code < 4000) or (4500 <= code < 5000)
        await self._disconnect(code, disconnect["reason"], reconnect)

    async def _process_reply(self, reply: Dict[str, Any]) -> None:
        if reply.get("id", 0) > 0:
            await self._future_success(reply["id"], reply)
        elif reply.get("push"):
            logger.debug("received push reply %s", str(reply))
            push = reply["push"]
            if "pub" in push:
                await self._process_publication(push["channel"], push["pub"])
            elif "join" in push:
                await self._process_join(push["channel"], push["join"])
            elif "leave" in push:
                await self._process_leave(push["channel"], push["leave"])
            elif "unsubscribe" in push:
                await self._process_unsubscribe(push["channel"], push["unsubscribe"])
            elif "disconnect" in push:
                await self._process_disconnect(push["disconnect"])
            else:
                logger.debug("skip unknown push reply %s", str(reply))
        else:
            await self._handle_ping()

    async def _process_publication(self, channel: str, pub: Any) -> None:
        sub = self._subs.get(channel)
        if sub:
            await sub._process_publication(pub)
        else:
            server_sub = self._server_subs.get(channel)
            if server_sub:
                await self._process_server_publication(channel, pub)

    async def _process_join(self, channel: str, join: Any) -> None:
        client_info = self._extract_client_info(join["info"])
        sub = self._subs.get(channel)
        if sub:
            await sub.events.on_join(JoinContext(info=client_info))
        else:
            server_sub = self._server_subs.get(channel)
            if server_sub:
                await self.events.on_join(ServerJoinContext(channel=channel, info=client_info))

    async def _process_leave(self, channel: str, leave: Any) -> None:
        client_info = self._extract_client_info(leave["info"])
        sub = self._subs.get(channel)
        if sub:
            await sub.events.on_leave(LeaveContext(info=client_info))
        else:
            server_sub = self._server_subs.get(channel)
            if server_sub:
                await self.events.on_leave(ServerLeaveContext(channel=channel, info=client_info))

    def _extract_client_info(self, info: Any) -> ClientInfo:
        return ClientInfo(
            client=info.get("client", ""),
            user=info.get("user", ""),
            conn_info=self._decode_data(info.get("conn_info")),
            chan_info=self._decode_data(info.get("chan_info")),
        )

    async def _process_incoming_data(self, message: bytes) -> None:
        logger.debug("start parsing message: %s", str(message))
        replies = self._codec.decode_replies(message)
        logger.debug("got %d replies", len(replies))
        for reply in replies:
            logger.debug("got reply %s", str(reply))
            await self._process_reply(reply)

    async def _process_messages(self) -> None:
        logger.debug("start message processing routine")
        while True:
            if self._messages:
                message = await self._messages.get()
                if message is None:
                    break
                logger.debug("start processing message: %s", str(message))
                await self._process_incoming_data(message)
        logger.debug("stop message processing routine")

    async def _listen(self) -> None:
        logger.debug("start reading connection")
        if self._conn is None:
            raise CentrifugeError("connection is not initialized")

        while self._conn.open:
            try:
                result = await self._conn.recv()
                if result:
                    logger.debug("data received %s", result)
                    await self._messages.put(result)
            except exceptions.ConnectionClosed:
                break

        logger.debug("stop reading connection")

        ws_code = self._conn.close_code
        ws_reason = self._conn.close_reason
        if not ws_code:
            ws_code = 0
            ws_reason = ""
        logger.debug("connection closed, code: %d, reason: %s", ws_code, ws_reason)

        default_code = _ConnectingCode.TRANSPORT_CLOSED
        disconnect_code = _code_number(default_code)
        disconnect_reason = _code_message(default_code)
        reconnect = True

        if ws_code < 3000:
            if ws_code == 1009:
                code = _DisconnectedCode.MESSAGE_SIZE_LIMIT
                disconnect_code = _code_number(code)
                disconnect_reason = _code_message(code)
        else:
            disconnect_code = ws_code
            disconnect_reason = ws_reason
            if (3500 <= ws_code < 4000) or (4500 <= ws_code < 5000):
                reconnect = False

        await self._disconnect(disconnect_code, disconnect_reason, reconnect)

    def _publication_from_proto(self, pub: Any) -> Publication:
        info = pub.get("info")
        client_info = self._extract_client_info(info) if info else None
        offset = int(pub.get("offset", 0))
        return Publication(
            offset=offset,
            data=self._decode_data(pub.get("data")),
            info=client_info,
            tags=pub.get("tags", {}),
            delta=pub.get("delta", False),
        )


class Subscription:
    """
    Subscription describes client subscription to a channel.

    It can be in one of three states:
        - unsubscribed (initial state, or after unsubscribe called, or terminal unsubscribe code
                        received from server)
        - subscribing (when subscribe called, or when automatic re-subscription is in progress)
        - subscribed (after successfully subscribe)

    See more details about subscription behaviour in
    https://centrifugal.dev/docs/transports/client_api.
    """

    _resubscribe_backoff_factor = 2
    _resubscribe_backoff_jitter = 0.5

    def __init__(self) -> None:
        # Some definitions required to make PyCharm linting happy.
        self.state: SubscriptionState = SubscriptionState.UNSUBSCRIBED
        self._subscribed_future = None
        raise CentrifugeError(
            "do not create Subscription instances directly, use Client.new_subscription method",
        )

    def _initialize(
        self,
        client: Client,
        channel: str,
        events: Optional[SubscriptionEventHandler] = None,
        token: str = "",
        get_token: Optional[Callable[[str], Awaitable[str]]] = None,
        data: Optional[Any] = None,
        min_resubscribe_delay: float = 0.1,
        max_resubscribe_delay: float = 10.0,
        positioned: bool = False,
        recoverable: bool = False,
        join_leave: bool = False,
        delta: Optional[DeltaType] = None,
    ) -> None:
        """Initializes Subscription instance.
        Note: use Client.new_subscription method to create new subscriptions in your app.
        """
        self.channel = channel
        self.state = SubscriptionState.UNSUBSCRIBED
        self.events = events or SubscriptionEventHandler()
        self._subscribed_future: asyncio.Future[bool] = asyncio.Future()
        self._subscribed = False
        self._client: Optional[Client] = client
        self._token = token
        self._get_token = get_token
        self._data = data
        self._min_resubscribe_delay = min_resubscribe_delay
        self._max_resubscribe_delay = max_resubscribe_delay
        self._positioned = positioned
        self._recoverable = recoverable
        self._join_leave = join_leave
        self._resubscribe_attempts = 0
        self._refresh_timer: Optional[TimerHandle] = None
        self._resubscribe_timer: Optional[TimerHandle] = None
        self._recover: bool = False
        self._offset: int = 0
        self._epoch: str = ""
        self._prev_data: Optional[Any] = None

        if delta and delta not in {DeltaType.FOSSIL}:
            raise CentrifugeError("unsupported delta format")
        self._delta = delta
        self._delta_negotiated: bool = False

    @classmethod
    def _create_instance(cls, *args: Any, **kwargs: Any) -> "Subscription":
        obj = cls.__new__(cls)
        obj._initialize(*args, **kwargs)
        return obj

    def _check_state(self):
        if self.state == SubscriptionState.UNSUBSCRIBED:
            raise SubscriptionUnsubscribedError("subscription unsubscribed")

    async def ready(self, timeout: Optional[float] = None) -> None:
        self._check_state()
        completed = await _wait_for_future(
            self._subscribed_future,
            timeout=timeout or self._client._timeout,
        )
        if not completed:
            raise OperationTimeoutError("timeout waiting for subscription to be ready")

    async def history(
        self,
        limit: int = 0,
        since: Optional[StreamPosition] = None,
        reverse: bool = False,
        timeout: Optional[float] = None,
    ) -> HistoryResult:
        await self.ready(timeout=timeout)
        return await self._client.history(
            self.channel,
            limit=limit,
            since=since,
            reverse=reverse,
            timeout=timeout,
        )

    async def presence(self, timeout: Optional[float] = None) -> PresenceResult:
        await self.ready(timeout=timeout)
        # noinspection PyProtectedMember
        return await self._client.presence(self.channel, timeout=timeout)

    async def presence_stats(self, timeout: Optional[float] = None) -> PresenceStatsResult:
        await self.ready(timeout=timeout)
        # noinspection PyProtectedMember
        return await self._client.presence_stats(self.channel, timeout=timeout)

    async def publish(self, data: Any, timeout: Optional[float] = None) -> PublishResult:
        await self.ready(timeout=timeout)
        # noinspection PyProtectedMember
        return await self._client.publish(self.channel, data, timeout=timeout)

    async def subscribe(self) -> None:
        if self.state == SubscriptionState.SUBSCRIBING:
            return

        self.state = SubscriptionState.SUBSCRIBING

        handler = self.events.on_subscribing
        code = _SubscribingCode.SUBSCRIBE_CALLED
        await handler(SubscribingContext(code=_code_number(code), reason=_code_message(code)))

        await self._client._resubscribe(self)

    async def unsubscribe(self) -> None:
        code = _UnsubscribedCode.UNSUBSCRIBE_CALLED
        await self._move_unsubscribed(
            code=_code_number(code),
            reason=_code_message(code),
            send_unsubscribe_command=True,
        )

    def _clear_subscribed_state(self) -> None:
        if self._refresh_timer:
            logger.debug("canceling refresh timer for %s", self.channel)
            self._refresh_timer.cancel()
            self._refresh_timer = None

    def _clear_subscribing_state(self) -> None:
        self._resubscribe_attempts = 0
        if self._resubscribe_timer:
            logger.debug("canceling resubscribe timer for %s", self.channel)
            self._resubscribe_timer.cancel()
            self._resubscribe_timer = None

    async def _move_unsubscribed(
        self,
        code: int,
        reason: str,
        send_unsubscribe_command: bool = False,
    ) -> None:
        if self.state == SubscriptionState.UNSUBSCRIBED:
            return

        if self.state == SubscriptionState.SUBSCRIBED:
            self._clear_subscribed_state()
        elif self.state == SubscriptionState.SUBSCRIBING:
            self._clear_subscribing_state()

        self.state = SubscriptionState.UNSUBSCRIBED

        if self._subscribed_future.done():
            self._subscribed_future = asyncio.Future()
        self._subscribed_future.set_exception(
            SubscriptionUnsubscribedError("subscription unsubscribed"),
        )
        await self._consume_subscribed_future()

        handler = self.events.on_unsubscribed
        await handler(
            UnsubscribedContext(
                code=code,
                reason=reason,
            ),
        )

        if send_unsubscribe_command:
            # noinspection PyProtectedMember
            await self._client._unsubscribe(self.channel)

    async def _consume_subscribed_future(self) -> None:
        with contextlib.suppress(CentrifugeError):
            await self._subscribed_future

    async def _move_subscribing(
        self,
        code: int,
        reason: str,
        skip_schedule_resubscribe: bool = False,
    ) -> None:
        if self.state == SubscriptionState.SUBSCRIBING:
            return

        if self.state == SubscriptionState.SUBSCRIBED:
            self._clear_subscribed_state()

        self.state = SubscriptionState.SUBSCRIBING
        if self._subscribed_future.done():
            self._subscribed_future = asyncio.Future()

        handler = self.events.on_subscribing
        await handler(
            SubscribingContext(
                code=code,
                reason=reason,
            ),
        )

        if not skip_schedule_resubscribe:
            asyncio.ensure_future(self._client._resubscribe(self))

    async def _move_subscribed(self, subscribe: Dict[str, Any]) -> None:
        self.state = SubscriptionState.SUBSCRIBED
        self._subscribed_future.set_result(True)
        on_subscribed_handler = self.events.on_subscribed
        recoverable = subscribe.get("recoverable", False)
        positioned = subscribe.get("positioned", False)
        stream_position = None
        if positioned or recoverable:
            stream_position = StreamPosition(
                offset=int(subscribe.get("offset", 0)),
                epoch=subscribe.get("epoch", ""),
            )

        if recoverable:
            self._recover = True
            self._offset = stream_position.offset
            self._epoch = stream_position.epoch

        expires = subscribe.get("expires", False)
        if expires:
            ttl = subscribe["ttl"]
            self._refresh_timer = self._client._loop.call_later(
                ttl,
                lambda: asyncio.ensure_future(self._refresh(), loop=self._client._loop),
            )

        self._delta_negotiated = subscribe.get("delta", False)

        await on_subscribed_handler(
            SubscribedContext(
                channel=self.channel,
                recoverable=recoverable,
                positioned=positioned,
                stream_position=stream_position,
                was_recovering=subscribe.get("was_recovering", False),
                recovered=subscribe.get("recovered", False),
                data=self._client._decode_data(subscribe.get("data")),
            ),
        )

        publications = subscribe.get("publications", [])
        if publications:
            for pub in publications:
                await self._process_publication(pub)

        self._clear_subscribing_state()

    async def _refresh(self) -> None:
        if self.state != SubscriptionState.SUBSCRIBED:
            return
        await self._client._sub_refresh(self.channel)

    async def _schedule_resubscribe(self) -> None:
        if self.state != SubscriptionState.SUBSCRIBING:
            return

        delay = _backoff(
            self._resubscribe_attempts,
            self._min_resubscribe_delay,
            self._max_resubscribe_delay,
        )
        self._resubscribe_attempts += 1
        logger.debug("start resubscribing in %f", delay)
        self._resubscribe_timer = self._client._loop.call_later(
            delay,
            lambda: asyncio.ensure_future(self._resubscribe()),
        )

    async def _resubscribe(self) -> None:
        if self.state != SubscriptionState.SUBSCRIBING:
            return
        await self._client._resubscribe(self)

    async def _process_publication(self, pub: Any) -> None:
        publication = self._client._publication_from_proto(pub)

        if self._delta and self._delta_negotiated:
            new_data, prev_data = self._client._codec.apply_delta_if_needed(
                self._prev_data, publication
            )
            publication.data = new_data
            self._prev_data = prev_data

        await self.events.on_publication(PublicationContext(pub=publication))
        if publication.offset > 0:
            self._offset = publication.offset

    def _need_recover(self):
        return self._recover
