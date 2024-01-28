# centrifuge-python (work in progress)

This is a WebSocket SDK for [Centrifugo](https://github.com/centrifugal/centrifugo) server (and any [Centrifuge-based](https://github.com/centrifugal/centrifuge) server) on top of Python asyncio library.

## Run tests

To run tests first start Centrifugo server:

```bash
docker run -p 8000:8000 centrifugo/centrifugo:v5 centrifugo --client_insecure --log_level debug
```

And then:

```bash
python -m venv env
. env/bin/activate
pip install -r requirements.txt
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
pip install -r requirements.txt
python example.py
```
