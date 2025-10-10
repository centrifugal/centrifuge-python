import asyncio
import logging
import signal

from centrifuge import (
    CentrifugeError,
    Client,
    ClientEventHandler,
    ConnectedContext,
    ConnectingContext,
    DisconnectedContext,
    ErrorContext,
    JoinContext,
    LeaveContext,
    PublicationContext,
    SubscribedContext,
    SubscribingContext,
    SubscriptionErrorContext,
    UnsubscribedContext,
    SubscriptionEventHandler,
    ServerSubscribedContext,
    ServerSubscribingContext,
    ServerUnsubscribedContext,
    ServerPublicationContext,
    ServerJoinContext,
    ServerLeaveContext,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
# Configure centrifuge-python logger.
cf_logger = logging.getLogger("centrifuge")
cf_logger.setLevel(logging.DEBUG)


async def get_client_token() -> str:
    # To reject connection raise centrifuge.UnauthorizedError() exception:
    # raise centrifuge.UnauthorizedError()

    logging.info("get client token called")

    # REPLACE with your own logic to get token from the backend!
    example_token = (
        "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiI0MiIsImV4cCI6Nzc1NDI2MTQwOSwiaWF0Ij"
        "oxNzA2MjYxNDA5fQ.9jQEr9XqAW1BY9oolmawhtLRx1ZLJZS6ivgYznuf4-Y"
    )
    return example_token


async def get_subscription_token(channel: str) -> str:
    # To reject subscription raise centrifuge.UnauthorizedError() exception:
    # raise centrifuge.UnauthorizedError()

    logging.info("get subscription token called for channel %s", channel)

    # REPLACE with your own logic to get token from the backend!
    example_token = (
        "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiI0MiIsImV4cCI6Nzc1NDQ0MzA1NywiaWF0IjoxNz"
        "A2NDQzMDU3LCJjaGFubmVsIjoiZXhhbXBsZTpjaGFubmVsIn0._rcyM78ol1MgCqngA4Vyt8P3o1SnDX_hSXhEA"
        "_xByKU"
    )
    return example_token


class ClientEventLoggerHandler(ClientEventHandler):
    """Check out comments of ClientEventHandler methods to see when they are called."""

    async def on_connecting(self, ctx: ConnectingContext) -> None:
        logging.info("connecting: %s", ctx)

    async def on_connected(self, ctx: ConnectedContext) -> None:
        logging.info("connected: %s", ctx)

    async def on_disconnected(self, ctx: DisconnectedContext) -> None:
        logging.info("disconnected: %s", ctx)

    async def on_error(self, ctx: ErrorContext) -> None:
        logging.error("client error: %s", ctx)

    async def on_subscribed(self, ctx: ServerSubscribedContext) -> None:
        logging.info("subscribed server-side sub: %s", ctx)

    async def on_subscribing(self, ctx: ServerSubscribingContext) -> None:
        logging.info("subscribing server-side sub: %s", ctx)

    async def on_unsubscribed(self, ctx: ServerUnsubscribedContext) -> None:
        logging.info("unsubscribed from server-side sub: %s", ctx)

    async def on_publication(self, ctx: ServerPublicationContext) -> None:
        logging.info("publication from server-side sub: %s", ctx.pub.data)

    async def on_join(self, ctx: ServerJoinContext) -> None:
        logging.info("join in server-side sub: %s", ctx)

    async def on_leave(self, ctx: ServerLeaveContext) -> None:
        logging.info("leave in server-side sub: %s", ctx)


class SubscriptionEventLoggerHandler(SubscriptionEventHandler):
    """Check out comments of SubscriptionEventHandler methods to see when they are called."""

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
        logging.error("subscription error: %s", ctx)


def run_example():
    client = Client(
        "ws://localhost:8000/connection/websocket",
        events=ClientEventLoggerHandler(),
        get_token=get_client_token,
        use_protobuf=False,
    )

    sub = client.new_subscription(
        "example:channel",
        events=SubscriptionEventLoggerHandler(),
        get_token=get_subscription_token,
        # you can pass `delta=centrifuge.DeltaType.FOSSIL` (should be also enabled on server)
        # and other options here.
    )

    async def run():
        await client.connect()
        await sub.subscribe()

        try:
            # Note that in Protobuf case we need to encode payloads to bytes:
            # result = await sub.publish(data=json.dumps({"input": "test"}).encode())
            # But in JSON protocol case we can just pass dict which will be encoded to
            # JSON automatically.
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

        logging.info("all done, client connection is still alive, press Ctrl+C to exit")

    asyncio.ensure_future(run())
    loop = asyncio.get_event_loop()

    async def shutdown(received_signal):
        logging.info("received exit signal %s...", received_signal.name)
        await client.disconnect()

        tasks = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
        for task in tasks:
            task.cancel()

        logging.info("Cancelling outstanding tasks")
        await asyncio.gather(*tasks, return_exceptions=True)
        loop.stop()

    signals = (signal.SIGTERM, signal.SIGINT)
    for s in signals:
        loop.add_signal_handler(
            s, lambda received_signal=s: asyncio.create_task(shutdown(received_signal))
        )

    try:
        loop.run_forever()
    finally:
        loop.close()
        logging.info("successfully completed service shutdown")


if __name__ == "__main__":
    run_example()
