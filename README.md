# Centrifuge-python (work in progress)

This is a websocket client for [Centrifugo](https://github.com/centrifugal/centrifugo) server on top of Python asyncio library.

Usage example:

```python
import time
import json
import asyncio
from cent import generate_token
from centrifuge import Client, Credentials


def run(loop):

    # Generate credentials.
    # In production this must only be done on backend side and you should
    # never show secret to client!
    user = "3000"
    timestamp = str(int(time.time()))
    info = json.dumps({"first_name": "Python", "last_name": "Client"})
    token = generate_token("secret", user, timestamp, info=info)

    address = "ws://localhost:8000/connection/websocket"
    credentials = Credentials(user, timestamp, info, token)
    client = Client(address, credentials)

    yield from client.connect()

    @asyncio.coroutine
    def message_handler(**kwargs):
        print("Message:", kwargs)

    @asyncio.coroutine
    def join_handler(**kwargs):
        print("Join:", kwargs)

    @asyncio.coroutine
    def leave_handler(**kwargs):
        print("Leave:", kwargs)

    yield from client.subscribe(
        "public:chat",
        on_message=message_handler,
        on_join=join_handler,
        on_leave=leave_handler
    )


if __name__ == '__main__':
    loop = asyncio.get_event_loop()
    asyncio.ensure_future(run(loop))
    try:
        loop.run_forever()
    except KeyboardInterrupt:
        print("interrupted")
    finally:
        loop.close()
```

Or with Python 3.5 async/await syntax:

```python
import time
import json
import asyncio
from centrifuge import Client, Credentials
from cent import generate_token


async def run():

    # Generate credentials.
    # In production this must only be done on backend side and you should
    # never show secret to client!
    user = "3000"
    timestamp = str(int(time.time()))
    info = json.dumps({"first_name": "Python", "last_name": "Client"})
    token = generate_token("secret", user, timestamp, info=info)

    credentials = Credentials(user, timestamp, info, token)
    address = "ws://localhost:8000/connection/websocket"

    async def connect_handler(**kwargs):
        print("Connected", kwargs)

    async def disconnect_handler(**kwargs):
        print("Disconnected:", kwargs)

    async def connect_error_handler(**kwargs):
        print("Error:", kwargs)

    client = Client(
        address, credentials,
        on_connect=connect_handler,
        on_disconnect=disconnect_handler,
        on_error=connect_error_handler
    )

    await client.connect()

    async def message_handler(**kwargs):
        print("Message:", kwargs)

    async def join_handler(**kwargs):
        print("Join:", kwargs)

    async def leave_handler(**kwargs):
        print("Leave:", kwargs)

    async def error_handler(**kwargs):
        print("Sub error:", kwargs)

    await client.subscribe(
        "public:chat",
        on_message=message_handler,
        on_join=join_handler,
        on_leave=leave_handler,
        on_error=error_handler
    )


if __name__ == '__main__':
    loop = asyncio.get_event_loop()
    asyncio.ensure_future(run())
    try:
        loop.run_forever()
    except KeyboardInterrupt:
        print("interrupted")
    finally:
        loop.close()
```

### TODO

* [connection refresh](https://fzambia.gitbooks.io/centrifugal/content/server/connection_check.html) support
* functional tests
* more detailed API description
