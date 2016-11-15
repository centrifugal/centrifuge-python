import asyncio
from datetime import datetime
import uuid
import websockets
from websockets.exceptions import ConnectionClosed
import json
from random import randint
import logging


class CentrifugoClient(object):
    Factor = 2
    MaxDelay = 60

    CONNECT = 'connect'
    options = {}
    ws = None
    connect_user_id = None
    timestamp = None
    info = None
    toke = None
    _loop = None
    client_id = None
    subscribes = dict()
    default_methods = dict()
    connection_address = None
    reconnect_ = False
    messages = asyncio.Queue()
    base_delay = 2
    delay = base_delay

    def __init__(self, loop, reconnect=False):
        self._loop = loop
        self.reconnect_ = reconnect
        self.default_methods = {
            "connect": self._is_connect_success,
            "subscribe": self._is_subscribe_success,
            "message": self._message_method_process,
            "disconnect": self._disconnect_from_centrifugo
        }

    @asyncio.coroutine
    def close(self):
        self._close()

    def _close(self):
        self.ws.close()

    def _exponential_backoff(self, delay):
        delay = min(delay * self.Factor, self.MaxDelay)
        return delay + randint(0, 3)

    @asyncio.coroutine
    def reconnect(self):
        if self.ws and self.ws.open:
            self._close()
        if self.reconnect_:
            self.delay = self._exponential_backoff(self.delay)
            yield from asyncio.sleep(self.delay)
            yield from self._create_connection()
            for chanel in self.subscribes.keys():
                yield from self.subscribe(chanel, self.subscribes[chanel])

    def _get_message(self, params, method=CONNECT):
        connect_id = uuid.uuid4().hex
        message = {
            'uid': connect_id,
            'method': method,
            'params': params
        }
        return connect_id, message

    def _is_connect_success(self, response):
        body = response.get("body")
        self.client_id = body.get("client")
        if body.get("error"):
            self.close()

    def _is_subscribe_success(self, response):
        error = response.get("error")
        method = response.get("method")
        if error and method:
            self.subscribes.pop(method)

    @asyncio.coroutine
    def _create_connection(self):
        try:
            self.ws = yield from websockets.connect(self.connection_address)
        except OSError:
            asyncio.ensure_future(self.reconnect())
            return
        self.delay = self.base_delay
        params = {
            'user': self.connect_user_id,
            'timestamp': self.timestamp,
            'info': self.info,
            'token': self.token
        }
        uid, message = self._get_message(params=params)
        yield from self.ws.send(json.dumps(message))
        future = asyncio.Future()
        asyncio.ensure_future(self._listening(future))
        future = asyncio.Future()
        asyncio.ensure_future(self._process_messages(future))

    @asyncio.coroutine
    def centrifugo_conect(self, connection_address, options, user_id, timestamp, info, token):
        self.connection_address = connection_address
        self.options = options
        self.connect_user_id = str(user_id)
        self.timestamp = str(timestamp)
        self.info = str(info)
        self.token = str(token)
        yield from self._create_connection()


    @asyncio.coroutine
    def subscribe(self, chanel, function):
        self.subscribes.update({chanel: function})
        uid, message = self._get_message(method="subscribe", params={'channel': chanel})
        while not self.ws:
            yield from asyncio.sleep(1)
        try:
            yield from self.ws.send(json.dumps(message))
        except ConnectionClosed:
            self._reconnect()

    def _disconnect_from_centrifugo(self, message):
        body = message.get("body")
        reconnect = body.get("reconnect")
        if not reconnect:
            yield from self.ws.close()
            self.reconnect_ = False

    def _message_method_process(self, message):
        body = message.get("body")
        self.subscribes.get(body.get("channel"))(body)

    @asyncio.coroutine
    def _response_item_process(self, response_item):
        method = response_item.get("method")
        if method:
            cb = self.default_methods.get(method)
            if cb:
                cb(response_item)

    def get_subscribes(self):
        return self.subscribes.keys()

    @asyncio.coroutine
    def _parse_response(self, response):
        try:
            obj_response = json.loads(response)
            if isinstance(obj_response, dict):
                asyncio.ensure_future(self._response_item_process(obj_response))
            if isinstance(obj_response, list):
                for item in obj_response:
                    asyncio.ensure_future(self._response_item_process(item))
        except json.JSONDecodeError:
            pass

    @asyncio.coroutine
    def _check_message(self):
        if self.messages:
            last_message = yield from self.messages.get()
            asyncio.ensure_future(self._parse_response(last_message))

    @asyncio.coroutine
    def _process_messages(self, future):
        while True:
            yield from self._check_message()

    @asyncio.coroutine
    def _listening(self, future):
        while self.ws.open:
            try:
                result = yield from self.ws.recv()
                if result:
                    logging.debug("result_message {}, {}".format(datetime.now(), result))
                    yield from self.messages.put(result)
            except ConnectionClosed:
                self.reconnect()
