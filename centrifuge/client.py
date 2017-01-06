import uuid
import json
import asyncio
import logging
from random import randint

import websockets
from websockets.exceptions import ConnectionClosed


logger = logging.getLogger('centrifuge')


class CentrifugeException(Exception):
    """
    CentrifugeException is a base exception for all other exceptions in this library.
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

    def __init__(self, address, credentials, loop=None, reconnect=True, **kwargs):
        self.address = address
        self.credentials = credentials
        self._loop = loop or asyncio.get_event_loop()
        self._reconnect = reconnect
        self._conn = None
        self._client_id = None
        self._subs = {}
        self._messages = asyncio.Queue()
        self._delay = 1
        self._handlers = {
            "connect": kwargs.get("on_connect"),
            "disconnect": kwargs.get("on_disconnect"),
            "error": kwargs.get("on_error")
        }
        self._status = STATUS_DISCONNECTED

    @asyncio.coroutine
    def close(self):
        yield from self._close()

    @asyncio.coroutine
    def _close(self):
        try:
            yield from self._conn.close()
        except ConnectionClosed:
            pass

    def _exponential_backoff(self, delay):
        delay = min(delay * self.factor, self.max_delay)
        return delay + randint(0, int(delay*self.jitter))

    @asyncio.coroutine
    def reconnect(self):
        if self._status == STATUS_CONNECTED:
            return

        if self._conn and self._conn.open:
            return

        if not self._reconnect:
            logger.debug("centrifuge: won't reconnect")
            return

        logger.debug("centrifuge: start reconnecting")

        self._status = STATUS_CONNECTING

        self._delay = self._exponential_backoff(self._delay)
        yield from asyncio.sleep(self._delay)
        success = yield from self._create_connection()
        if success:
            success = yield from self._subscribe(self._subs.keys())

        if not success:
            asyncio.ensure_future(self.reconnect())

    @staticmethod
    def _get_message(method, params):
        message = {
            'uid': uuid.uuid4().hex,
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
        except ConnectionClosed:
            return False
        asyncio.ensure_future(self._listen())
        asyncio.ensure_future(self._process_messages())
        return True

    @asyncio.coroutine
    def connect(self):
        self._status = STATUS_CONNECTING
        success = yield from self._create_connection()
        if not success:
            asyncio.ensure_future(self.reconnect())
        else:
            handler = self._handlers.get("connect")
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
        except ConnectionClosed:
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

        try:
            yield from self._conn.send(json.dumps(message))
        except ConnectionClosed:
            pass

    @asyncio.coroutine
    def _disconnect(self, reason, reconnect):
        if not reconnect:
            self._reconnect = False
        if self._status == STATUS_DISCONNECTED:
            return
        self._status = STATUS_DISCONNECTED
        yield from self.close()
        handler = self._handlers.get("disconnect")
        if handler:
            yield from handler(**{"reason": reason, "reconnect": reconnect})
        asyncio.ensure_future(self.reconnect())

    @asyncio.coroutine
    def _process_connect(self, response):
        body = response.get("body")
        self._client_id = body.get("client")
        if body.get("error"):
            yield from self.close()
            handler = self._handlers.get("error")
            if handler:
                yield from handler()
        else:
            self._status = STATUS_CONNECTED

    @asyncio.coroutine
    def _process_subscribe(self, response):
        body = response.get("body", {})
        channel = body.get("channel")

        sub = self._subs.get(channel)
        if not sub:
            return

        error = response.get("error")
        if not error:
            msg_handler = sub.handlers.get("message")
            messages = body.get("messages", [])
            if msg_handler and messages:
                for message in messages:
                    sub.last_message_id = message.get("uid")
                    yield from msg_handler(**message)
        else:
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
    def _process_disconnect(self, response):
        logger.debug("centrifuge: disconnect received")
        body = response.get("body", {})
        reconnect = body.get("reconnect")
        reason = body.get("reason", "")
        yield from self._disconnect(reason, reconnect)

    @asyncio.coroutine
    def _process_response(self, response):
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
            if cb:
                yield from cb(response)

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
            except ConnectionClosed:
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
