# centrifuge-python

[![CI](https://github.com/centrifugal/centrifuge-python/actions/workflows/test.yml/badge.svg)](https://github.com/centrifugal/centrifuge-python/actions/workflows/test.yml?query=event%3Apush+branch%3Amaster+workflow%3ATest)
[![pypi](https://img.shields.io/pypi/v/centrifuge-python.svg)](https://pypi.python.org/pypi/centrifuge-python)
[![versions](https://img.shields.io/pypi/pyversions/centrifuge-python.svg)](https://github.com/centrifugal/centrifuge-python)
[![license](https://img.shields.io/github/license/centrifugal/centrifuge-python.svg)](https://github.com/centrifugal/centrifuge-python/blob/master/LICENSE)

This is a WebSocket real-time SDK for [Centrifugo](https://github.com/centrifugal/centrifugo) server (and any [Centrifuge-based](https://github.com/centrifugal/centrifuge) server) on top of Python asyncio library.

> [!TIP]
> If you are looking for Centrifugo [server API](https://centrifugal.dev/docs/server/server_api) client – check out [pycent](https://github.com/centrifugal/pycent) instead.

Before starting to work with this library check out Centrifugo [client SDK API specification](https://centrifugal.dev/docs/transports/client_api) as it contains common information about Centrifugal real-time SDK behavior. This SDK supports all major features of Centrifugo client protocol - see [SDK feature matrix](https://centrifugal.dev/docs/transports/client_sdk#sdk-feature-matrix).

## Install

```
pip install centrifuge-python
```

Then in your code:

```
from centrifuge import Client
```

See [example code](https://github.com/centrifugal/centrifuge-python/blob/master/example.py) and [how to run it](#run-example) locally.

## JSON vs Protobuf protocols

By default, SDK uses JSON protocol. If you want to use Protobuf protocol instead then pass `use_protobuf=True` option to `Client` constructor.

When using JSON protocol:

* all payloads (data to publish, connect/subscribe data) you pass to the library are encoded to JSON internally using `json.dumps` before sending to server. So make sure you pass only JSON-serializable data to the library.
* all payloads received from server are decoded to Python objects using `json.loads` internally before passing to your code.

When using Protobuf protocol:

* all payloads you pass to the library must be `bytes` or `None` if optional. If you pass non-`bytes` data – exception will be raised.
* all payloads received from the library will be `bytes` or `None` if not present.
* don't forget that when using Protobuf protocol you can still have JSON payloads - just encode them to `bytes` before passing to the library.

## Custom TLS configuration

When connecting to a `wss://` endpoint the SDK uses the default TLS context of the `ssl` module – i.e. server certificates are verified against the system CA store. To customize TLS – for example to trust a custom CA – pass your own `ssl.SSLContext` as `ssl_context` option:

```python
import ssl

ssl_ctx = ssl.create_default_context(cafile="/path/to/ca.pem")

client = Client(
    "wss://localhost:8000/connection/websocket",
    ssl_context=ssl_ctx,
)
```

The same option allows disabling certificate verification entirely – only do this for local development, never in production:

```python
import ssl

ssl_ctx = ssl.create_default_context()
ssl_ctx.check_hostname = False
ssl_ctx.verify_mode = ssl.CERT_NONE

client = Client(
    "wss://localhost:8000/connection/websocket",
    ssl_context=ssl_ctx,
)
```

## Connecting through a proxy

By default the proxy configuration is taken from the environment (`WS_PROXY`/`WSS_PROXY`, `HTTP_PROXY`/`HTTPS_PROXY`, honoring `NO_PROXY`). To set the proxy explicitly – use `proxy` option of `Client` constructor:

```python
client = Client(
    "ws://localhost:8000/connection/websocket",
    proxy="http://user:pass@proxy-host:3128",
)
```

Pass `proxy=None` to always connect directly, ignoring the environment configuration.

SOCKS proxies (`socks5://...`) are supported too, but require the [python-socks](https://pypi.org/project/python-socks/) package to be installed:

```bash
pip install python-socks
```

Invalid proxy URLs and a missing `python-socks` package are reported as `ValueError` from the `Client` constructor, rather than in the middle of connecting.

A couple of things to keep in mind when going through a proxy:

* with a `wss://` address the proxy only sees the `CONNECT host:port` request – the WebSocket traffic inside the tunnel stays encrypted end to end, and the server certificate is still verified as usual. With a `ws://` address the proxy sees everything, including the connection token.
* credentials in an `http://` proxy URL are sent to the proxy as a base64-encoded `Proxy-Authorization` header over an unencrypted connection. They are never forwarded to the Centrifugo server, but use an `https://` proxy if the proxy connection itself may be observed.

## Callbacks should not block

Event callbacks are called by SDK using `await` internally, the websocket connection read loop is blocked for the time SDK waits for the callback to be executed. This means that if you need to perform long operations in callbacks consider moving the work to a separate coroutine/task to return fast and continue reading data from the websocket.

The fact WebSocket read is blocked for the time we execute callbacks means that you can not call awaitable SDK APIs from callback – because SDK does not have a chance to read the reply. You will get `OperationTimeoutError` exception. The rule is the same - do the work asynchronously, for example use `asyncio.ensure_future`.

## Run example

To run [example](https://github.com/centrifugal/centrifuge-python/blob/master/example.py), first start Centrifugo – the [docker-compose.yml](https://github.com/centrifugal/centrifuge-python/blob/master/docker-compose.yml) of this repo configures everything the example needs (it's the same server the tests use):

```bash
docker compose up
```

And then:

```bash
python -m venv env
. env/bin/activate
make dev
python example.py
```

## Run tests

To run tests locally, start test Centrifugo server:

```
docker compose up
```

Then:

```bash
python -m venv env
. env/bin/activate
make dev
make test
```
