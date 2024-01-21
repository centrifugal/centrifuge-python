import uuid
import json
import random
import asyncio
import logging

import websockets
from typing import Optional
from typing import Callable, Awaitable


from google.protobuf.json_format import ParseDict
import centrifuge.protocol.client_pb2 as protocol


logger = logging.getLogger('centrifuge')


class ConnectionEventHandler:

    async def on_connecting(self, **kwargs):
        """Called when connecting."""
        pass

    async def on_connected(self, **kwargs):
        """Called when connected."""
        pass

    async def on_disconnected(self, **kwargs):
        """Called when disconnected."""
        pass

    async def on_error(self, **kwargs):
        """Called when there's an error."""
        pass


class SubscriptionEventHandler:

    async def on_subscribing(self, **kwargs):
        """Called when subscribing."""
        pass

    async def on_subscribed(self, **kwargs):
        """Called when subscribed."""
        pass

    async def on_unsubscribed(self, **kwargs):
        """Called when unsubscribed."""
        pass

    async def on_publication(self, **kwargs):
        """Called when there's a publication."""
        pass

    async def on_join(self, **kwargs):
        """Called when joining."""
        pass

    async def on_leave(self, **kwargs):
        """Called when leaving."""
        pass

    async def on_error(self, **kwargs):
        """Called when there's a subscription error."""
        pass


class CentrifugeException(Exception):
    """
    CentrifugeException is a base exception for all other exceptions
    in this library.
    """
    pass


class ConnectionClosed(CentrifugeException):
    """
    ConnectionClosed raised when underlying websocket connection closed.
    """
    pass


class Timeout(CentrifugeException):
    """
    Timeout raised every time operation times out.
    """
    pass


class SubscriptionError(CentrifugeException):
    """
    SubscriptionError raised when an error subscribing on channel occurred.
    """
    pass


class CallError(CentrifugeException):
    """
    Call raised when an error returned from server as result of presence/history/publish call.
    """
    pass


class DuplicateSubscriptionError(CentrifugeException):
    pass


STATUS_CONNECTED = 'connected'
STATUS_CONNECTING = 'connecting'
STATUS_DISCONNECTED = 'disconnected'


