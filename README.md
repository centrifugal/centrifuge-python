# centrifuge-python (work in progress)

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

## Run tests

To run tests first start Centrifugo server:

```bash
docker run -p 8000:8000 centrifugo/centrifugo:v5 centrifugo --client_insecure --log_level debug
```

And then:

```bash
python -m venv env
. env/bin/activate
make dev
python -m unittest discover -s tests
```

## Run example

Start Centrifugo with config like this (defines namespace called "example", enables features used in the example):

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
