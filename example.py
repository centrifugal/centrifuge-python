import time
import json
import asyncio
from centrifuge import Client, Credentials, SubscriptionError
from cent import generate_token

# Configure centrifuge logger
import logging
logger = logging.getLogger('centrifuge')
logger.setLevel(logging.DEBUG)
handler = logging.StreamHandler()
handler.setLevel(logging.DEBUG)
logger.addHandler(handler)


def run(loop):

    # Generate credentials.
    # In production this must only be done on backend side and you should
    # never show secret to client!
    user = "3000"
    timestamp = str(int(time.time()))
    info = json.dumps({"first_name": "Python", "last_name": "Client"})
    token = generate_token("secret", user, timestamp, info=info)

    credentials = Credentials(user, timestamp, info, token)
    client = Client(loop)

    yield from client.connect("ws://localhost:8000/connection/websocket", credentials)

    @asyncio.coroutine
    def message_handler(msg):
        print("Message:", msg)

    @asyncio.coroutine
    def join_handler(msg):
        print("Join:", msg)

    @asyncio.coroutine
    def leave_handler(msg):
        print("Leave:", msg)

    try:
        sid = yield from client.subscribe(
            "public:chat",
            message_handler, join=join_handler, leave=leave_handler
        )
    except SubscriptionError:
        print(str(SubscriptionError))
        return
    else:
        print(sid)


if __name__ == '__main__':
    loop = asyncio.get_event_loop()
    asyncio.ensure_future(run(loop))
    try:
        loop.run_forever()
    except KeyboardInterrupt:
        print("interrupted")
    finally:
        loop.close()
