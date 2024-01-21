import time
import json
import asyncio
import signal
from centrifuge import Client, CentrifugeException

# Configure centrifuge logger
import logging
logger = logging.getLogger('centrifuge')
logger.setLevel(logging.DEBUG)
handler = logging.StreamHandler()
handler.setLevel(logging.DEBUG)
logger.addHandler(handler)


async def connecting_handler(**kwargs):
    print("Connecting", kwargs)


async def connected_handler(**kwargs):
    print("Connected:", kwargs)


async def disconnected_handler(**kwargs):
    print("Disconnected:", kwargs)


async def error_handler(**kwargs):
    print("Error:", kwargs)


async def subscribing_handler(**kwargs):
    print("Subscribing:", kwargs)


async def subscribed_handler(**kwargs):
    print("Subscribed:", kwargs)


async def unsubscribed_handler(**kwargs):
    print("Unsubscribed:", kwargs)


async def publication_handler(**kwargs):
    print("Publication:", kwargs)


async def join_handler(**kwargs):
    print("Join:", kwargs)


async def leave_handler(**kwargs):
    print("Leave:", kwargs)


async def subscription_error_handler(**kwargs):
    print("Subscription error:", kwargs)


async def run(client: Client):
    await client.connect()

    sub = await client.subscribe(
        "public:chat",
        on_subscribing=subscribing_handler,
        on_subscribed=subscribed_handler,
        on_unsubscribed=unsubscribed_handler,
        on_error=subscription_error_handler,
        on_publication=publication_handler,
        on_join=join_handler,
        on_leave=leave_handler,
    )

    try:
        success = await sub.publish({})
    except CentrifugeException as e:
        print("Publish error:", type(e), e)
    else:
        print("Publish successful:", success)

    try:
        history = await sub.history()
    except CentrifugeException as e:
        print("Channel history error:", type(e), e)
    else:
        print("Channel history:", history)

    try:
        presence = await sub.presence()
    except CentrifugeException as e:
        print("Channel presence error:", type(e), e)
    else:
        print("Channel presence:", presence)


async def shutdown(signal, loop, client: Client):
    logging.info(f"Received exit signal {signal.name}...")
    await client.disconnect()

    tasks = [t for t in asyncio.all_tasks() if t is not
             asyncio.current_task()]

    for task in tasks:
        task.cancel()

    logging.info("Cancelling outstanding tasks")
    await asyncio.gather(*tasks, return_exceptions=True)
    loop.stop()


if __name__ == '__main__':
    cfClient = Client(
        "ws://localhost:8000/connection/websocket",
        on_connecting=connected_handler,
        on_connected=connecting_handler,
        on_disconnected=disconnected_handler,
        on_error=error_handler,
    )

    loop = asyncio.get_event_loop()

    signals = (signal.SIGTERM, signal.SIGINT)
    for s in signals:
        loop.add_signal_handler(
            s, lambda s=s: asyncio.create_task(shutdown(s, loop, cfClient)))

    asyncio.ensure_future(run(cfClient))

    try:
        loop.run_forever()
    finally:
        logging.info("Successfully shutdown service")
        loop.close()
