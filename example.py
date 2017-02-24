import time
import json
import asyncio
from centrifuge import Client, Credentials, CentrifugeException, PrivateSign
from cent import generate_token, generate_channel_sign

# Configure centrifuge logger
import logging
logger = logging.getLogger('centrifuge')
logger.setLevel(logging.DEBUG)
handler = logging.StreamHandler()
handler.setLevel(logging.DEBUG)
logger.addHandler(handler)


def run():

    # Generate credentials.
    # In production this must only be done on backend side and you should
    # never show secret to client!
    user = "3000"
    timestamp = str(int(time.time()))
    info = json.dumps({"first_name": "Python", "last_name": "Client"})
    token = generate_token("secret", user, timestamp, info=info)

    credentials = Credentials(user, timestamp, info, token)
    address = "ws://localhost:8000/connection/websocket"

    @asyncio.coroutine
    def connect_handler(**kwargs):
        print("Connected", kwargs)

    @asyncio.coroutine
    def disconnect_handler(**kwargs):
        print("Disconnected:", kwargs)

    @asyncio.coroutine
    def connect_error_handler(**kwargs):
        print("Error:", kwargs)

    @asyncio.coroutine
    def private_sub_handler(**kwargs):
        print("Private channel request:", kwargs)
        client_id = kwargs.get("client_id")
        channels = kwargs.get("channels")
        data = {}
        for channel in channels:
            # in production here must be a call to your application backend to get
            # private channel sign, in example we simply generate sign for every channel
            # on client side using function from cent library for simplicity. Your real
            # app clients should never see this secret key!
            data[channel] = PrivateSign(generate_channel_sign("secret", client_id, channel))

        return data

    client = Client(
        address, credentials,
        on_connect=connect_handler,
        on_disconnect=disconnect_handler,
        on_error=connect_error_handler,
        on_private_sub=private_sub_handler
    )

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

    @asyncio.coroutine
    def subscribe_handler(**kwargs):
        print("Sub subscribed:", kwargs)

    @asyncio.coroutine
    def unsubscribe_handler(**kwargs):
        print("Sub unsubscribed:", kwargs)

    @asyncio.coroutine
    def error_handler(**kwargs):
        print("Sub error:", kwargs)

    sub = yield from client.subscribe(
        "public:chat",
        on_message=message_handler,
        on_join=join_handler,
        on_leave=leave_handler,
        on_error=error_handler,
        on_subscribe=subscribe_handler,
        on_unsubscribe=unsubscribe_handler
    )

    try:
        success = yield from sub.publish({})
    except CentrifugeException as e:
        print("Publish error:", type(e), e)
    else:
        print("Publish successful:", success)

    try:
        history = yield from sub.history()
    except CentrifugeException as e:
        print("Channel history error:", type(e), e)
    else:
        print("Channel history:", history)

    try:
        presence = yield from sub.presence()
    except CentrifugeException as e:
        print("Channel presence error:", type(e), e)
    else:
        print("Channel presence:", presence)


if __name__ == '__main__':
    loop = asyncio.get_event_loop()
    asyncio.ensure_future(run())
    try:
        loop.run_forever()
    except KeyboardInterrupt:
        print("interrupted")
    finally:
        loop.close()
