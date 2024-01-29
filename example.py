import asyncio
import logging
import signal
from typing import Any

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
    Subscription,
)
from centrifuge.handlers import SubscriptionEventHandler, ConnectionEventHandler

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
# Configure centrifuge-python logger.
cf_logger = logging.getLogger("centrifuge")
cf_logger.setLevel(logging.DEBUG)


class SignalHandler:
    def __init__(self, client: Client) -> None:
        self.client = client

    def handler(self, *_: Any) -> None:
        loop = asyncio.get_event_loop()
        loop.create_task(shutdown(self.client))


class ConnectionEventLoggerHandler(ConnectionEventHandler):
    async def on_connecting(self, ctx: ConnectingContext) -> None:
        logging.info("connecting: %s", ctx)

    async def on_connected(self, ctx: ConnectedContext) -> None:
        logging.info("connected: %s", ctx)

    async def on_disconnected(self, ctx: DisconnectedContext) -> None:
        logging.info("disconnected: %s", ctx)

    async def on_error(self, ctx: ErrorContext) -> None:
        logging.error("client error: %s", ctx)


class SubscriptionEventLoggerHandler(SubscriptionEventHandler):
    async def on_subscribing(self, ctx: SubscribingContext) -> None:
        logging.info("subscribing: %s", ctx)

    async def on_subscribed(self, ctx: SubscribedContext) -> None:
        logging.info("subscribed: %s", ctx)

    async def on_unsubscribed(self, ctx: UnsubscribedContext) -> None:
        logging.info("unsubscribed: %s", ctx)

    async def on_publication(self, ctx: PublicationContext) -> None:
        logging.info("publication: %s", ctx.pub.data)

    async def on_join(self, ctx: JoinContext) -> None:
        logging.info("join: %s", ctx)

    async def on_leave(self, ctx: LeaveContext) -> None:
        logging.info("leave: %s", ctx)

    async def on_error(self, ctx: SubscriptionErrorContext) -> None:
        logging.info("subscription error: %s", ctx)


async def get_token(ctx: ConnectionTokenContext) -> str:
    # To reject connection raise centrifuge.UnauthorizedError() exception:
    # raise centrifuge.UnauthorizedError()

    logging.info("get connection token called: %s", ctx)

    # REPLACE with your own logic to get token from the backend!
    example_token = (
        "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiI0MiIsImV4cCI6Nzc1NDI2MTQwOSwiaWF0Ij"
        "oxNzA2MjYxNDA5fQ.9jQEr9XqAW1BY9oolmawhtLRx1ZLJZS6ivgYznuf4-Y"
    )
    return example_token


async def get_subscription_token(ctx: SubscriptionTokenContext) -> str:
    # To reject subscription raise centrifuge.UnauthorizedError() exception:
    # raise centrifuge.UnauthorizedError()

    logging.info("get subscription token called: %s", ctx)

    # REPLACE with your own logic to get token from the backend!
    example_token = (
        "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiI0MiIsImV4cCI6Nzc1NDQ0MzA1NywiaWF0IjoxNz"
        "A2NDQzMDU3LCJjaGFubmVsIjoiZXhhbXBsZTpjaGFubmVsIn0._rcyM78ol1MgCqngA4Vyt8P3o1SnDX_hSXhEA"
        "_xByKU"
    )
    return example_token


async def run(sub: Subscription, client: Client) -> None:
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
        presence_stats = await sub.presence_stats()
        logging.info(presence_stats)
    except CentrifugeError as e:
        logging.error("error presence stats: %s", e)

    try:
        presence = await sub.presence()
        logging.info(presence)
    except CentrifugeError as e:
        logging.error("error presence: %s", e)

    try:
        history = await sub.history(limit=10, reverse=True)
        logging.info(history)
    except CentrifugeError as e:
        logging.error("error history: %s", e)

    logging.info("all done, connection is still alive, press Ctrl+C to exit")


async def shutdown(client: Client) -> None:
    logging.info("Received exit signal")
    await client.disconnect()

    tasks = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
    for task in tasks:
        task.cancel()

    logging.info("Cancelling outstanding tasks")
    await asyncio.gather(*tasks, return_exceptions=True)


async def run_example() -> None:
    client = Client(
        "ws://localhost:8000/connection/websocket",
        events=ConnectionEventLoggerHandler(),
        get_token=get_token,
        use_protobuf=False,
    )
    signal_handler = SignalHandler(client)

    sub = client.new_subscription(
        "example:channel",
        events=SubscriptionEventLoggerHandler(),
        get_token=get_subscription_token,
    )

    await run(sub, client)

    signal.signal(signal.SIGTERM, signal_handler.handler)
    signal.signal(signal.SIGINT, signal_handler.handler)


if __name__ == "__main__":
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(run_example())
    loop.run_forever()
