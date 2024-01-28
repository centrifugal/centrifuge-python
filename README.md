# centrifuge-python (work in progress)

WORK IN PROGRESS

This is a websocket SDK for [Centrifugo](https://github.com/centrifugal/centrifugo) server on top of Python asyncio library.

## Run tests

To run tests first start Centrifugo server:

```bash
docker run -p 8000:8000 centrifugo/centrifugo:v5 centrifugo --client_insecure --log_level debug
```

And then:

```bash
python -m unittest discover -s tests
```
