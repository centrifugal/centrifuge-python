import asyncio
from centrifuge import Client, Credentials, SubscriptionError


"""
# Configure centrifuge logger
import logging
logger = logging.getLogger('centrifuge')
logger.setLevel(logging.DEBUG)
handler = logging.StreamHandler()
handler.setLevel(logging.DEBUG)
logger.addHandler(handler)
"""


def run(loop):
    credentials = Credentials("", "", "", "")
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
            message_handler, join_handler=join_handler, leave_handler=leave_handler
        )
    except SubscriptionError:
        print(str(SubscriptionError))
        return
    else:
        print(sid)

    while True:
        yield from asyncio.sleep(1)


if __name__ == '__main__':
    loop = asyncio.get_event_loop()
    loop.run_until_complete(run(loop))
    loop.close()
