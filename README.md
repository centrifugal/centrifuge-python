# centrifuge-python (work in progress)

[![CI](https://github.com/centrifugal/centrifuge-python/actions/workflows/test.yml/badge.svg)](https://github.com/centrifugal/centrifuge-python/actions/workflows/test.yml?query=event%3Apush+branch%3Amaster+workflow%3ATest)
[![pypi](https://img.shields.io/pypi/v/centrifuge-python.svg)](https://pypi.python.org/pypi/centrifuge-python)
[![versions](https://img.shields.io/pypi/pyversions/centrifuge-python.svg)](https://github.com/centrifugal/centrifuge-python)
[![license](https://img.shields.io/github/license/centrifugal/centrifuge-python.svg)](https://github.com/centrifugal/centrifuge-python/blob/master/LICENSE)

This is a WebSocket real-time SDK for [Centrifugo](https://github.com/centrifugal/centrifugo) server (and any [Centrifuge-based](https://github.com/centrifugal/centrifuge) server) on top of Python asyncio library.

> [!TIP]
> If you are looking for Centrifugo [server API](https://centrifugal.dev/docs/server/server_api) client – check out [pycent](https://github.com/centrifugal/pycent) instead.

Before starting to work with this library check out [Centrifugo client SDK API specification](https://centrifugal.dev/docs/transports/client_api) as it contains common information about Centrifugal real-time SDK behavior.

The features implemented by this SDK can be found in [SDK feature matrix](https://centrifugal.dev/docs/transports/client_sdk#sdk-feature-matrix).

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

By default, SDK uses JSON protocol. If you want to use Protobuf protocol instead then pass `use_protobuf=True` option.

When using JSON protocol:

* all payloads (data to publish, connect/subscribe data) you pass to the library are encoded to JSON internally using `json.dumps` before sending to server. So make sure you pass only JSON-serializable data to the library.
* all payloads received from server are decoded to Python objects using `json.loads` internally before passing to your code.

When using Protobuf protocol:

* all payloads you pass to the library must be `bytes` or `None` if optional. If you pass non-`bytes` data – exception will be raised.
* all payloads received from the library will be `bytes` or `None` if not present.
* don't forget that when using Protobuf protocol you can still have JSON payloads - just encode them to `bytes` before passing to the library.

## Callbacks should not block

Event callbacks are called by SDK using `await` internally, the websocket connection read loop is blocked for the time SDK waits for the callback to be executed. This means that if you need to perform long operations in callbacks consider moving the work to a separate coroutine/task to return fast and continue reading data from the websocket.

## Run tests

To run tests, first start Centrifugo server:

```bash
docker run -p 8000:8000 centrifugo/centrifugo:v5 centrifugo --client_insecure --log_level debug
```

And then:

```bash
python -m venv env
. env/bin/activate
make dev
make test
```

## Run example

To run [example](https://github.com/centrifugal/centrifuge-python/blob/master/example.py), first start Centrifugo with config like this:

```json
{
  "token_hmac_secret_key": "secret",
  "namespaces": [
    {
      "name": "example",
      "presence": true,
      "history_size": 300,
      "history_ttl": "300s",
      "join_leave": true,
      "force_push_join_leave": true,
      "allow_publish_for_subscriber": true,
      "allow_presence_for_subscriber": true,
      "allow_history_for_subscriber": true
    }
  ]
}
```

And then:

```bash
python -m venv env
. env/bin/activate
make dev
python example.py
```
