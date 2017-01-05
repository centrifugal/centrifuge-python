# Centrifuge websocket client for Python

Usage example:

```python
import asyncio
from centrifuge import Client, Credentials


def run(loop):
    credentials = Credentials("", "", "", "")
    client = Client(loop)

    yield from client.connect("ws://localhost:8000/connection/websocket", credentials)

    @asyncio.coroutine
    def message_handler(msg):
        print(msg)

    sid = yield from client.subscribe("public:chat", message_handler)

    while True:
        yield from asyncio.sleep(1)


if __name__ == '__main__':
    loop = asyncio.get_event_loop()
    loop.run_until_complete(run(loop))
    loop.close()
```