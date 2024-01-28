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