class Client:
    """
    Client is a websocket client to Centrifuge/Centrifugo server.
    """
    factor = 2
    base_delay = 1
    max_delay = 60
    jitter = 0.5

    def __init__(
            self,
            address,
            events: Optional[ConnectionEventHandler] = None,
            token: str = '',
            get_token: Optional[Callable[..., Awaitable[None]]] = None,
            **kwargs):

        self.address = address
        if events is None:
            self.events = ConnectionEventHandler()
        else:
            self.events = events

        self.status = STATUS_DISCONNECTED
        self.client_id = None
        self._conn = None
        self._subs = {}
        self._messages = asyncio.Queue()
        self._delay = 1
        self._reconnect = kwargs.get("reconnect", True)
        self._ping = kwargs.get("ping", True)
        self._ping_timeout = kwargs.get("ping_timeout", 25)
        self._pong_wait_timeout = kwargs.get("pong_wait_timeout", 5)
        self._ping_timer = None
        self._future = None
        self._token = token
        self._get_token = get_token
        self._loop = kwargs.get("loop", asyncio.get_event_loop())
        self._futures = {}

    def subscriptions(self):
        return self._subs.copy()

    def new_subscription(self, channel: str, handler: Optional[SubscriptionEventHandler] = None):
        if self.get_subscription(channel):
            raise DuplicateSubscriptionError('subscription to channel "' + channel + '" is already registered')
        sub = Subscription(self, channel, handler=handler)
        self._subs[channel] = sub
        return sub

    def get_subscription(self, channel):
        return self._subs.get(channel)

    def remove_subscription(self, sub):
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

    def _exponential_backoff(self, delay):
        delay = min(delay * self.factor, self.max_delay)
        return delay + random.randint(0, int(delay*self.jitter))

    async def _schedule_reconnect(self):
        if self.status == STATUS_CONNECTED:
            return

        if self._conn and self._conn.open:
            return

        if not self._reconnect:
            logger.debug("centrifuge: won't reconnect")
            return

        logger.debug("centrifuge: start reconnecting")

        self.status = STATUS_CONNECTING

        self._delay = self._exponential_backoff(self._delay)
        await asyncio.sleep(self._delay)
        success = await self._create_connection()
        if success:
            success = await self._subscribe(self._subs.keys())

        if not success:
            asyncio.ensure_future(self._schedule_reconnect())

    @staticmethod
    def _get_message(method, params, uid=None):
        message = {
            'uid': uid or uuid.uuid4().hex,
            'method': method,
            'params': params
        }
        return message

    async def _create_connection(self):
        try:
            self._conn = await websockets.connect(self.address)
        except OSError:
            return False

        self._delay = self.base_delay
        command_dict = {'id': 1, 'connect': {}}
        # command = ParseDict(command_dict, protocol.Command())
        try:
            await self._conn.send(json.dumps(command_dict))
        except websockets.ConnectionClosed:
            return False
        asyncio.ensure_future(self._listen())
        asyncio.ensure_future(self._process_messages())

        self._future = asyncio.Future()
        success = await self._future
        if success:
            if self._ping:
                self._ping_timer = self._loop.call_later(
                    self._ping_timeout, lambda: asyncio.ensure_future(self._send_ping(), loop=self._loop)
                )
            handler = self._handlers.get("connected")
            if handler:
                await handler(**{"client": self.client_id})

        return True

    async def _send_ping(self):
        if self.status != STATUS_CONNECTED:
            return

        uid = uuid.uuid4().hex
        message = self._get_message("ping", {}, uid=uid)

        try:
            await self._conn.send(json.dumps(message))
        except websockets.ConnectionClosed:
            return

        future = self._register_future(uid, self._pong_wait_timeout)
        try:
            await future
        except Timeout:
            await self._disconnect("no ping", True)

    async def connect(self):
        self.status = STATUS_CONNECTING
        success = await self._create_connection()
        if not success:
            asyncio.ensure_future(self._schedule_reconnect())

    async def disconnect(self):
        await self._disconnect('clean disconnect', False)

    async def subscribe(self, channel, **kwargs):
        sub = Subscription(self, channel, **kwargs)
        self._subs[channel] = sub
        asyncio.ensure_future(self._subscribe([channel]))
        return sub

    async def _subscribe(self, channels):
        if not channels:
            return True

        messages = []

        private_channels = []
        for channel in channels:
            if channel.startswith(self.private_channel_prefix):
                private_channels.append(channel)

        private_data = {}
        handler = self._handlers.get("private_sub")
        if private_channels and handler:
            data = await handler(**{"client": self.client_id, "channels": private_channels})
            if isinstance(data, dict):
                private_data = data

        for channel in channels:
            params = {'channel': channel}
            if channel in private_data:
                params.update({
                    "client": self.client_id,
                    "sign": private_data[channel].sign,
                    "info": private_data[channel].info
                })
            if channel in self._subs:
                params.update({
                    "recover": True,
                    "last": self._subs[channel].last_message_id
                })
            message = self._get_message("subscribe", params)
            messages.append(message)

        if not self._conn:
            return False

        try:
            await self._conn.send(json.dumps(messages))
        except websockets.ConnectionClosed:
            return False

        return True

    async def _resubscribe(self, sub):
        self._subs[sub.channel] = sub
        asyncio.ensure_future(self._subscribe([sub.channel]))
        return sub

    async def _unsubscribe(self, sub):

        if sub.channel in self._subs:
            del self._subs[sub.channel]

        message = self._get_message("unsubscribe", {'channel': sub.channel})

        unsubscribe_handler = sub.handlers.get("unsubscribe")
        if unsubscribe_handler:
            await unsubscribe_handler(**{"channel": sub.channel})

        try:
            await self._conn.send(json.dumps(message))
        except websockets.ConnectionClosed:
            pass

    def _register_future(self, uid, timeout):
        future = asyncio.Future()
        self._futures[uid] = future

        if timeout:
            def cb():
                if not future.done():
                    future.set_exception(Timeout)
                    del self._futures[uid]
            self._loop.call_later(timeout, cb)

        return future

    def _future_error(self, uid, error):
        future = self._futures.get(uid)
        if not future:
            return
        future.set_exception(CallError(error))
        del self._futures[uid]

    def _future_success(self, uid, result):
        future = self._futures.get(uid)
        if not future:
            return
        future.set_result(result)
        del self._futures[uid]

    async def _history(self, sub, timeout=None):
        if sub.channel not in self._subs:
            raise CallError("subscription not in subscribed state")

        await sub._future

        uid = uuid.uuid4().hex
        message = self._get_message("history", {'channel': sub.channel}, uid=uid)

        try:
            await self._conn.send(json.dumps(message))
        except websockets.ConnectionClosed:
            raise ConnectionClosed

        future = self._register_future(uid, timeout or 5)
        result = await future
        return result

    async def _presence(self, sub, timeout=None):
        if sub.channel not in self._subs:
            raise CallError("subscription not in subscribed state")

        await sub._future

        uid = uuid.uuid4().hex
        message = self._get_message("presence", {'channel': sub.channel}, uid=uid)

        try:
            await self._conn.send(json.dumps(message))
        except websockets.ConnectionClosed:
            raise ConnectionClosed

        future = self._register_future(uid, timeout or 5)
        result = await future
        return result

    async def _publish(self, sub, data):
        if sub.channel not in self._subs:
            raise CallError("subscription not in subscribed state")

        await sub._future

        uid = uuid.uuid4().hex
        message = self._get_message("publish", {'channel': sub.channel, 'data': data}, uid=uid)

        try:
            await self._conn.send(json.dumps(message))
        except websockets.ConnectionClosed:
            raise ConnectionClosed

        future = self._register_future(uid, 0)
        result = await future
        return result

    async def _disconnect(self, reason, reconnect):
        if self._ping_timer:
            self._ping_timer.cancel()
            self._ping_timer = None

        if not reconnect:
            self._reconnect = False

        if self.status == STATUS_DISCONNECTED:
            return

        self.status = STATUS_DISCONNECTED
        self.client_id = None

        if self._conn and self._conn.state != 3:
            await self._close()

        for ch, sub in self._subs.items():
            sub._future = asyncio.Future()
            unsubscribe_handler = sub.handlers.get("unsubscribe")
            if unsubscribe_handler:
                await unsubscribe_handler(**{"channel": sub.channel})

        handler = self._handlers.get("disconnected")
        if handler:
            await handler(**{"reason": reason, "reconnect": reconnect})

        if reconnect:
            asyncio.ensure_future(self._schedule_reconnect())

    async def _process_connect(self, response):
        body = response.get("body")
        self.client_id = body.get("client")
        if body.get("error"):
            if self._future:
                self._future.set_result(False)
            await self._close()
            handler = self._handlers.get("error")
            if handler:
                await handler()
        else:
            self.status = STATUS_CONNECTED
            if self._future:
                self._future.set_result(True)

    async def _process_subscribe(self, response):
        body = response.get("body", {})
        channel = body.get("channel")

        sub = self._subs.get(channel)
        if not sub:
            return

        error = response.get("error")
        if not error:
            sub._future.set_result(True)
            subscribe_handler = sub.handlers.get("subscribe")
            if subscribe_handler:
                await subscribe_handler(**{"channel": channel})
            msg_handler = sub.handlers.get("message")
            messages = body.get("messages", [])
            if msg_handler and messages:
                for message in messages:
                    sub.last_message_id = message.get("uid")
                    await msg_handler(**message)
        else:
            sub._future.set_exception(SubscriptionError(error))
            error_handler = sub.handlers.get("error")
            if error_handler:
                kw = {"channel": channel, "error": error, "advice": response.get("advice", "")}
                await error_handler(**kw)

    async def _process_message(self, response):
        body = response.get("body")
        sub = self._subs.get(body.get("channel"))
        if not sub:
            return
        handler = sub.handlers.get("message")
        if handler:
            sub.last_message_id = body.get("uid")
            await handler(**body)

    async def _process_join(self, response):
        body = response.get("body")
        sub = self._subs.get(body.get("channel"))
        if not sub:
            return
        handler = sub.handlers.get("join")
        if handler:
            await handler(**body)

    async def _process_leave(self, response):
        body = response.get("body")
        sub = self._subs.get(body.get("channel"))
        if not sub:
            return
        handler = sub.handlers.get("leave")
        if handler:
            await handler(**body)

    async def _process_publish(self, response):
        uid = response.get("uid")
        if not uid:
            return

        error = response.get("error")
        if error:
            self._future_error(uid, error)
        else:
            self._future_success(uid, response.get("body", {}).get("status", False))

    async def _process_presence(self, response):
        uid = response.get("uid")
        if not uid:
            return

        error = response.get("error")
        if error:
            self._future_error(uid, error)
        else:
            self._future_success(uid, response.get("body", {}).get("data", {}))

    async def _process_history(self, response):
        uid = response.get("uid")
        if not uid:
            return

        error = response.get("error")
        if error:
            self._future_error(uid, error)
        else:
            self._future_success(uid, response.get("body", {}).get("data", []))

    async def _process_ping(self, response):
        uid = response.get("uid")
        if not uid:
            return

        error = response.get("error")
        if error:
            self._future_error(uid, error)
        else:
            self._future_success(uid, response.get("body", {}).get("data", ""))

    async def _process_disconnect(self, response):
        logger.debug("centrifuge: disconnect received")
        body = response.get("body", {})
        reconnect = body.get("reconnect")
        reason = body.get("reason", "")
        await self._disconnect(reason, reconnect)

    async def _process_response(self, response):
        # Restart ping timer every time we received something from connection.
        # This allows us to reduce amount of pings sent around in busy apps.
        if self._ping_timer:
            self._ping_timer.cancel()
            self._ping_timer = self._loop.call_later(
                self._ping_timeout, lambda: asyncio.ensure_future(self._send_ping(), loop=self._loop)
            )

        method = response.get("method")
        if method:
            cb = None
            if method == 'connect':
                cb = self._process_connect
            elif method == 'subscribe':
                cb = self._process_subscribe
            elif method == 'message':
                cb = self._process_message
            elif method == 'join':
                cb = self._process_join
            elif method == 'leave':
                cb = self._process_leave
            elif method == 'disconnect':
                cb = self._process_disconnect
            elif method == 'publish':
                cb = self._process_publish
            elif method == 'presence':
                cb = self._process_presence
            elif method == 'history':
                cb = self._process_history
            elif method == 'ping':
                cb = self._process_ping
            if cb:
                await cb(response)
            else:
                logger.debug(
                    "centrifuge: received message with unknown method %s",
                    method
                )

    async def _parse_response(self, message):
        try:
            response = json.loads(message)
            if isinstance(response, dict):
                await self._process_response(response)
            if isinstance(response, list):
                for obj_response in response:
                    await self._process_response(obj_response)
        except json.JSONDecodeError as err:
            logger.error("centrifuge: %s", err)

    async def _process_messages(self):
        logger.debug("centrifuge: start message processing routine")
        while True:
            if self._messages:
                message = await self._messages.get()
                await self._parse_response(message)

    async def _listen(self):
        logger.debug("centrifuge: start message listening routine")
        while self._conn.open:
            try:
                result = await self._conn.recv()
                if result:
                    logger.debug("centrifuge: data received, {}".format(result))
                    await self._messages.put(result)
            except websockets.ConnectionClosed:
                break

        logger.debug("centrifuge: stop listening")

        reason = ""
        reconnect = True
        if self._conn.close_reason:
            try:
                data = json.loads(self._conn.close_reason)
            except ValueError:
                pass
            else:
                reconnect = data.get("reconnect", True)
                reason = data.get("reason", "")

        await self._disconnect(reason, reconnect)


class Subscription:
    """
    Subscription describes client subscription to Centrifugo channel.
    """
    def __init__(self, client, channel, handler: Optional[SubscriptionEventHandler] = None, **kwargs):
        self._future = asyncio.Future()
        self._subscribed = False
        self.client = client
        self.channel = channel
        self.last_message_id = None
        self.handlers = {
            'message': kwargs.get('on_message'),
            'subscribe': kwargs.get('on_subscribe'),
            'unsubscribe': kwargs.get('on_unsubscribe'),
            'join': kwargs.get('on_join'),
            'leave': kwargs.get('on_leave'),
            'error': kwargs.get('on_error')
        }

    async def unsubscribe(self):
        await self.client._unsubscribe(self)

    async def subscribe(self):
       await self.client._resubscribe(self)

    async def history(self, timeout=None):
        res = await self.client._history(self, timeout=timeout)
        return res

    async def presence(self, timeout=None):
        res = await self.client._presence(self, timeout=timeout)
        return res

    async def publish(self, data):
        success = await self.client._publish(self, data)
        return success
