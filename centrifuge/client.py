import asyncio
import base64
import contextlib
import importlib.util
import logging
import ssl
from asyncio import TimerHandle
from contextlib import asynccontextmanager
from dataclasses import dataclass
from enum import Enum
from typing import (
    TYPE_CHECKING,
    Any,
    Awaitable,
    Dict,
    Literal,
    Optional,
    Set,
    Tuple,
    Union,
    List,
    Callable,
)

import websockets
from websockets import exceptions

from centrifuge.codecs import _JsonCodec, _ProtobufCodec
from centrifuge.filter import FilterNode
from centrifuge.codes import (
    _DISCONNECTED_STATE_INVALIDATED,
    _ERROR_CODE_UNRECOVERABLE_POSITION,
    _SUBSCRIPTION_FLAG_CHANNEL_COMPACTION,
    _SUBSCRIPTION_FLAG_REJECT_UNRECOVERED,
    _UNSUBSCRIBED_STATE_INVALIDATED,
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
    from websockets.asyncio.client import ClientConnection
    from asyncio import AbstractEventLoop

logger = logging.getLogger("centrifuge")

# Jittered delays (in seconds) used to retry a token refresh when the app's
# get_token callback raises. The SDK spec promises such a retry, see
# https://centrifugal.dev/docs/transports/client_api - values match other
# Centrifugal SDKs.
# How long closing a transport waits for the server to answer the close frame
# before aborting the TCP connection. Nothing waits for it.
_CLOSE_TIMEOUT = 1.0


def _task_cancelling() -> bool:
    """Whether the current task is being cancelled (as opposed to a coroutine it
    awaits raising CancelledError, e.g. an app callback). Python 3.10 can't tell
    them apart, and an app callback is assumed there."""
    task = asyncio.current_task()
    cancelling = getattr(task, "cancelling", None)
    return bool(cancelling and cancelling() > 0)


# How long a timeout that fires late (the process was suspended) waits for what
# arrived meanwhile. See _TimeoutTimer.
_LATE_TIMER_GRACE = 1.0


class _GuardedEvents:
    """Calls the event handlers of the app (looked up when called, so a handler
    replaced on the handler object is the one called). An exception escaping a
    handler doesn't reach the SDK's own code: it is reported, and the client stops
    (see Client._handler_raised); the SDK goes on as if the handler had returned."""

    def __init__(
        self,
        events: Callable[[], Any],
        client: Callable[[], Optional["Client"]],
        publication_failed: Callable[[Any], None],
    ) -> None:
        self._events = events
        self._client = client
        self._publication_failed = publication_failed

    def __getattr__(self, name: str) -> Callable[..., Awaitable[Any]]:
        handler = getattr(self._events(), name)

        async def call(*args: Any, **kwargs: Any) -> Any:
            try:
                return await handler(*args, **kwargs)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                if name == "on_publication" and args:
                    # Not delivered: the teardown that follows must not count it.
                    self._publication_failed(args[0])
                client = self._client()
                if client is not None:
                    await client._handler_raised(e)
                else:
                    asyncio.get_running_loop().call_exception_handler({
                        "message": "exception in a centrifuge event handler",
                        "exception": e,
                    })
                return None

        return call


class _TimeoutTimer:
    """A loop.call_later() for timeouts. A timer that fires much later than
    scheduled means the process was suspended (frozen in the background, stopped
    in a debugger): replies and pings that arrived meanwhile may still be waiting
    in the socket, so the callback runs after a short grace period instead,
    letting them be handled first."""

    def __init__(self, loop: "AbstractEventLoop", delay: float, callback: Callable[[], Any]):
        self._loop = loop
        self._callback = callback
        self._deadline = loop.time() + delay
        self._handle = loop.call_later(delay, self._fire)

    def _fire(self) -> None:
        if self._loop.time() - self._deadline > _LATE_TIMER_GRACE:
            self._deadline = float("inf")
            self._handle = self._loop.call_later(_LATE_TIMER_GRACE, self._callback)
            return
        self._callback()

    def cancel(self) -> None:
        self._handle.cancel()

    def cancelled(self) -> bool:
        return self._handle.cancelled()


_REFRESH_RETRY_MIN_DELAY = 5.0
_REFRESH_RETRY_MAX_DELAY = 10.0
_SUB_REFRESH_RETRY_MIN_DELAY = 10.0
_SUB_REFRESH_RETRY_MAX_DELAY = 20.0


def _python_socks_installed() -> bool:
    return importlib.util.find_spec("python_socks") is not None


def _redact_proxy(proxy: str) -> str:
    """Strip credentials from a proxy URL, so that it's safe to put in an error message."""
    scheme, separator, rest = proxy.partition("://")
    if not separator:
        # Malformed URL without a scheme - it may still carry credentials.
        scheme, rest = "", proxy
    _, at, hostinfo = rest.rpartition("@")
    if not at:
        return proxy
    return f"{scheme}{separator}***@{hostinfo}"


def _load_parse_proxy() -> Optional[Callable[[str], Any]]:
    """Return the proxy URL parser of websockets, or None if it can not be found.

    The helper appeared in websockets 15.0 as websockets.uri.parse_proxy and moved
    to websockets.proxy in websockets 16.0 - both layouts are supported here.
    """
    for module_name in ("websockets.proxy", "websockets.uri"):
        try:
            module = importlib.import_module(module_name)
        except ImportError:  # pragma: no cover - all these modules exist.
            continue
        parse_proxy = getattr(module, "parse_proxy", None)
        if parse_proxy is not None:
            return parse_proxy
    return None


def _validate_proxy(proxy: Union[str, Literal[True], None]) -> None:
    """Validate the proxy option in the constructor, raising ValueError if it can't work.

    Proxy misconfiguration is checked upfront because errors raised in the middle
    of connecting are easy to miss: from the automatic reconnect task they end up
    as an unretrieved task exception, and those which are neither OSError nor
    WebSocketException (like the ImportError raised for a SOCKS proxy without
    python-socks installed) are not even reported to the on_error handler.
    """
    if proxy is True or proxy is None:
        # True (take the proxy from the environment, the websockets default) and
        # None (always connect directly) need no further validation.
        return

    if not isinstance(proxy, str):
        raise ValueError(f"proxy must be a proxy URL, None or True, got {proxy!r}")

    parse_proxy = _load_parse_proxy()
    if parse_proxy is None:  # pragma: no cover - in case websockets moves it again.
        return

    try:
        parsed = parse_proxy(proxy)
    except exceptions.InvalidProxy as e:
        # Not str(e) - it embeds the proxy URL as is, together with the
        # credentials it may contain, into a message which likely gets logged.
        raise ValueError(f"{_redact_proxy(proxy)} isn't a valid proxy: {e.msg}") from e

    if parsed.scheme.startswith("socks") and not _python_socks_installed():
        raise ValueError(
            f"{parsed.scheme} proxy requires the python-socks package to be installed"
        )


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
    timeout: Optional[_TimeoutTimer]


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
        ssl_context: Optional[ssl.SSLContext] = None,
        proxy: Union[str, Literal[True], None] = True,
    ):
        """Initializes new Client instance.

        Client can be in one of 3 states:
            - disconnected (initial state, or after disconnect called, or terminal disconnect code
                            received from server)
            - connecting (when connect called, or when automatic reconnection is in progress)
            - connected (after successful connect)
        See more details about SDK behaviour in https://centrifugal.dev/docs/transports/client_api.

        ssl_context customizes TLS for wss:// addresses - for example to trust a
        custom CA or (not recommended in production) to skip certificate
        verification. When not set, a default TLS context is used. Passing it
        together with a ws:// address is an error.

        proxy sets the proxy to connect through, in the form
        "http://user:pass@host:port" ("socks5://..." also works, but requires the
        python-socks package to be installed). By default (proxy=True) proxy
        configuration is taken from the environment (WS_PROXY/WSS_PROXY,
        HTTP_PROXY/HTTPS_PROXY, honoring NO_PROXY). Pass None to always connect
        directly, ignoring the environment configuration.
        """
        if ssl_context is not None and not address.lower().startswith("wss://"):
            raise ValueError("ssl_context option is only applicable to wss:// addresses")

        _validate_proxy(proxy)

        self.state: ClientState = ClientState.DISCONNECTED
        self.events: ClientEventHandler = events or ClientEventHandler()
        self._address: str = address
        self._use_protobuf: bool = use_protobuf
        self._codec: Union[
            _ProtobufCodec,
            _JsonCodec,
        ] = _ProtobufCodec() if use_protobuf else _JsonCodec()
        self._conn: Optional["ClientConnection"] = None
        # Identifies the current connection attempt, or the current connection.
        # Changes on every attempt and every teardown: continuations of an
        # attempt or connection that is over (a token request, a reply, the
        # socket reader) check it and stop.
        self._transport_id: int = 0
        # Changes on every state transition started by connect() or a teardown:
        # a teardown superseded by another one (e.g. disconnect() called from one
        # of its event handlers) stops emitting events and doesn't reconnect.
        self._transition: int = 0
        # The WebSocket handshake in progress, cancelled by a teardown.
        self._open_task: Optional[asyncio.Task] = None
        self._id: int = 0
        self._subs: Dict[str, Subscription] = {}
        self._messages: asyncio.Queue = asyncio.Queue()
        self._delay: float = 0
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
        self.__connected_future: Optional[asyncio.Future] = None
        self._token = token
        self._get_token = get_token
        self._handler_failures = 0
        self._ev = _GuardedEvents(
            lambda: self.events, lambda: self, self._server_publication_failed
        )
        # Set when the server answered "token expired" (109): the next attempt gets
        # a new token from get_token - or, without one, stops as unauthorized.
        self._refresh_required = False
        self._bound_loop = loop
        self._inflight_commands: Dict[int, _Callback] = {}
        self._reconnect_attempts = 0
        self._reconnect_timer = None
        self._headers = headers or {}
        self._ssl_context = ssl_context
        self._proxy = proxy
        self._server_subs: Dict[str, _ServerSubscription] = {}
        # Server-side publications whose handler is running: channel ->
        # (connection, offset). Committed when the connection ends during the
        # handler, so the next connect recovers after them.
        self._server_pending: Dict[str, Tuple[int, int]] = {}
        # Channel compaction: numeric channel ID -> subscription, used to route
        # pushes that carry an ID instead of the string channel name. IDs are
        # scoped to a server session — the registry is dropped when the connected
        # state is cleared and each subscription re-registers from its subscribe
        # reply.
        self._subs_by_id: Dict[int, "Subscription"] = {}
        self._listen_task: Optional[asyncio.Task] = None
        self._process_messages_task: Optional[asyncio.Task] = None
        self._background_tasks: Set[asyncio.Task] = set()

    def _spawn(self, coro: Awaitable[Any]) -> asyncio.Task:
        """Start a background task nothing is going to await.

        The task is kept in a set until it finishes: the event loop only holds
        weak references to tasks, so a fire-and-forget one may be garbage
        collected before it completes. Its outcome is inspected when it is done,
        because an exception in a task nobody awaits is otherwise reported at
        best late (when the task is collected) and at worst not at all.
        """
        task = asyncio.ensure_future(coro)
        self._background_tasks.add(task)
        task.add_done_callback(self._background_task_done)
        return task

    def _background_task_done(self, task: asyncio.Task) -> None:
        self._background_tasks.discard(task)
        if task.cancelled():
            return
        exception = task.exception()
        if exception is not None:
            logger.error("unhandled exception in background task", exc_info=exception)

    @property
    def _connected_future(self) -> asyncio.Future:
        # Created on first use for the same reason as the loop below:
        # asyncio.Future() binds to the current loop, which does not exist yet
        # when the Client is constructed outside of asyncio.run().
        if self.__connected_future is None:
            self.__connected_future = asyncio.Future()
        return self.__connected_future

    @_connected_future.setter
    def _connected_future(self, future: asyncio.Future) -> None:
        self.__connected_future = future

    @property
    def _loop(self) -> "AbstractEventLoop":
        # The loop is resolved on first use instead of in the constructor: since
        # Python 3.14 asyncio.get_event_loop() raises RuntimeError when there is
        # no running loop, and a Client is usually created before asyncio.run()
        # starts one. Every use happens inside a running loop, so by then it is
        # available - and it is remembered to keep the client bound to one loop.
        if self._bound_loop is None:
            self._bound_loop = asyncio.get_running_loop()
        return self._bound_loop

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
        tags_filter: Optional[FilterNode] = None,
        get_state: Optional[Callable[[str], Awaitable[StreamPosition]]] = None,
    ) -> "Subscription":
        """Creates new subscription to channel. If subscription already exists then
        DuplicateSubscriptionError exception will be raised.

        tags_filter sets a server-side publication filter based on publication
        tags: the server delivers only publications whose tags match the filter.
        It must be enabled for the namespace on the server (allow_tags_filter)
        and cannot be combined with delta. Build it with the Filter helpers.

        get_state is called to load the app's current state and stream position.
        Requires Centrifugo >= 6.8.0.

        The SDK calls it:

        - On initial subscribe (no saved position)
        - On reconnect when recovery fails (server returns error 112 —
          unrecoverable position)

        NOT called on reconnects where the server successfully recovers missed
        publications — in that case the recovered publications arrive as events
        and get_state is skipped.

        The app should load its data from its own source of truth (database,
        API), render it, and return the stream position. The SDK subscribes with
        recovery from the returned position, so any publications between the
        state read and the subscribe are delivered as publication events.

        IMPORTANT: inside get_state, read the stream position FIRST, then read
        your data. This ensures the position is a lower bound — any data loaded
        after the position read is guaranteed to be included. The reverse order
        can produce gaps.

        Recovered publications may overlap with data already loaded in get_state.
        This works correctly when updates are idempotent (applying the same
        update twice produces the same result). For non-idempotent updates,
        deduplicate by publication offset.

        On error, the SDK emits an error event with SUBSCRIPTION_GET_STATE code
        and retries with backoff.
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
            tags_filter=tags_filter,
            get_state=get_state,
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

        # Only this subscription: one removed before must not remove a newer
        # subscription of the same channel.
        if self._subs.get(sub.channel) is not sub:
            return

        if sub.state != SubscriptionState.UNSUBSCRIBED:
            raise CentrifugeError("can not remove subscription in non-unsubscribed state")

        sub._client = None
        del self._subs[sub.channel]

    def _close_transport(self) -> None:
        """Close the current transport (or abort the handshake in progress) without
        waiting for it: the peer may never answer the close frame (a dead link),
        and nothing waits for this transport anymore. websockets aborts the TCP
        connection after close_timeout."""
        if self._open_task is not None:
            self._open_task.cancel()
            self._open_task = None
        conn = self._conn
        self._conn = None
        if conn is not None:
            self._spawn(self._close_conn(conn))

    @staticmethod
    async def _close_conn(conn: "ClientConnection") -> None:
        with contextlib.suppress(Exception):
            await conn.close()

    def _attempt_current(self, attempt: int) -> bool:
        return self._transport_id == attempt and self.state == ClientState.CONNECTING

    def _connection_current(self, attempt: int) -> bool:
        return self._transport_id == attempt and self.state == ClientState.CONNECTED

    async def _schedule_reconnect(self) -> None:
        # Only a client that is connecting reconnects: after disconnect() or a
        # terminal disconnect the state is DISCONNECTED, and a later connect()
        # makes it CONNECTING again - so reconnects work after it too.
        if self.state != ClientState.CONNECTING:
            logger.debug("won't reconnect")
            return

        # Cancel existing reconnect timer to prevent timer leaks and duplicate scheduling
        if self._reconnect_timer:
            logger.debug("canceling existing reconnect timer")
            self._reconnect_timer.cancel()
            self._reconnect_timer = None

        delay = _backoff(
            self._reconnect_attempts,
            self._min_reconnect_delay,
            self._max_reconnect_delay,
        )
        self._reconnect_attempts += 1
        logger.debug("start reconnecting in %f", delay)
        self._reconnect_timer = self._loop.call_later(
            delay,
            lambda: self._spawn(self._reconnect()),
        )

    async def _reconnect(self) -> None:
        if self.state != ClientState.CONNECTING:
            return
        await self._create_connection()

    def _next_command_id(self) -> int:
        self._id += 1
        return self._id

    async def _create_connection(self) -> None:
        attempt = self._transport_id + 1
        try:
            await self._connection_attempt()
        except Exception as e:
            # An event handler of the app raised (handlers run as part of the
            # attempt): the client must not be left connecting with nothing in
            # progress.
            logger.exception("exception during connection attempt")
            await self._handler_failed(attempt, e)

    async def _handler_raised(self, e: Exception) -> None:
        """An exception escaping an event handler of the app. The SDK can't know
        what the handler left undone, so it fails loudly instead of going on as if
        nothing happened, as centrifuge-js does: the exception is re-raised to the
        event loop's exception handler, and the client disconnects with "bad
        protocol" and doesn't reconnect. Its own processing goes on as if the
        handler had returned, as for a handler calling disconnect()."""
        self._handler_failures += 1
        asyncio.get_running_loop().call_exception_handler({
            "message": "exception in a centrifuge event handler",
            "exception": e,
        })
        if self.state != ClientState.DISCONNECTED:
            code = _DisconnectedCode.BAD_PROTOCOL
            await self._disconnect(_code_number(code), f"exception in event handler: {e}", False)

    def _server_publication_failed(self, ctx: Any) -> None:
        channel = getattr(ctx, "channel", None)
        if channel is not None:
            self._server_pending.pop(channel, None)

    async def _handler_failed(self, attempt: int, e: Exception) -> None:
        if self._attempt_current(attempt):
            # Still connecting: retry with backoff.
            await self._fail_connection_attempt(attempt)
        elif self._connection_current(attempt):
            # While handling a server reply or push: the client can't know what
            # was applied - stop, as centrifuge-js does.
            code = _DisconnectedCode.BAD_PROTOCOL
            await self._disconnect(
                _code_number(code), f"exception during message handling: {e}", False
            )

    async def _connection_attempt(self) -> None:
        """One connection attempt: get a token if needed, open the transport, send
        the connect command and handle its reply. Every step acts only while the
        attempt is current - a teardown (disconnect(), a lost connection) or a
        newer attempt makes it stale, and it stops."""
        if self.state != ClientState.CONNECTING:
            return
        self._transport_id += 1
        attempt = self._transport_id
        # A transport left from before (normally closed by the teardown already)
        # is closed: one connection at a time.
        if self._conn is not None or self._open_task is not None:
            self._stop_transport_tasks()
            self._close_transport()

        if self._refresh_required and self._get_token is None:
            # The token expired and there is nothing to get a new one with.
            handler = self._ev.on_error
            await handler(
                ErrorContext(
                    code=_code_number(_ErrorCode.BAD_CONFIGURATION),
                    error=CentrifugeError(
                        "token expired but no get_token function set in the configuration"
                    ),
                )
            )
            if self._attempt_current(attempt):
                code = _DisconnectedCode.UNAUTHORIZED
                await self._disconnect(_code_number(code), _code_message(code), False)
            return

        if self._get_token and (self._refresh_required or not self._token):
            try:
                token = await self._get_token()
            except asyncio.CancelledError as e:
                if _task_cancelling():
                    raise
                await self._connect_token_failed(attempt, e)
                return
            except Exception as e:
                await self._connect_token_failed(attempt, e)
                return
            # A token requested by an attempt that is over (e.g. before
            # disconnect() + connect() to switch users) is not used.
            if not self._attempt_current(attempt):
                return
            self._token = token
            self._refresh_required = False

        open_task = asyncio.ensure_future(self._open_transport())
        self._open_task = open_task
        try:
            conn = await open_task
        except asyncio.CancelledError:
            if _task_cancelling():
                raise
            # Aborted by a teardown (e.g. disconnect() during the handshake).
            return
        except (OSError, asyncio.TimeoutError, exceptions.WebSocketException, ImportError) as e:
            # ImportError is what websockets raises for a SOCKS proxy without
            # python-socks installed. An explicitly configured proxy is checked
            # in the constructor, but a proxy coming from the environment can
            # only fail here - and it must not escape into the reconnect task.
            if self._open_task is open_task:
                self._open_task = None
            if not self._attempt_current(attempt):
                return
            handler = self._ev.on_error
            await handler(ErrorContext(code=_code_number(_ErrorCode.TRANSPORT_CLOSED), error=e))
            if self._attempt_current(attempt):
                await self._schedule_reconnect()
            return
        if self._open_task is open_task:
            self._open_task = None

        if not self._attempt_current(attempt):
            logger.debug("state changed during websocket connection, closing")
            self._spawn(self._close_conn(conn))
            return

        self._conn = conn
        queue: asyncio.Queue = asyncio.Queue()
        self._messages = queue
        self._listen_task = self._spawn(self._listen(conn, queue, attempt))
        self._process_messages_task = self._spawn(self._process_messages(queue, attempt))

        self._delay = self._min_reconnect_delay

        cmd_id = self._next_command_id()
        command = self._construct_connect_command(cmd_id)
        sent_token = self._token
        async with self._register_future_with_done(cmd_id) as future:
            await self._send_commands([command])

            try:
                reply = await future
            except OperationTimeoutError as e:
                if not self._attempt_current(attempt):
                    return
                handler = self._ev.on_error
                await handler(ErrorContext(code=_code_number(_ErrorCode.TIMEOUT), error=e))
                await self._fail_connection_attempt(attempt)
                return
            except Exception as e:
                if not self._attempt_current(attempt):
                    return
                handler = self._ev.on_error
                await handler(
                    ErrorContext(code=_code_number(_ErrorCode.CONNECT_ERROR), error=e),
                )
                await self._fail_connection_attempt(attempt)
                return

            if not self._attempt_current(attempt):
                logger.debug("state changed while waiting for connect reply, aborting")
                return

            if reply.get("error"):
                logger.debug("connect reply has error: %s", reply.get("error"))
                code, message, temporary = self._extract_error_details(reply)
                if _is_token_expired(code):
                    temporary = True
                    # The next attempt gets a new token - unless the app set one
                    # meanwhile (see set_token).
                    if sent_token == self._token:
                        self._refresh_required = True
                if temporary:
                    handler = self._ev.on_error
                    await handler(
                        ErrorContext(
                            code=_code_number(_ErrorCode.CONNECT_ERROR),
                            error=ReplyError(code, message, temporary),
                        ),
                    )
                    await self._fail_connection_attempt(attempt)
                else:
                    await self._disconnect(code, message, False)
                return

            await self._connected(reply["connect"], attempt)

    async def _connected(self, connect: Dict[str, Any], attempt: int) -> None:
        """Apply a successful connect reply."""
        self._refresh_required = False
        self.client_id = connect["client"]
        self.state = ClientState.CONNECTED
        self._send_pong = connect.get("pong", False)
        self._ping_interval = connect.get("ping", 0)
        if self._ping_interval > 0:
            self._restart_ping_wait()

        expires = connect.get("expires", False)
        if expires:
            ttl = connect["ttl"]
            # Cancel existing refresh timer to prevent timer leaks
            if self._refresh_timer:
                self._refresh_timer.cancel()
            self._refresh_timer = self._loop.call_later(
                ttl,
                lambda: self._spawn(self._refresh(attempt)),
            )

        self._connected_future.set_result(True)

        handler = self._ev.on_connected
        await handler(
            ConnectedContext(
                client=connect.get("client"),
                version=connect.get("version", ""),
                data=self._decode_data(connect.get("data")),
            ),
        )
        # The handler may have disconnected (or reconnected) the client.
        if not self._connection_current(attempt):
            return

        self._clear_connecting_state()

        for channel, sub in list(self._subs.items()):
            if not sub or sub.state != SubscriptionState.SUBSCRIBING:
                continue
            # This subscribe replaces a retry still waiting from before.
            sub._cancel_resubscribe_timer()
            self._spawn(self._subscribe(channel))

        await self._process_server_subs(connect.get("subs", {}), attempt)

    async def _open_transport(self) -> "ClientConnection":
        # None, not an empty list, for JSON: websockets sends an empty
        # Sec-WebSocket-Protocol header for [], which RFC 6455 does not
        # allow and servers such as the websockets one reject.
        subprotocols: Optional[List[str]] = None
        if self._use_protobuf:
            subprotocols = ["centrifuge-protobuf"]

        connect_kwargs: Dict[str, Any] = {}
        if self._ssl_context is not None:
            # Only pass ssl when explicitly configured: websockets treats
            # ssl=None as "no TLS" and raises ValueError for it on a wss://
            # address, instead of falling back to the default TLS context.
            connect_kwargs["ssl"] = self._ssl_context

        return await websockets.connect(
            self._address,
            proxy=self._proxy,
            subprotocols=subprotocols,
            additional_headers=self._headers,
            # The handshake is limited by the client timeout, like the commands
            # sent over the connection.
            open_timeout=self._timeout,
            close_timeout=_CLOSE_TIMEOUT,
            # websockets closes the connection on a message over 1 MiB by default,
            # and a recovery reply can be larger: the server decides how large a
            # message can be.
            max_size=None,
            **connect_kwargs,
        )

    async def _connect_token_failed(self, attempt: int, e: BaseException) -> None:
        if not self._attempt_current(attempt):
            return
        if isinstance(e, UnauthorizedError):
            code = _DisconnectedCode.UNAUTHORIZED
            await self._disconnect(_code_number(code), _code_message(code), False)
            return
        handler = self._ev.on_error
        await handler(
            ErrorContext(code=_code_number(_ErrorCode.CLIENT_CONNECT_TOKEN), error=e),
        )
        if self._attempt_current(attempt):
            await self._schedule_reconnect()

    async def _fail_connection_attempt(self, attempt: int) -> None:
        """Tear the attempt down and reconnect with backoff."""
        if not self._attempt_current(attempt):
            return
        code = _ConnectingCode.TRANSPORT_CLOSED
        await self._disconnect(_code_number(code), _code_message(code), True)

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

    async def _process_server_subs(self, subs: Dict[str, Dict[str, Any]], attempt: int):
        logger.debug("process server subs: %s", subs)
        # Event handlers may disconnect the client: nothing more is emitted for a
        # connection that is over.
        for channel, subscribe in subs.items():
            if not self._connection_current(attempt):
                return
            self._server_subs[channel] = _ServerSubscription(
                offset=int(subscribe.get("offset", 0)),
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

            handler = self._ev.on_subscribed
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
                    if not self._connection_current(attempt):
                        return
                    await self._process_server_publication(channel, pub)

        # using None is important here to deal with {} too.
        channels_to_delete = [ch for ch in self._server_subs if subs.get(ch) is None]
        for channel in channels_to_delete:
            del self._server_subs[channel]
        for channel in channels_to_delete:
            if not self._connection_current(attempt):
                return
            handler = self._ev.on_unsubscribed
            await handler(ServerUnsubscribedContext(channel=channel))

    async def _process_server_publication(self, channel: str, pub: Any):
        publication = self._publication_from_proto(pub)
        connection = self._transport_id
        if publication.offset > 0:
            self._server_pending[channel] = (connection, publication.offset)
        failures = self._handler_failures
        try:
            await self._ev.on_publication(
                ServerPublicationContext(channel=channel, pub=publication)
            )
        finally:
            if self._server_pending.get(channel, (None,))[0] == connection:
                del self._server_pending[channel]
        if self._handler_failures != failures:
            # The handler raised: the publication isn't delivered, the position
            # stays before it and the client stopped.
            return
        # A handler that finishes after its connection was lost (handlers aren't
        # cancelled) must not move the position the next connection recovers
        # from, possibly back.
        server_sub = self._server_subs.get(channel)
        if publication.offset > 0 and server_sub and self._transport_id == connection:
            server_sub.offset = publication.offset

    def _clear_connecting_state(self) -> None:
        self._reconnect_attempts = 0
        if self._reconnect_timer:
            logger.debug("clear reconnect timer")
            self._reconnect_timer.cancel()
            self._reconnect_timer = None

    async def _clear_connected_state(self, transition: int) -> None:
        # Channel compaction IDs are scoped to a server session — drop the routing
        # registry; each resubscribe re-registers a fresh ID. The per-subscription
        # _push_id is left as-is: the next subscribe reply re-registers it
        # regardless of whether the server reuses the same ID.
        self._subs_by_id.clear()
        # Every subscribed subscription moves to subscribing first, before any
        # event: whatever a handler does then (raise, disconnect, connect again),
        # no subscription is left subscribed without a connection - the next
        # connect resubscribes them all - and each still gets its event. Copies:
        # handlers may create or remove subscriptions meanwhile.
        moved = [
            (sub, sub._attempt) for sub in list(self._subs.values()) if sub._enter_subscribing()
        ]
        code = _SubscribingCode.TRANSPORT_CLOSED
        for sub, attempt in moved:
            # Only while the subscription is still where this teardown moved it:
            # a handler may have unsubscribed it, or the client connected again
            # and resubscribed it meanwhile.
            if sub._attempt != attempt or sub.state != SubscriptionState.SUBSCRIBING:
                continue
            try:
                await sub._ev.on_subscribing(
                    SubscribingContext(code=_code_number(code), reason=_code_message(code))
                )
            except Exception:
                logger.exception("exception in on_subscribing handler")

        for channel in list(self._server_subs):
            if self._transition != transition:
                return
            try:
                await self._ev.on_subscribing(ServerSubscribingContext(channel=channel))
            except Exception:
                logger.exception("exception in on_subscribing handler")

    def set_token(self, token: str) -> None:
        """Set the connection token for the next connect (e.g. a new one after
        the old one expired, or "" on logout so that the next connect calls
        get_token). Without get_token it also clears the "token expired" state,
        so that set_token() + connect() works after it."""
        self._token = token
        if self._get_token is None:
            self._refresh_required = False

    async def connect(self) -> None:
        """Initiate connection to server."""
        if self.state in {ClientState.CONNECTING, ClientState.CONNECTED}:
            return

        self.state = ClientState.CONNECTING
        self._reconnect_attempts = 0
        self._transition += 1
        transition = self._transition
        if self._connected_future.done():
            self._connected_future = asyncio.Future()

        code = _ConnectingCode.CONNECT_CALLED
        await self._ev.on_connecting(
            ConnectingContext(code=_code_number(code), reason=_code_message(code))
        )
        # A handler may have disconnected (or raised, which disconnects).
        if self._transition != transition or self.state != ClientState.CONNECTING:
            return
        # The attempt runs in a task of its own: cancelling connect() (e.g. with
        # asyncio.wait_for) doesn't abandon it half-way, it continues and the
        # client reconnects as usual.
        await asyncio.shield(self._spawn(self._create_connection()))

    async def disconnect(self) -> None:
        """Disconnect from server."""
        if self.state == ClientState.DISCONNECTED:
            return

        code = _DisconnectedCode.DISCONNECT_CALLED
        await self._disconnect(_code_number(code), _code_message(code), False)

    async def _refresh(self, connection: Optional[int] = None) -> None:
        """One step of the connection token refresh chain. The chain belongs to the
        connection it was started for: every step stops once that connection is
        over, so a token request or refresh command still in flight across a
        reconnect does not refresh the next connection, which runs its own chain."""
        if connection is None:
            connection = self._transport_id
        if not self._connection_current(connection) or not self._get_token:
            return

        try:
            token = await self._get_token()
        except asyncio.CancelledError as e:
            if _task_cancelling():
                raise
            await self._refresh_token_failed(connection, e)
            return
        except Exception as e:
            await self._refresh_token_failed(connection, e)
            return

        if not self._connection_current(connection):
            return
        if not token:
            # As in centrifuge-js: no token to refresh the connection with.
            code = _DisconnectedCode.UNAUTHORIZED
            await self._disconnect(_code_number(code), _code_message(code), False)
            return
        self._token = token

        cmd_id = self._next_command_id()
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
            if not self._connection_current(connection):
                return
            handler = self._ev.on_error
            await handler(
                ErrorContext(code=_code_number(_ErrorCode.CLIENT_REFRESH_TOKEN), error=e),
            )
            # The command did not reach the server (timeout, transport closed) -
            # retry, otherwise the connection is left with no refresh scheduled.
            self._schedule_refresh_retry(connection)
            return

        if not self._connection_current(connection):
            return

        if reply.get("error"):
            code, message, temporary = self._extract_error_details(reply)
            if not temporary:
                # Terminal error: retrying can not help, so go to the disconnected
                # state as the SDK spec requires for terminal conditions met while
                # connected.
                await self._disconnect(code, message, False)
                return
            handler = self._ev.on_error
            await handler(
                ErrorContext(
                    code=_code_number(_ErrorCode.CLIENT_REFRESH_TOKEN),
                    error=ReplyError(code, message, temporary),
                ),
            )
            self._schedule_refresh_retry(connection)
            return

        refresh = reply["refresh"]
        expires = refresh.get("expires", False)
        if expires:
            ttl = refresh["ttl"]
            # A new step replaces the pending one.
            if self._refresh_timer:
                self._refresh_timer.cancel()
            self._refresh_timer = self._loop.call_later(
                ttl,
                lambda: self._spawn(self._refresh(connection)),
            )

    async def _refresh_token_failed(self, connection: int, e: BaseException) -> None:
        if not self._connection_current(connection):
            return
        if isinstance(e, UnauthorizedError):
            code = _DisconnectedCode.UNAUTHORIZED
            await self._disconnect(_code_number(code), _code_message(code), False)
            return
        handler = self._ev.on_error
        await handler(
            ErrorContext(code=_code_number(_ErrorCode.CLIENT_REFRESH_TOKEN), error=e),
        )
        self._schedule_refresh_retry(connection)

    def _schedule_refresh_retry(self, connection: int) -> None:
        """Re-arm the connection token refresh after a temporary failure.

        Without it a single transient error (a failing get_token callback, a lost
        or timed out refresh command, a temporary server error) would leave the
        connection with no further refresh attempts until the server drops it as
        expired, while the SDK spec promises a retry after some jittered time.
        """
        if not self._connection_current(connection) or not self._get_token:
            return
        if self._refresh_timer:
            self._refresh_timer.cancel()
        delay = _backoff(0, _REFRESH_RETRY_MIN_DELAY, _REFRESH_RETRY_MAX_DELAY)
        logger.debug("retry connection token refresh in %f", delay)
        self._refresh_timer = self._loop.call_later(
            delay,
            lambda: self._spawn(self._refresh(connection)),
        )

    async def _sub_refresh(self, channel: str, session: Optional[int] = None):
        """One step of a subscription token refresh chain. The chain belongs to the
        subscribed session (and connection) it was started for: a token that
        arrives after the subscription resubscribed or the client reconnected is
        not used - the new session runs its own chain."""
        sub = self._subs.get(channel)
        if not sub:
            return
        if session is None:
            session = sub._attempt
        connection = self._transport_id

        def current() -> bool:
            return sub._session_current(session) and self._connection_current(connection)

        if not current() or not sub._get_token:
            return

        try:
            token = await sub._get_token(channel)
        except asyncio.CancelledError as e:
            if _task_cancelling():
                raise
            await self._sub_refresh_token_failed(sub, session, current, e)
            return
        except Exception as e:
            await self._sub_refresh_token_failed(sub, session, current, e)
            return

        if not current():
            return
        if not token:
            # As in centrifuge-js: no token to refresh the subscription with.
            code = _UnsubscribedCode.UNAUTHORIZED
            await sub._move_unsubscribed(
                _code_number(code), _code_message(code), send_unsubscribe_command=True
            )
            return

        cmd_id = self._next_command_id()
        sub._token = token
        command = {
            "id": cmd_id,
            "sub_refresh": {
                "channel": channel,
                "token": token,
            },
        }

        future = self._register_future(cmd_id, self._timeout)
        await self._send_commands([command])

        try:
            reply = await future
        except Exception as e:
            if not current():
                return
            handler = sub._ev.on_error
            await handler(
                SubscriptionErrorContext(
                    code=_code_number(_ErrorCode.SUBSCRIPTION_REFRESH_TOKEN),
                    error=e,
                ),
            )
            # The command did not reach the server (timeout, transport closed) -
            # retry, otherwise the subscription is left with no refresh scheduled.
            self._schedule_sub_refresh_retry(sub, session)
            return

        if not current():
            return

        if reply.get("error"):
            code, message, temporary = self._extract_error_details(reply)
            if not temporary:
                # Terminal error: retrying can not help, so the subscription goes
                # to the unsubscribed state, like on a terminal subscribe error.
                await sub._move_unsubscribed(code, message, send_unsubscribe_command=True)
                return
            handler = sub._ev.on_error
            await handler(
                SubscriptionErrorContext(
                    code=_code_number(_ErrorCode.SUBSCRIPTION_REFRESH_TOKEN),
                    error=ReplyError(code, message, temporary),
                ),
            )
            self._schedule_sub_refresh_retry(sub, session)
            return

        sub_refresh = reply["sub_refresh"]
        expires = sub_refresh.get("expires", False)
        if expires:
            ttl = sub_refresh["ttl"]
            # A new step replaces the pending one.
            if sub._refresh_timer:
                sub._refresh_timer.cancel()
            sub._refresh_timer = self._loop.call_later(
                ttl,
                lambda: self._spawn(sub._refresh(session)),
            )

    async def _sub_refresh_token_failed(
        self, sub: "Subscription", session: int, current: Callable[[], bool], e: BaseException
    ) -> None:
        if not current():
            return
        if isinstance(e, UnauthorizedError):
            code = _UnsubscribedCode.UNAUTHORIZED
            await sub._move_unsubscribed(
                _code_number(code),
                _code_message(code),
                send_unsubscribe_command=True,
            )
            return
        handler = sub._ev.on_error
        await handler(
            SubscriptionErrorContext(
                code=_code_number(_ErrorCode.SUBSCRIPTION_REFRESH_TOKEN),
                error=e,
            ),
        )
        self._schedule_sub_refresh_retry(sub, session)

    def _schedule_sub_refresh_retry(self, sub: "Subscription", session: int) -> None:
        """Re-arm the subscription token refresh after a temporary failure.

        The subscription counterpart of _schedule_refresh_retry above.
        """
        if not sub._session_current(session) or not sub._get_token:
            return
        if sub._refresh_timer:
            sub._refresh_timer.cancel()
        delay = _backoff(0, _SUB_REFRESH_RETRY_MIN_DELAY, _SUB_REFRESH_RETRY_MAX_DELAY)
        logger.debug("retry subscription token refresh for %s in %f", sub.channel, delay)
        sub._refresh_timer = self._loop.call_later(
            delay,
            lambda: self._spawn(sub._refresh(session)),
        )

    @staticmethod
    def _extract_error_details(reply):
        error = reply["error"]
        return error["code"], error["message"], error.get("temporary", False)

    async def _subscribe(self, channel):
        sub = self._subs.get(channel)
        if not sub:
            return None
        connection = self._transport_id
        try:
            return await self._subscribe_attempt(sub, channel)
        except Exception as e:
            # An event handler of the app raised (handlers run as part of the
            # subscribe flow): the subscription must not be left subscribing with
            # nothing in progress.
            logger.exception("exception during subscribe to %s", channel)
            if not self._connection_current(connection):
                return None
            if sub.state == SubscriptionState.SUBSCRIBING:
                await sub._schedule_resubscribe()
            else:
                await self._handler_failed(connection, e)
            return None

    async def _subscribe_attempt(self, sub: "Subscription", channel: str):
        if self.state != ClientState.CONNECTED:
            logger.debug("skip subscribe to %s until connected", channel)
            return None
        # Started for a subscription that subscribed (or unsubscribed) meanwhile,
        # e.g. by a resubscribe timer: nothing to do.
        if sub.state != SubscriptionState.SUBSCRIBING:
            return None
        # At most one subscribe flow per subscription and connection (a resubscribe
        # timer, the reconnect and subscribe() may each start one), as in
        # centrifuge-js: a second one would make the first one's reply stale, and
        # the server would answer the second subscribe "already subscribed".
        if sub._inflight == (self._transport_id, sub._attempt):
            return None

        logger.debug("subscribe to channel %s", channel)

        # Every step below acts only for this subscribe attempt over this
        # connection: after unsubscribe() + subscribe(), a resubscribe or a
        # reconnect, the attempt is over - its token, state or reply is not used.
        attempt = sub._next_attempt()
        connection = self._transport_id

        def current() -> bool:
            return (
                sub._attempt == attempt
                and sub.state == SubscriptionState.SUBSCRIBING
                and self._connection_current(connection)
            )

        marker = (connection, attempt)
        sub._inflight = marker
        try:
            return await self._subscribe_steps(sub, channel, attempt, current)
        finally:
            if sub._inflight == marker:
                sub._inflight = None

    async def _subscribe_steps(
        self, sub: "Subscription", channel: str, attempt: int, current: Callable[[], bool]
    ):
        # get_state: ask the app for its current state position. Only called when
        # we don't have a saved position (first subscribe or after a position reset
        # due to unrecoverable position error 112). On normal reconnects with a
        # valid saved position we skip get_state and let the server try recovery —
        # get_state is only called again if recovery fails.
        if sub._get_state and not sub._need_recover():
            proceed = await self._load_state_position(sub, channel, attempt, current)
            if not proceed:
                return None

        if not sub._token and sub._get_token:
            try:
                token = await sub._get_token(channel)
            except asyncio.CancelledError as e:
                if _task_cancelling():
                    raise
                await self._subscribe_token_failed(sub, current, e)
                return False
            except Exception as e:
                await self._subscribe_token_failed(sub, current, e)
                return False

            # A token requested by an attempt that is over is not used.
            if not current():
                logger.debug("subscribe attempt for %s is over after token fetch", channel)
                return None
            if not token:
                # As in centrifuge-js: no token to subscribe with.
                code = _UnsubscribedCode.UNAUTHORIZED
                await sub._move_unsubscribed(_code_number(code), _code_message(code))
                return False
            sub._token = token

        cmd_id = self._next_command_id()
        command = self._construct_subscribe_command(sub, cmd_id)
        async with self._register_future_with_done(cmd_id) as future:
            await self._send_commands([command])
            try:
                reply = await future
            except ClientDisconnectedError:
                # The connection is gone; the subscription resubscribes once the
                # client is connected again. Not a subscribe failure.
                return None
            except OperationTimeoutError as e:
                if not current():
                    return None
                handler = sub._ev.on_error
                await handler(
                    SubscriptionErrorContext(code=_code_number(_ErrorCode.TIMEOUT), error=e),
                )
                # The server may have subscribed (the reply was lost or late): a
                # subscribe retry on this connection would be denied as already
                # subscribed. Reconnect, as centrifuge-js does.
                if current():
                    code = _ConnectingCode.SUBSCRIBE_TIMEOUT
                    await self._disconnect(_code_number(code), _code_message(code), True)
                return None
            except Exception as e:
                if not current():
                    return None
                handler = sub._ev.on_error
                await handler(
                    SubscriptionErrorContext(
                        code=_code_number(_ErrorCode.SUBSCRIBE_ERROR),
                        error=e,
                    ),
                )
                await sub._schedule_resubscribe()
                return None

            # A reply to an attempt that is over (e.g. before unsubscribe() +
            # subscribe()) is not applied to the newer one.
            if not current():
                return None

            if reply.get("error"):
                logger.debug("subscribe reply has error: %s", reply.get("error"))
                code, message, temporary = self._extract_error_details(reply)
                if code == _ERROR_CODE_UNRECOVERABLE_POSITION and sub._get_state:
                    # Unrecoverable position with get_state: reset position so the
                    # next subscribe attempt calls get_state to reload app state
                    # from scratch. No error event emitted — matches other SDKs.
                    sub._recover = False
                    sub._offset = 0
                    sub._epoch = ""
                    sub._prev_data = None
                    await sub._schedule_resubscribe()
                    return False
                if _is_token_expired(code) and sub._get_token is None:
                    # The token expired and there is no get_token to get another
                    # one: unauthorized, like a connection (instead of
                    # subscribing again without a token, which the server denies).
                    handler = sub._ev.on_error
                    await handler(
                        SubscriptionErrorContext(
                            code=_code_number(_ErrorCode.BAD_CONFIGURATION),
                            error=CentrifugeError(
                                "subscription token expired but no get_token function set"
                            ),
                        ),
                    )
                    if current():
                        unauthorized = _UnsubscribedCode.UNAUTHORIZED
                        await sub._move_unsubscribed(
                            _code_number(unauthorized), _code_message(unauthorized)
                        )
                    return False
                if _is_token_expired(code):
                    temporary = True
                    sub._token = ""
                if temporary:
                    handler = sub._ev.on_error
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

    @staticmethod
    async def _subscribe_token_failed(
        sub: "Subscription", current: Callable[[], bool], e: BaseException
    ) -> None:
        if not current():
            return
        if isinstance(e, UnauthorizedError):
            code = _UnsubscribedCode.UNAUTHORIZED
            await sub._move_unsubscribed(_code_number(code), _code_message(code))
            return
        handler = sub._ev.on_error
        await handler(
            SubscriptionErrorContext(
                code=_code_number(_ErrorCode.SUBSCRIPTION_SUBSCRIBE_TOKEN),
                error=e,
            ),
        )
        if current():
            await sub._schedule_resubscribe()

    async def _load_state_position(
        self, sub: "Subscription", channel: str, attempt: int, current: Callable[[], bool]
    ) -> bool:
        """Run sub._get_state and store the returned position on the subscription.

        Returns True when the subscribe flow may proceed, False when it must stop
        (get_state failed and a retry was scheduled, or subscription/client state
        changed during the await).
        """
        try:
            position = await sub._get_state(channel)
        except Exception as e:
            if not current():
                return False
            handler = sub._ev.on_error
            await handler(
                SubscriptionErrorContext(
                    code=_code_number(_ErrorCode.SUBSCRIPTION_GET_STATE),
                    error=e,
                ),
            )
            self._spawn(sub._schedule_resubscribe())
            return False

        # A position loaded for an attempt that is over is not used: a later
        # attempt may have recovered already, and this one would rewind it.
        if sub._attempt != attempt or sub.state != SubscriptionState.SUBSCRIBING:
            logger.debug("subscribe attempt for %s is over after get_state", channel)
            return False

        sub._recover = True
        sub._offset = position.offset
        sub._epoch = position.epoch

        # Re-check the connection after async get_state(). The loaded position is
        # kept (matches other SDKs): the resubscribe on reconnect recovers from
        # it instead of calling get_state again.
        if not current():
            logger.debug("client disconnected during get_state for %s", channel)
            return False

        return True

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

        # Always offer channel compaction: when the server supports and allows it,
        # the subscribe result carries a numeric channel ID and subsequent pushes
        # use that ID instead of the string channel name.
        flag = _SUBSCRIPTION_FLAG_CHANNEL_COMPACTION
        if sub._get_state:
            # Ask the server to reject the subscribe with error 112 when recovery
            # from the provided position is impossible, instead of returning
            # recovered=false — so we can call get_state again to reload state.
            flag |= _SUBSCRIPTION_FLAG_REJECT_UNRECOVERED
        subscribe["flag"] = flag

        if sub._delta:
            subscribe["delta"] = sub._delta.value

        if sub._tags_filter is not None:
            subscribe["tf"] = sub._tags_filter.node

        command = {
            "id": cmd_id,
            "subscribe": subscribe,
        }
        return command

    async def _resubscribe(self, sub: "Subscription"):
        self._subs[sub.channel] = sub
        self._spawn(self._subscribe(sub.channel))

    async def _unsubscribe(self, sub: "Subscription"):

        if self.state != ClientState.CONNECTED:
            # Server-side subscriptions do not outlive the connection, so there is
            # nothing to unsubscribe from. Sending anyway would fail without a
            # transport, wait for the whole timeout over a closed one, and get the
            # client disconnected by the server before the connect reply.
            return

        unsubscribe = {"channel": sub.channel}

        cmd_id = self._next_command_id()
        command = {
            "id": cmd_id,
            "unsubscribe": unsubscribe,
        }
        future = self._register_future(cmd_id, self._timeout)
        connection = self._transport_id

        await self._send_commands([command])
        # The reply is handled on its own: unsubscribe() may be called from an
        # event handler, i.e. from the task that processes replies.
        self._spawn(self._unsubscribe_result(future, connection))

    async def _unsubscribe_result(self, future: asyncio.Future, connection: int) -> None:
        try:
            reply = await future
        except ClientDisconnectedError:
            # The connection was lost before the reply - the subscription is gone
            # on the server together with it.
            return
        except OperationTimeoutError:
            reply = None
        if not self._connection_current(connection):
            return
        if reply is None or reply.get("error"):
            # The server may still hold the subscription (and keep sending its
            # publications): reconnect, as centrifuge-js does.
            code = _ConnectingCode.UNSUBSCRIBE_ERROR
            await self._disconnect(_code_number(code), _code_message(code), True)

    def _register_future(self, cmd_id: int, timeout: float) -> asyncio.Future:
        future = asyncio.Future()

        th = None
        if timeout:

            def cb():
                if not future.done():
                    self._future_error(cmd_id, OperationTimeoutError())

            th = _TimeoutTimer(self._loop, timeout, cb)

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

            th = _TimeoutTimer(self._loop, self._timeout, cb)

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
        cb = self._inflight_commands.pop(cmd_id, None)
        if not cb:
            return
        # The future is already done (cancelled) when the coroutine awaiting it
        # was cancelled, e.g. by asyncio.wait_for() - resolving it again raises.
        if not cb.future.done():
            cb.future.set_exception(exc)
        if cb.done and not cb.done.done():
            cb.done.set_result(True)
        # Cancel timeout timer to prevent timer leak
        if cb.timeout:
            cb.timeout.cancel()

    async def _future_success(self, cmd_id: int, reply):
        cb = self._inflight_commands.pop(cmd_id, None)
        if not cb:
            return
        # See _future_error: the awaiting coroutine may have been cancelled.
        if not cb.future.done():
            cb.future.set_result(reply)
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
        if self.state == ClientState.DISCONNECTED:
            raise ClientDisconnectedError("client disconnected")

    async def ready(self, timeout: Optional[float] = None) -> None:
        self._check_state()
        future = self._connected_future
        completed = await _wait_for_future(future, timeout=timeout or self._timeout)
        if not completed:
            raise OperationTimeoutError("timeout waiting for connection to be ready")
        # Raises when the wait ended with disconnect().
        future.result()

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
        """Teardown: close the transport, fail pending commands, move to connecting
        (to reconnect) or disconnected, and emit events. The transport is closed
        before any event, and nothing from it is processed afterwards."""
        if self.state == ClientState.DISCONNECTED:
            return

        if code == _DISCONNECTED_STATE_INVALIDATED:
            # State invalidated (delivered as a WebSocket close frame or a
            # Disconnect push, both funnel here): drop the connection token so the
            # next connect fetches a fresh one via get_token, and invalidate every
            # subscription's cached state before reconnecting.
            self._invalidate_connection_state()

        previous_state = self.state
        # Server-side publications being handled count as delivered.
        for channel, (connection, offset) in self._server_pending.items():
            server_sub = self._server_subs.get(channel)
            if connection == self._transport_id and server_sub is not None:
                server_sub.offset = offset
        self._server_pending.clear()
        # Abandons the attempt in progress, or the connection.
        self._transport_id += 1
        self._transition += 1
        transition = self._transition

        if self._ping_timer:
            logger.debug("canceling ping timer")
            self._ping_timer.cancel()
            self._ping_timer = None

        if self._refresh_timer:
            logger.debug("canceling refresh timer")
            self._refresh_timer.cancel()
            self._refresh_timer = None

        if self._reconnect_timer:
            self._reconnect_timer.cancel()
            self._reconnect_timer = None

        self._stop_transport_tasks()
        self._close_transport()
        self._clear_outgoing_futures()

        if self._connected_future.done():
            self._connected_future = asyncio.Future()

        if reconnect:
            self.state = ClientState.CONNECTING
        else:
            self.state = ClientState.DISCONNECTED
            self._connected_future.set_exception(
                ClientDisconnectedError(f"client disconnected: {code} ({reason})"),
            )
            # Mark the exception retrieved: nobody may be waiting for it.
            self._connected_future.exception()

        if previous_state == ClientState.CONNECTED:
            try:
                await self._clear_connected_state(transition)
            except Exception:
                # An event handler raised: the teardown must still complete (and
                # reconnect), or the client is left connecting with nothing
                # scheduled.
                logger.exception("exception in event handler during disconnect")
            if self._transition != transition:
                return

        # As in centrifuge-js and the SDK spec: a connection that is lost and
        # going to reconnect moves to connecting (with the reason); disconnected
        # is only for disconnect() and terminal codes. A failed attempt that
        # retries stays connecting, without an event.
        try:
            await self._emit_teardown_event(code, reason, reconnect, previous_state)
        except Exception:
            logger.exception("exception in event handler during disconnect")
        if self._transition != transition:
            return

        if reconnect:
            await self._schedule_reconnect()

    async def _emit_teardown_event(
        self, code: int, reason: str, reconnect: bool, previous_state: ClientState
    ) -> None:
        if not reconnect:
            handler = self._ev.on_disconnected
            await handler(DisconnectedContext(code=code, reason=reason))
        elif previous_state == ClientState.CONNECTED:
            handler = self._ev.on_connecting
            await handler(ConnectingContext(code=code, reason=reason))

    def _stop_transport_tasks(self) -> None:
        # The reader is cancelled - unless the teardown runs in it (a close
        # detected by the reader). The message processor is never cancelled: it
        # may be running an event handler of the app. It stops before the next
        # message (a sentinel wakes it up) and doesn't process the rest of the
        # current frame.
        current = asyncio.current_task()
        if self._listen_task is not None and self._listen_task is not current:
            self._listen_task.cancel()
        self._listen_task = None
        self._messages.put_nowait(None)
        self._messages = asyncio.Queue()
        self._process_messages_task = None

    def _invalidate_connection_state(self) -> None:
        # Without get_token there is no new token to get: keep the token the
        # client has (or none) and reconnect with it - the server rejects it if
        # it's no longer valid.
        if self._get_token is not None:
            self._token = ""
            self._refresh_required = True
        for sub in self._subs.values():
            sub._invalidate_state()
        # Positions of publications being handled are invalidated too.
        self._server_pending.clear()
        for server_sub in self._server_subs.values():
            # Reset cached recovery position to a sentinel the server can never
            # match, mirroring _invalidate_state() above for client-side subs.
            # Without this, _construct_connect_command() would keep sending the
            # stale pre-invalidation offset/epoch as recovery params on every
            # reconnect, defeating state invalidation for server-side subs.
            server_sub.offset = 0
            server_sub.epoch = "_"

    async def _no_ping(self) -> None:
        code = _ConnectingCode.NO_PING
        await self._disconnect(_code_number(code), _code_message(code), True)

    def _restart_ping_wait(self) -> None:
        if self._ping_timer:
            self._ping_timer.cancel()
        self._ping_timer = _TimeoutTimer(
            self._loop,
            self._ping_interval + self._max_server_ping_delay,
            lambda: self._spawn(self._no_ping()),
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
        conn = self._conn
        if conn is None:
            # No transport (a teardown happened meanwhile): the commands fail
            # like the ones already sent when the connection was lost.
            for command in commands:
                cmd_id = command.get("id")
                if cmd_id:
                    error = ClientDisconnectedError(f"command {cmd_id} canceled, no connection")
                    self._future_error(cmd_id, error)
            return
        logger.debug("send commands: %s", commands)
        encoded = self._codec.encode_commands(commands)
        try:
            await conn.send(encoded)
        except exceptions.ConnectionClosed as e:
            logger.debug("connection closed with error during send: %s", e)
            if self._conn is not conn:
                return
            code = _ConnectingCode.TRANSPORT_CLOSED
            await self._disconnect(_code_number(code), _code_message(code), True)
        except exceptions.ProtocolError as e:
            logger.error("protocol error during send: %s", e)
            if self._conn is not conn:
                return
            handler = self._ev.on_error
            await handler(ErrorContext(code=_code_number(_ErrorCode.TRANSPORT_CLOSED), error=e))
            code = _ConnectingCode.TRANSPORT_CLOSED
            await self._disconnect(_code_number(code), _code_message(code), True)

    async def _process_unsubscribe(self, channel: str, unsubscribe: Dict[str, Any]) -> None:
        # Only for a subscribed subscription: an unsubscribe push that arrives
        # while a subscribe is in progress is for the previous subscription, and
        # one after unsubscribe() must not resubscribe. Applied before the next
        # message of the frame is processed.
        sub = self._subscribed_sub_for_push(channel, 0)
        code = unsubscribe["code"]
        if sub:
            if code < 2500:
                await sub._move_unsubscribed(code, unsubscribe["reason"])
            else:
                if code == _UNSUBSCRIBED_STATE_INVALIDATED:
                    # State invalidated: drop the subscription token and cached
                    # state so the resubscribe obtains a fresh token and re-syncs.
                    sub._invalidate_state()
                await sub._move_subscribing(code, unsubscribe["reason"])
        else:
            server_sub = self._server_subs.get(channel)
            if server_sub:
                del self._server_subs[channel]
                handler = self._ev.on_unsubscribed
                await handler(ServerUnsubscribedContext(channel=channel))

    async def _process_subscribe(self, channel: str, subscribe: Dict[str, Any]) -> None:
        # A server-side subscription added while connected (e.g. with the server
        # API's subscribe): from now on its publications come with the channel.
        recoverable = subscribe.get("recoverable", False)
        positioned = subscribe.get("positioned", False)
        offset = int(subscribe.get("offset", 0))
        epoch = subscribe.get("epoch", "")
        self._server_subs[channel] = _ServerSubscription(
            offset=offset, epoch=epoch, recoverable=recoverable
        )
        stream_position = None
        if positioned or recoverable:
            stream_position = StreamPosition(offset=offset, epoch=epoch)
        handler = self._ev.on_subscribed
        await handler(
            ServerSubscribedContext(
                channel=channel,
                recoverable=recoverable,
                positioned=positioned,
                stream_position=stream_position,
                was_recovering=False,
                recovered=False,
                data=self._decode_data(subscribe.get("data")),
            ),
        )

    async def _process_disconnect(self, disconnect: Dict[str, Any]) -> None:
        logger.debug("disconnect push received")
        code = disconnect["code"]
        reconnect = not ((3500 <= code < 4000) or (4500 <= code < 5000))
        await self._disconnect(code, disconnect["reason"], reconnect)

    async def _process_reply(self, reply: Dict[str, Any]) -> None:
        if reply.get("id", 0) > 0:
            await self._future_success(reply["id"], reply)
        elif reply.get("push"):
            logger.debug("received push reply %s", reply)
            push = reply["push"]
            # Channel compaction: pub/join/leave pushes may carry a numeric channel
            # ID instead of the channel name. Other push types always carry the
            # channel. int(...) because protobuf int64 decodes to a string.
            push_id = int(push.get("id", 0))
            if "pub" in push:
                await self._process_publication(push.get("channel", ""), push["pub"], push_id)
            elif "join" in push:
                await self._process_join(push.get("channel", ""), push["join"], push_id)
            elif "leave" in push:
                await self._process_leave(push.get("channel", ""), push["leave"], push_id)
            elif "unsubscribe" in push:
                await self._process_unsubscribe(push["channel"], push["unsubscribe"])
            elif "subscribe" in push:
                await self._process_subscribe(push["channel"], push["subscribe"])
            elif "disconnect" in push:
                await self._process_disconnect(push["disconnect"])
            else:
                logger.debug("skip unknown push reply %s", reply)
        else:
            await self._handle_ping()

    def _sub_for_push(self, channel: str, push_id: int) -> Optional["Subscription"]:
        # Channel compaction: route by numeric channel ID when the push carries
        # one (the channel name is then omitted), by channel name otherwise. Only
        # client-side subscriptions negotiate compaction.
        if push_id > 0:
            return self._subs_by_id.get(push_id)
        return self._subs.get(channel)

    def _update_subscription_push_id(self, sub: "Subscription", old_id: int, new_id: int) -> None:
        # Remove the old numeric ID mapping (only if it still points to this
        # subscription) and register the new one. Either ID may be 0 (no mapping).
        if old_id > 0 and self._subs_by_id.get(old_id) is sub:
            del self._subs_by_id[old_id]
        if new_id > 0:
            self._subs_by_id[new_id] = sub

    def _subscribed_sub_for_push(self, channel: str, push_id: int) -> Optional["Subscription"]:
        # The client-side subscription a push is for: only a subscribed one. A
        # push for a subscription that is subscribing or unsubscribed belongs to
        # a subscribe session that is over (or not established yet) and must not
        # be applied - it would deliver events the app no longer expects and
        # move the stream position. None means the push goes to a server-side
        # subscription of the channel, if any (compacted pushes with a numeric
        # ID are only ever for client-side subscriptions).
        sub = self._sub_for_push(channel, push_id)
        if sub is not None and sub.state == SubscriptionState.SUBSCRIBED:
            return sub
        return None

    async def _process_publication(self, channel: str, pub: Any, push_id: int = 0) -> None:
        sub = self._subscribed_sub_for_push(channel, push_id)
        if sub:
            await sub._process_publication(pub)
        elif push_id == 0 and channel in self._server_subs:
            await self._process_server_publication(channel, pub)

    async def _process_join(self, channel: str, join: Any, push_id: int = 0) -> None:
        client_info = self._extract_client_info(join["info"])
        sub = self._subscribed_sub_for_push(channel, push_id)
        if sub:
            await sub._ev.on_join(JoinContext(info=client_info))
        elif push_id == 0 and channel in self._server_subs:
            await self._ev.on_join(ServerJoinContext(channel=channel, info=client_info))

    async def _process_leave(self, channel: str, leave: Any, push_id: int = 0) -> None:
        client_info = self._extract_client_info(leave["info"])
        sub = self._subscribed_sub_for_push(channel, push_id)
        if sub:
            await sub._ev.on_leave(LeaveContext(info=client_info))
        elif push_id == 0 and channel in self._server_subs:
            await self._ev.on_leave(ServerLeaveContext(channel=channel, info=client_info))

    def _extract_client_info(self, info: Any) -> ClientInfo:
        return ClientInfo(
            client=info.get("client", ""),
            user=info.get("user", ""),
            conn_info=self._decode_data(info.get("conn_info")),
            chan_info=self._decode_data(info.get("chan_info")),
        )

    async def _process_incoming_data(self, message: bytes, attempt: int) -> None:
        logger.debug("start parsing message: %s", message)
        replies = self._codec.decode_replies(message)
        logger.debug("got %d replies", len(replies))
        for reply in replies:
            # A message earlier in this frame ended the connection (a disconnect
            # push, or disconnect() called from an event handler): the rest of
            # the frame belongs to it.
            if self._transport_id != attempt:
                return
            logger.debug("got reply %s", reply)
            await self._process_reply(reply)

    async def _process_messages(self, queue: asyncio.Queue, attempt: int) -> None:
        logger.debug("start message processing routine")
        while True:
            message = await queue.get()
            if message is None or self._transport_id != attempt:
                return
            logger.debug("start processing message: %s", message)
            try:
                await self._process_incoming_data(message, attempt)
            except Exception as e:
                # An event handler of the app raised, or the message can't be
                # decoded: the client can't know what was applied. It used to stop
                # reading silently; now it disconnects visibly, as centrifuge-js.
                logger.exception("exception during message handling")
                await self._handler_failed(attempt, e)
                return

    async def _listen(self, conn: "ClientConnection", queue: asyncio.Queue, attempt: int) -> None:
        logger.debug("start reading connection")
        while True:
            try:
                result = await conn.recv()
            except exceptions.ConnectionClosed as e:
                logger.debug("connection closed with error during recv: %s", e)
                break
            except exceptions.ProtocolError as e:
                logger.error("protocol error during recv: %s", e)
                break
            if self._transport_id != attempt:
                return
            if result:
                logger.debug("data received %s", result)
                queue.put_nowait(result)

        # Closed by the client itself in a teardown: nothing to report.
        if self._transport_id != attempt:
            return

        logger.debug("stop reading connection")

        ws_code = conn.close_code
        ws_reason = conn.close_reason
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
        tags_filter: Optional[FilterNode] = None,
        get_state: Optional[Callable[[str], Awaitable[StreamPosition]]] = None,
    ) -> None:
        """Initializes Subscription instance.
        Note: use Client.new_subscription method to create new subscriptions in your app.
        """
        self.channel = channel
        self.state = SubscriptionState.UNSUBSCRIBED
        self.events = events or SubscriptionEventHandler()
        self._ev = _GuardedEvents(
            lambda: self.events, lambda: self._client, self._publication_failed
        )
        self._subscribed_future: asyncio.Future[bool] = asyncio.Future()
        self._subscribed = False
        self._client: Optional[Client] = client
        self._token = token
        self._get_token = get_token
        self._get_state = get_state
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
        # Numeric channel ID assigned by the server when channel compaction is
        # negotiated. Pushes then carry this ID instead of the channel name.
        self._push_id: int = 0
        # Identifies the current subscribe attempt, or the current subscribed
        # session. Changes on every subscribe attempt and every state change;
        # token and state requests, subscribe replies and refresh steps check it
        # and stop when theirs is over.
        self._attempt: int = 0
        # The publication an event handler is processing, as (session, offset):
        # it counts as delivered when the subscription leaves the session during
        # the handler (e.g. unsubscribe() + subscribe() from it), so the next
        # subscribe recovers after it.
        self._pending: Optional[Tuple[int, int]] = None
        # The subscribe flow in progress, as (connection, attempt).
        self._inflight: Optional[Tuple[int, int]] = None

        if delta and delta not in {DeltaType.FOSSIL}:
            raise CentrifugeError("unsupported delta format")
        if delta and tags_filter is not None:
            raise CentrifugeError("cannot use delta and tags filter together")
        self._delta = delta
        self._delta_negotiated: bool = False
        self._tags_filter = tags_filter

    @classmethod
    def _create_instance(cls, *args: Any, **kwargs: Any) -> "Subscription":
        obj = cls.__new__(cls)
        obj._initialize(*args, **kwargs)
        return obj

    def _check_state(self):
        if self._client is None:
            raise CentrifugeError(
                f"subscription to {self.channel} was removed from the client, create a new one"
            )
        if self.state == SubscriptionState.UNSUBSCRIBED:
            raise SubscriptionUnsubscribedError("subscription unsubscribed")

    async def ready(self, timeout: Optional[float] = None) -> None:
        self._check_state()
        future = self._subscribed_future
        completed = await _wait_for_future(future, timeout=timeout or self._client._timeout)
        if not completed:
            raise OperationTimeoutError("timeout waiting for subscription to be ready")
        # Raises when the wait ended with unsubscribe.
        future.result()

    async def history(
        self,
        limit: int = 0,
        since: Optional[StreamPosition] = None,
        reverse: bool = False,
        timeout: Optional[float] = None,
    ) -> HistoryResult:
        # The client is taken before waiting: the subscription may be removed
        # meanwhile, and the call goes on (as in centrifuge-js).
        client = self._client
        await self.ready(timeout=timeout)
        return await client.history(
            self.channel,
            limit=limit,
            since=since,
            reverse=reverse,
            timeout=timeout,
        )

    async def presence(self, timeout: Optional[float] = None) -> PresenceResult:
        client = self._client
        await self.ready(timeout=timeout)
        # noinspection PyProtectedMember
        return await client.presence(self.channel, timeout=timeout)

    async def presence_stats(self, timeout: Optional[float] = None) -> PresenceStatsResult:
        client = self._client
        await self.ready(timeout=timeout)
        # noinspection PyProtectedMember
        return await client.presence_stats(self.channel, timeout=timeout)

    async def publish(self, data: Any, timeout: Optional[float] = None) -> PublishResult:
        client = self._client
        await self.ready(timeout=timeout)
        # noinspection PyProtectedMember
        return await client.publish(self.channel, data, timeout=timeout)

    def set_tags_filter(self, tags_filter: Optional[FilterNode]) -> None:
        """Sets the server-side publication tags filter. Applied on the next
        subscribe attempt, not the current one. Pass None to clear it. Cannot be
        combined with delta compression. Build with the Filter helpers.
        """
        if tags_filter is not None and self._delta:
            raise CentrifugeError("cannot use delta and tags filter together")
        self._tags_filter = tags_filter

    async def subscribe(self) -> None:
        if self._client is None:
            raise CentrifugeError(
                f"subscription to {self.channel} was removed from the client, create a new one"
            )
        if self.state == SubscriptionState.SUBSCRIBING:
            return
        if self.state == SubscriptionState.SUBSCRIBED:
            return

        self.state = SubscriptionState.SUBSCRIBING
        if self._subscribed_future.done():
            self._subscribed_future = asyncio.Future()

        code = _SubscribingCode.SUBSCRIBE_CALLED
        await self._ev.on_subscribing(
            SubscribingContext(code=_code_number(code), reason=_code_message(code))
        )
        # Not when a handler unsubscribed. A handler that raised stopped the client:
        # the subscribe waits for the next connect.
        if self.state == SubscriptionState.SUBSCRIBING and self._client is not None:
            await self._client._resubscribe(self)

    async def unsubscribe(self) -> None:
        code = _UnsubscribedCode.UNSUBSCRIBE_CALLED
        await self._move_unsubscribed(
            code=_code_number(code),
            reason=_code_message(code),
            send_unsubscribe_command=True,
        )

    def _next_attempt(self) -> int:
        self._attempt += 1
        return self._attempt

    def _session_current(self, session: int) -> bool:
        return self._attempt == session and self.state == SubscriptionState.SUBSCRIBED

    def _publication_failed(self, _ctx: Any) -> None:
        # The publication being handled isn't delivered: the end of the session
        # must not commit it.
        self._pending = None

    def _commit_pending(self) -> None:
        # Called when the subscribed session ends.
        if self._pending is not None and self._pending[0] == self._attempt:
            self._offset = self._pending[1]
        self._pending = None

    def _clear_subscribed_state(self) -> None:
        if self._refresh_timer:
            logger.debug("canceling refresh timer for %s", self.channel)
            self._refresh_timer.cancel()
            self._refresh_timer = None

    def _cancel_resubscribe_timer(self) -> None:
        if self._resubscribe_timer:
            self._resubscribe_timer.cancel()
            self._resubscribe_timer = None

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

        # The handler below may remove the subscription from the client.
        client = self._client

        if self.state == SubscriptionState.SUBSCRIBED:
            self._clear_subscribed_state()
            self._commit_pending()
        elif self.state == SubscriptionState.SUBSCRIBING:
            self._clear_subscribing_state()

        self.state = SubscriptionState.UNSUBSCRIBED
        self._attempt += 1
        # Channel compaction ID is no longer valid once unsubscribed.
        self._set_push_id(0)

        if self._subscribed_future.done():
            self._subscribed_future = asyncio.Future()
        self._subscribed_future.set_exception(
            SubscriptionUnsubscribedError("subscription unsubscribed"),
        )
        await self._consume_subscribed_future()

        # The command goes out before the event, as in centrifuge-js: a handler
        # that raises (the exception propagates to the caller) or subscribes
        # again must not keep it from the server, or reorder it.
        if send_unsubscribe_command and client is not None:
            # noinspection PyProtectedMember
            await client._unsubscribe(self)

        await self._ev.on_unsubscribed(UnsubscribedContext(code=code, reason=reason))

    async def _consume_subscribed_future(self) -> None:
        with contextlib.suppress(CentrifugeError):
            await self._subscribed_future

    def _enter_subscribing(self) -> bool:
        """Move a subscribed subscription to subscribing, without events. Returns
        whether it moved."""
        if self.state != SubscriptionState.SUBSCRIBED:
            return False
        self._clear_subscribed_state()
        self._commit_pending()
        self.state = SubscriptionState.SUBSCRIBING
        self._attempt += 1
        if self._subscribed_future.done():
            self._subscribed_future = asyncio.Future()
        return True

    async def _move_subscribing(
        self,
        code: int,
        reason: str,
        skip_schedule_resubscribe: bool = False,
    ) -> None:
        if self.state == SubscriptionState.SUBSCRIBING:
            return

        if self.state == SubscriptionState.SUBSCRIBED:
            self._enter_subscribing()
        else:
            self.state = SubscriptionState.SUBSCRIBING
            self._attempt += 1
            if self._subscribed_future.done():
                self._subscribed_future = asyncio.Future()

        attempt = self._attempt
        try:
            await self._ev.on_subscribing(SubscribingContext(code=code, reason=reason))
        finally:
            # The resubscribe starts also when the handler raises.
            if (
                not skip_schedule_resubscribe
                and self._attempt == attempt
                and self._client is not None
            ):
                self._client._spawn(self._client._resubscribe(self))

    async def _move_subscribed(self, subscribe: Dict[str, Any]) -> None:
        # Only transition to SUBSCRIBED from SUBSCRIBING state
        if self.state != SubscriptionState.SUBSCRIBING:
            logger.debug(
                "cannot move to subscribed from state %s for channel %s",
                self.state,
                self.channel,
            )
            return

        self.state = SubscriptionState.SUBSCRIBED
        session = self._next_attempt()
        self._subscribed_future.set_result(True)
        on_subscribed_handler = self._ev.on_subscribed
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
            # Cancel existing subscription refresh timer to prevent timer leaks
            if self._refresh_timer:
                self._refresh_timer.cancel()
            self._refresh_timer = self._client._loop.call_later(
                ttl,
                lambda: self._client._spawn(self._refresh(session)),
            )

        self._delta_negotiated = subscribe.get("delta", False)

        # Channel compaction: register the numeric channel ID assigned by the
        # server (0 when not negotiated — also clears a stale ID from a previous
        # subscribe session). int(...) because protobuf int64 decodes to a string.
        self._set_push_id(int(subscribe.get("id", 0)))

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

        self._clear_subscribing_state()

        # The handler may have unsubscribed (or the client disconnected, and the
        # subscription maybe even subscribed again): the recovered publications
        # belong to a subscribed session that is over then.
        for pub in subscribe.get("publications", []):
            if not self._session_current(session):
                break
            await self._process_publication(pub)

    async def _refresh(self, session: Optional[int] = None) -> None:
        if session is None:
            session = self._attempt
        if not self._session_current(session):
            return
        await self._client._sub_refresh(self.channel, session)

    async def _schedule_resubscribe(self) -> None:
        if self.state != SubscriptionState.SUBSCRIBING:
            return

        # Cancel existing resubscribe timer to prevent timer leaks
        if self._resubscribe_timer:
            logger.debug("canceling existing resubscribe timer for %s", self.channel)
            self._resubscribe_timer.cancel()
            self._resubscribe_timer = None

        delay = _backoff(
            self._resubscribe_attempts,
            self._min_resubscribe_delay,
            self._max_resubscribe_delay,
        )
        self._resubscribe_attempts += 1
        logger.debug("start resubscribing in %f", delay)
        self._resubscribe_timer = self._client._loop.call_later(
            delay,
            lambda: self._client._spawn(self._resubscribe()),
        )

    async def _resubscribe(self) -> None:
        if self.state != SubscriptionState.SUBSCRIBING:
            return
        await self._client._resubscribe(self)

    async def _process_publication(self, pub: Any) -> None:
        if self.state != SubscriptionState.SUBSCRIBED:
            return
        session = self._attempt
        publication = self._client._publication_from_proto(pub)

        if self._delta and self._delta_negotiated:
            new_data, prev_data = self._client._codec.apply_delta_if_needed(
                self._prev_data, publication
            )
            publication.data = new_data
            self._prev_data = prev_data

        if publication.offset > 0:
            self._pending = (session, publication.offset)
        client = self._client
        failures = client._handler_failures if client is not None else 0
        try:
            await self._ev.on_publication(PublicationContext(pub=publication))
        finally:
            if self._pending is not None and self._pending[0] == session:
                self._pending = None
        if client is not None and client._handler_failures != failures:
            # The handler raised: the publication isn't delivered, the position
            # stays before it and the client stopped.
            return
        # The position moves after the app has the publication - and only in the
        # session it belongs to: handlers aren't cancelled, and one that finishes
        # after its session ended must not move the position (possibly back).
        if publication.offset > 0 and self._session_current(session):
            self._offset = publication.offset

    def _need_recover(self):
        return self._recover

    def _set_push_id(self, push_id: int) -> None:
        # Update the channel compaction ID registration in the client's push
        # routing registry. Pass 0 to clear (no compaction / sub gone). Always
        # re-registers even when the ID is unchanged: the client drops the registry
        # when the connected state is cleared and on reconnect the server commonly
        # assigns the same ID again, so the registration must be restored.
        if push_id == 0 and self._push_id == 0:
            return
        old_id = self._push_id
        self._push_id = push_id
        self._client._update_subscription_push_id(self, old_id, push_id)

    def _invalidate_state(self) -> None:
        # Reset cached subscription state on "state invalidated" (unsubscribe code
        # 2502 or connection disconnect code 3014) so the resubscribe re-syncs:
        # clear the token (next subscribe fetches a fresh one via get_token), the
        # fossil delta base (a stale base would corrupt decoding of the first
        # publication), and the channel-compaction ID mapping. The recovery
        # position is reset to a sentinel epoch ("_") the server can never match
        # (offset 0); the recover flag is left untouched. So a recoverable
        # subscription resubscribes with was_recovering=True, recovered=False —
        # letting the app reload via its existing recovery-failure path — while a
        # non-recoverable one just resubscribes (the sentinel is not sent). The
        # real epoch/offset are adopted from the subscribe reply. Without get_token
        # there is no new token to get: a static token is kept (the server
        # rejects it if it's no longer valid).
        if self._get_token is not None:
            self._token = ""
        self._offset = 0
        self._epoch = "_"
        self._pending = None
        self._prev_data = None
        self._set_push_id(0)
