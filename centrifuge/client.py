import uuid
import json
import random
import asyncio
import logging

import websockets


logger = logging.getLogger('centrifuge')


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


class Credentials:
    """
    Credentials is a wrapper over connection parameters client must
    supply in first connect message right after establishing websocket
    connection with Centrifugo.
    """
    def __init__(self, user, timestamp, info, token):
        self.user = str(user)
        self.timestamp = str(timestamp)
        self.info = str(info)
        self.token = str(token)


class Subscription:
    """
    Subscription describes client subscription to Centrifugo channel.
    """
    def __init__(self, client, channel, **kwargs):
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

    @asyncio.coroutine
    def unsubscribe(self):
        yield from self.client._unsubscribe(self)

    @asyncio.coroutine
    def subscribe(self):
        yield from self.client._resubscribe(self)

    @asyncio.coroutine
    def history(self, timeout=None):
        res = yield from self.client._history(self, timeout=timeout)
        return res

    @asyncio.coroutine
    def presence(self, timeout=None):
        res = yield from self.client._presence(self, timeout=timeout)
        return res

    @asyncio.coroutine
    def publish(self, data):
        success = yield from self.client._publish(self, data)
        return success


STATUS_CONNECTED = 'connected'
STATUS_CONNECTING = 'connecting'
STATUS_DISCONNECTED = 'disconnected'


