import asyncio
import logging
import signal

from centrifuge import (
    CentrifugeError,
    Client,
    ConnectedContext,
    ConnectingContext,
    ConnectionTokenContext,
    DisconnectedContext,
    ErrorContext,
    JoinContext,
    LeaveContext,
    PublicationContext,
    SubscribedContext,
    SubscribingContext,
    SubscriptionErrorContext,
    SubscriptionTokenContext,
    UnsubscribedContext,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
# Configure centrifuge-python logger.
cf_logger = logging.getLogger("centrifuge")
cf_logger.setLevel(logging.DEBUG)


class SignalHandler:
    def __init__(self, client):
        self.client = client

    def handler(self, *_):
        loop = asyncio.get_event_loop()
        loop.create_task(shutdown(self.client))


async def connecting_handler(ctx: ConnectingContext):
    logging.info("connecting: %s", ctx)


async def connected_handler(ctx: ConnectedContext):
    logging.info("connected: %s", ctx)


async def disconnected_handler(ctx: DisconnectedContext):
    logging.info("disconnected: %s", ctx)


async def error_handler(ctx: ErrorContext):
    logging.error("client error: %s", ctx)


async def subscribing_handler(ctx: SubscribingContext):
    logging.info("subscribing: %s", ctx)


async def subscribed_handler(ctx: SubscribedContext):
    logging.info("subscribed: %s", ctx)


async def unsubscribed_handler(ctx: UnsubscribedContext):
    logging.info("unsubscribed: %s", ctx)


async def publication_handler(ctx: PublicationContext):
    logging.info("publication: %s", ctx.pub.data)


async def join_handler(ctx: JoinContext):
    logging.info("join: %s", ctx)


async def leave_handler(ctx: LeaveContext):
    logging.info("leave: %s", ctx)


async def subscription_error_handler(ctx: SubscriptionErrorContext):
    logging.info("subscription error: %s", ctx)


async def get_token(ctx: ConnectionTokenContext) -> str:
    # To reject connection raise centrifuge.UnauthorizedError() exception:
    # raise centrifuge.UnauthorizedError()

    logging.info("get connection token called: %s", ctx)

    # REPLACE with your own logic to get token from the backend!
    example_token = (
        "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiI0MiIsImV4cCI6Nzc1NDI2MTQwOSwiaWF0IjoxNzA2MjYx"
        "NDA5fQ.9jQEr9XqAW1BY9oolmawhtLRx1ZLJZS6ivgYznuf4-Y"
    )
    return example_token


async def get_subscription_token(ctx: SubscriptionTokenContext) -> str:
    # To reject subscription raise centrifuge.UnauthorizedError() exception:
    # raise centrifuge.UnauthorizedError()

    logging.info("get subscription token called: %s", ctx)

    # REPLACE with your own logic to get token from the backend!
    example_token = (
        "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiI0MiIsImV4cCI6Nzc1NDQ0MzA1NywiaWF0IjoxNzA2NDQzMD"
        "U3LCJjaGFubmVsIjoiZXhhbXBsZTpjaGFubmVsIn0._rcyM78ol1MgCqngA4Vyt8P3o1SnDX_hSXhEA_xByKU"
    )
    return example_token


async def run(sub, client):
    await client.connect()
    await sub.subscribe()

    try:
        # Note that in Protobuf case we need to encode payloads to bytes: result = await
        # sub.publish(data=json.dumps({"input": "test"}).encode())
        # But in JSON protocol case we can just pass dict which will be encoded to JSON
        # automatically.
        await sub.publish(data={"input": "test"})
    except CentrifugeError as e:
        logging.error("error publish: %s", e)

    try:
        result = await sub.presence_stats()
        logging.info(result)
    except CentrifugeError as e:
        logging.error("error presence stats: %s", e)

    try:
        result = await sub.presence()
        logging.info(result)
    except CentrifugeError as e:
        logging.error("error presence: %s", e)

    try:
        result = await sub.history(limit=1, reverse=True)
        logging.info(result)
    except CentrifugeError as e:
        logging.error("error history: %s", e)

    logging.info("all done, connection is still alive, press Ctrl+C to exit")


async def shutdown(client):
    logging.info("Received exit signal")
    await client.disconnect()

    tasks = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
    for task in tasks:
        task.cancel()

    logging.info("Cancelling outstanding tasks")
    await asyncio.gather(*tasks, return_exceptions=True)


async def run_example():
    client = Client(
        "ws://localhost:8000/connection/websocket",
        get_token=get_token,
        use_protobuf=False,
    )
    signal_handler = SignalHandler(client)

    client.on_connecting(connecting_handler)
    client.on_connected(connected_handler)
    client.on_disconnected(disconnected_handler)
    client.on_error(error_handler)

    sub = client.new_subscription("example:channel", get_token=get_subscription_token)
    sub.on_subscribing(subscribing_handler)
    sub.on_subscribed(subscribed_handler)
    sub.on_unsubscribed(unsubscribed_handler)
    sub.on_publication(publication_handler)
    sub.on_join(join_handler)
    sub.on_leave(leave_handler)

    await run(sub, client)

    signal.signal(signal.SIGTERM, signal_handler.handler)
    signal.signal(signal.SIGINT, signal_handler.handler)


if __name__ == "__main__":
    asyncio.run(run_example())
