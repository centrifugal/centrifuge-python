import uuid
import json
import asyncio
import logging
from random import randint
from datetime import datetime

import websockets
from websockets.exceptions import ConnectionClosed


logger = logging.getLogger('centrifuge')


class CentrifugeException(Exception):
    pass


class SubscriptionError(CentrifugeException):
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


class Client:
    factor = 2
    base_delay = 1
    max_delay = 60
    jitter = 0.5

    def __init__(self, loop, reconnect=True):
        self._loop = loop

        self.address = None
        self.options = {}

        self.conn = None
        self.client_id = None

        self.credentials = None

        self.subs = dict()
        self.messages = asyncio.Queue()
        self.delay = 1

        self._reconnect = reconnect

    @asyncio.coroutine
    def close(self):
        self._close()

    def _close(self):
        self.conn.close()

    def _exponential_backoff(self, delay):
        delay = min(delay * self.factor, self.max_delay)
        return delay + randint(0, int(delay*self.jitter))

    @asyncio.coroutine
    def reconnect(self):
        logger.debug("centrifuge: start reconnecting")
        if self.conn and self.conn.open:
            self._close()
        if self._reconnect:
            self.delay = self._exponential_backoff(self.delay)
            yield from asyncio.sleep(self.delay)
            yield from self._create_connection()
            for channel in self.subs.keys():
                # TODO: should send this in batch.
                yield from self._subscribe(channel)

    @staticmethod
    def _get_message(method, params):
        connect_id = uuid.uuid4().hex
        message = {
            'uid': connect_id,
            'method': method,
            'params': params
        }
        return connect_id, message

    @asyncio.coroutine
    def _process_connect(self, response):
        body = response.get("body")
        self.client_id = body.get("client")
        if body.get("error"):
            self.close()

    @asyncio.coroutine
    def _process_subscribe(self, response):
        error = response.get("error")
        if error:
            channel = response.get("body", {}).get("channel")
            if channel:
                self.subs.pop(channel)

    @asyncio.coroutine
    def _create_connection(self):
        try:
            self.conn = yield from websockets.connect(self.address)
        except OSError:
            asyncio.ensure_future(self.reconnect())
            return
        self.delay = self.base_delay
        params = {
            'user': self.credentials.user,
            'timestamp': self.credentials.timestamp,
            'info': self.credentials.info,
            'token': self.credentials.token
        }
        uid, message = self._get_message('connect', params)
        yield from self.conn.send(json.dumps(message))
        asyncio.ensure_future(self._listen())
        asyncio.ensure_future(self._process_messages())

    @asyncio.coroutine
    def connect(self, address, credentials, **options):
        self.address = address
        self.credentials = credentials
        self.options = options
        yield from self._create_connection()

    @asyncio.coroutine
    def subscribe(self, channel, msg_handler, join_handler=None, leave_handler=None):
        self.subs.update({channel: {
            'message': msg_handler,
            'join': join_handler,
            'leave': leave_handler
        }})
        yield from self._subscribe(channel)

    @asyncio.coroutine
    def _subscribe(self, channel):
        uid, message = self._get_message("subscribe", {'channel': channel})
        while not self.conn:
            # TODO: make this through future
            yield from asyncio.sleep(1)
        try:
            yield from self.conn.send(json.dumps(message))
        except ConnectionClosed:
            self.reconnect()

    @asyncio.coroutine
    def _process_disconnect(self, response):
        body = response.get("body")
        reconnect = body.get("reconnect")
        if not reconnect:
            self._reconnect = False
        yield from self.conn.close()

    @asyncio.coroutine
    def _process_message(self, response):
        body = response.get("body")
        handlers = self.subs.get(body.get("channel"))
        if handlers:
            yield from handlers["message"](body)

    @asyncio.coroutine
    def _process_join(self, response):
        body = response.get("body")
        handlers = self.subs.get(body.get("channel"))
        if handlers:
            handler = handlers.get("join")
            if handler:
                yield from handler(body)

    @asyncio.coroutine
    def _process_leave(self, response):
        body = response.get("body")
        handlers = self.subs.get(body.get("channel"))
        if handlers:
            handler = handlers.get("leave")
            if handler:
                yield from handler(body)

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
        except json.JSONDecodeError:
            pass

    @asyncio.coroutine
    def _process_messages(self):
        while True:
            if self.messages:
                message = yield from self.messages.get()
                yield from self._parse_response(message)

    @asyncio.coroutine
    def _listen(self):
        while self.conn.open:
            try:
                result = yield from self.conn.recv()
                if result:
                    logger.debug("centrifuge: data received {}, {}".format(datetime.now(), result))
                    yield from self.messages.put(result)
            except ConnectionClosed:
                pass
        logger.debug("centrifuge: stop listening")
        asyncio.ensure_future(self.reconnect())