class Client:
    """
    Client is a Centrifugo server websocket client.
    """
    factor = 2
    base_delay = 1
    max_delay = 60
    jitter = 0.5

    def __init__(self, address, credentials, **kwargs):
        self.address = address
        self.credentials = credentials
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
        self._handlers = {
            "connect": kwargs.get("on_connect"),
            "disconnect": kwargs.get("on_disconnect"),
            "error": kwargs.get("on_error")
        }
        self._loop = kwargs.get("loop", asyncio.get_event_loop())
        self._futures = {}

    def channels(self):
        return self._subs.keys()

    @asyncio.coroutine
    def close(self):
        yield from self._close()

    @asyncio.coroutine
    def _close(self):
        try:
            yield from self._conn.close()
        except websockets.ConnectionClosed:
            pass

    def _exponential_backoff(self, delay):
        delay = min(delay * self.factor, self.max_delay)
        return delay + random.randint(0, int(delay*self.jitter))

    @asyncio.coroutine
    def reconnect(self):
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
        yield from asyncio.sleep(self._delay)
        success = yield from self._create_connection()
        if success:
            success = yield from self._subscribe(self._subs.keys())

        if not success:
            asyncio.ensure_future(self.reconnect())

    @staticmethod
    def _get_message(method, params, uid=None):
        message = {
            'uid': uid or uuid.uuid4().hex,
            'method': method,
            'params': params
        }
        return message

    @asyncio.coroutine
    def _create_connection(self):
        try:
            self._conn = yield from websockets.connect(self.address)
        except OSError:
            return False
        self._delay = self.base_delay
        params = {
            'user': self.credentials.user,
            'timestamp': self.credentials.timestamp,
            'info': self.credentials.info,
            'token': self.credentials.token
        }
        message = self._get_message('connect', params)
        try:
            yield from self._conn.send(json.dumps(message))
        except websockets.ConnectionClosed:
            return False
        asyncio.ensure_future(self._listen())
        asyncio.ensure_future(self._process_messages())
        return True

    def _send_ping(self):
        if self.status != STATUS_CONNECTED:
            return
        uid = uuid.uuid4().hex
        message = self._get_message("ping", {}, uid=uid)

        try:
            yield from self._conn.send(json.dumps(message))
        except websockets.ConnectionClosed:
            return

        future = self._register_future(uid, self._pong_wait_timeout)
        try:
            yield from future
        except Timeout:
            yield from self._disconnect("no ping", True)

    @asyncio.coroutine
    def connect(self):
        self.status = STATUS_CONNECTING
        success = yield from self._create_connection()
        if not success:
            asyncio.ensure_future(self.reconnect())
        else:
            handler = self._handlers.get("connect")
            if self._ping:
                self._ping_timer = self._loop.call_later(
                    self._ping_timeout, lambda: asyncio.ensure_future(self._send_ping(), loop=self._loop)
                )
            if handler:
                yield from handler()

    @asyncio.coroutine
    def disconnect(self):
        yield from self.close()

    @asyncio.coroutine
    def subscribe(self, channel, **kwargs):
        sub = Subscription(self, channel, **kwargs)
        self._subs[channel] = sub
        asyncio.ensure_future(self._subscribe([channel]))
        return sub

    @asyncio.coroutine
    def _subscribe(self, channels):
        if not channels:
            return True

        messages = []

        for channel in channels:
            params = {'channel': channel}
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
            yield from self._conn.send(json.dumps(messages))
        except websockets.ConnectionClosed:
            return False

        return True

    @asyncio.coroutine
    def _resubscribe(self, sub):
        self._subs[sub.channel] = sub
        asyncio.ensure_future(self._subscribe([sub.channel]))
        return sub

    @asyncio.coroutine
    def _unsubscribe(self, sub):

        if sub.channel in self._subs:
            del self._subs[sub.channel]

        message = self._get_message("unsubscribe", {'channel': sub.channel})

        unsubscribe_handler = sub.handlers.get("unsubscribe")
        if unsubscribe_handler:
            yield from unsubscribe_handler(**{"channel": sub.channel})

        try:
            yield from self._conn.send(json.dumps(message))
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

    @asyncio.coroutine
    def _history(self, sub, timeout=None):
        if sub.channel not in self._subs:
            raise CallError("subscription not in subscribed state")

        yield from sub._future

        uid = uuid.uuid4().hex
        message = self._get_message("history", {'channel': sub.channel}, uid=uid)

        try:
            yield from self._conn.send(json.dumps(message))
        except websockets.ConnectionClosed:
            raise ConnectionClosed

        future = self._register_future(uid, timeout or 5)
        result = yield from future
        return result

    @asyncio.coroutine
    def _presence(self, sub, timeout=None):
        if sub.channel not in self._subs:
            raise CallError("subscription not in subscribed state")

        yield from sub._future

        uid = uuid.uuid4().hex
        message = self._get_message("presence", {'channel': sub.channel}, uid=uid)

        try:
            yield from self._conn.send(json.dumps(message))
        except websockets.ConnectionClosed:
            raise ConnectionClosed

        future = self._register_future(uid, timeout or 5)
        result = yield from future
        return result

    @asyncio.coroutine
    def _publish(self, sub, data):
        if sub.channel not in self._subs:
            raise CallError("subscription not in subscribed state")

        yield from sub._future

        uid = uuid.uuid4().hex
        message = self._get_message("publish", {'channel': sub.channel, 'data': data}, uid=uid)

        try:
            yield from self._conn.send(json.dumps(message))
        except websockets.ConnectionClosed:
            raise ConnectionClosed

        future = self._register_future(uid, 0)
        result = yield from future
        return result

    @asyncio.coroutine
    def _disconnect(self, reason, reconnect):
        if self._ping_timer:
            self._ping_timer.cancel()
            self._ping_timer = None
        if not reconnect:
            self._reconnect = False
        if self.status == STATUS_DISCONNECTED:
            return
        self.status = STATUS_DISCONNECTED
        self.client_id = None
        yield from self.close()

        for ch, sub in self._subs.items():
            sub._future = asyncio.Future()
            unsubscribe_handler = sub.handlers.get("unsubscribe")
            if unsubscribe_handler:
                yield from unsubscribe_handler(**{"channel": sub.channel})

        handler = self._handlers.get("disconnect")
        if handler:
            yield from handler(**{"reason": reason, "reconnect": reconnect})
        asyncio.ensure_future(self.reconnect())

    @asyncio.coroutine
    def _process_connect(self, response):
        body = response.get("body")
        self.client_id = body.get("client")
        if body.get("error"):
            yield from self.close()
            handler = self._handlers.get("error")
            if handler:
                yield from handler()
        else:
            self.status = STATUS_CONNECTED

    @asyncio.coroutine
    def _process_subscribe(self, response):
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
                yield from subscribe_handler(**{"channel": channel})
            msg_handler = sub.handlers.get("message")
            messages = body.get("messages", [])
            if msg_handler and messages:
                for message in messages:
                    sub.last_message_id = message.get("uid")
                    yield from msg_handler(**message)
        else:
            sub._future.set_exception(SubscriptionError(error))
            error_handler = sub.handlers.get("error")
            if error_handler:
                kw = {"channel": channel, "error": error, "advice": response.get("advice", "")}
                yield from error_handler(**kw)

    @asyncio.coroutine
    def _process_message(self, response):
        body = response.get("body")
        sub = self._subs.get(body.get("channel"))
        if not sub:
            return
        handler = sub.handlers.get("message")
        if handler:
            sub.last_message_id = body.get("uid")
            yield from handler(**body)

    @asyncio.coroutine
    def _process_join(self, response):
        body = response.get("body")
        sub = self._subs.get(body.get("channel"))
        if not sub:
            return
        handler = sub.handlers.get("join")
        if handler:
            yield from handler(**body)

    @asyncio.coroutine
    def _process_leave(self, response):
        body = response.get("body")
        sub = self._subs.get(body.get("channel"))
        if not sub:
            return
        handler = sub.handlers.get("leave")
        if handler:
            yield from handler(**body)

    @asyncio.coroutine
    def _process_publish(self, response):
        uid = response.get("uid")
        if not uid:
            return

        error = response.get("error")
        if error:
            self._future_error(uid, error)
        else:
            self._future_success(uid, response.get("body", {}).get("status", False))

    @asyncio.coroutine
    def _process_presence(self, response):
        uid = response.get("uid")
        if not uid:
            return

        error = response.get("error")
        if error:
            self._future_error(uid, error)
        else:
            self._future_success(uid, response.get("body", {}).get("data", {}))

    @asyncio.coroutine
    def _process_history(self, response):
        uid = response.get("uid")
        if not uid:
            return

        error = response.get("error")
        if error:
            self._future_error(uid, error)
        else:
            self._future_success(uid, response.get("body", {}).get("data", []))

    @asyncio.coroutine
    def _process_ping(self, response):
        uid = response.get("uid")
        if not uid:
            return

        error = response.get("error")
        if error:
            self._future_error(uid, error)
        else:
            self._future_success(uid, response.get("body", {}).get("data", ""))

    @asyncio.coroutine
    def _process_disconnect(self, response):
        logger.debug("centrifuge: disconnect received")
        body = response.get("body", {})
        reconnect = body.get("reconnect")
        reason = body.get("reason", "")
        yield from self._disconnect(reason, reconnect)

    @asyncio.coroutine
    def _process_response(self, response):

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
                yield from cb(response)
            else:
                logger.debug(
                    "centrifuge: received message with unknown method %s",
                    method
                )

    @asyncio.coroutine
    def _parse_response(self, message):
        try:
            response = json.loads(message)
            if isinstance(response, dict):
                yield from self._process_response(response)
            if isinstance(response, list):
                for obj_response in response:
                    yield from self._process_response(obj_response)
        except json.JSONDecodeError as err:
            logger.error("centrifuge: %s", err)

    @asyncio.coroutine
    def _process_messages(self):
        logger.debug("centrifuge: start message processing routine")
        while True:
            if self._messages:
                message = yield from self._messages.get()
                yield from self._parse_response(message)

    @asyncio.coroutine
    def _listen(self):
        logger.debug("centrifuge: start message listening routine")
        while self._conn.open:
            try:
                result = yield from self._conn.recv()
                if result:
                    logger.debug("centrifuge: data received, {}".format(result))
                    yield from self._messages.put(result)
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

        yield from self._disconnect(reason, reconnect)
