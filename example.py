import asyncio
import signal

from centrifuge import Client, ConnectedContext, ConnectingContext, DisconnectedContext, \
    ErrorContext, SubscriptionErrorContext, LeaveContext, JoinContext, PublicationContext, \
    UnsubscribedContext, SubscribedContext, SubscribingContext, ConnectionTokenContext, \
    SubscriptionTokenContext, CentrifugeException

import logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
# Configure centrifuge-python logger.
cf_logger = logging.getLogger('centrifuge')
cf_logger.setLevel(logging.DEBUG)


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
    # To reject connection raise centrifuge.Unauthorized() exception:
    # raise centrifuge.Unauthorized()

    logging.info("get connection token called: %s", ctx)

    # REPLACE with your own logic to get token from the backend!
    example_token = 'eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiI0MiIsImV4cCI6Nzc1NDI2MTQwOSwiaWF0IjoxNzA2MjYx' \
                    'NDA5fQ.9jQEr9XqAW1BY9oolmawhtLRx1ZLJZS6ivgYznuf4-Y'
    return example_token


async def get_subscription_token(ctx: SubscriptionTokenContext) -> str:
    # To reject subscription raise centrifuge.Unauthorized() exception:
    # raise centrifuge.Unauthorized()

    logging.info("get subscription token called: %s", ctx)

    # REPLACE with your own logic to get token from the backend!
    example_token = 'eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiI0MiIsImV4cCI6Nzc1NDQ0MzA1NywiaWF0IjoxNzA2NDQzMD' \
                    'U3LCJjaGFubmVsIjoiZXhhbXBsZTpjaGFubmVsIn0._rcyM78ol1MgCqngA4Vyt8P3o1SnDX_hSXhEA_xByKU'
    return example_token


def run_example():
    client = Client(
        'ws://localhost:8000/connection/websocket',
        get_token=get_token,
        use_protobuf=False,
    )
    client.on_connecting(connecting_handler)
    client.on_connected(connected_handler)
    client.on_disconnected(disconnected_handler)
    client.on_error(error_handler)

    sub = client.new_subscription('example:channel', get_token=get_subscription_token)
    sub.on_subscribing(subscribing_handler)
    sub.on_subscribed(subscribed_handler)
    sub.on_unsubscribed(unsubscribed_handler)
    sub.on_publication(publication_handler)
    sub.on_join(join_handler)
    sub.on_leave(leave_handler)

    async def run():
        asyncio.ensure_future(client.connect())
        asyncio.ensure_future(sub.subscribe())

        try:
            # Note that in Protobuf case we need to encode payloads to bytes:
            # result = await sub.publish(data=json.dumps({"input": "test"}).encode())
            # But in JSON protocol case we can just pass dict which will be encoded to JSON automatically.
            await sub.publish(data={"input": "test"})
        except CentrifugeException as e:
            logging.error("error publish: %s", e)

        try:
            result = await sub.presence_stats()
            logging.info(result)
        except CentrifugeException as e:
            logging.error("error presence stats: %s", e)

        try:
            result = await sub.presence()
            logging.info(result)
        except CentrifugeException as e:
            logging.error("error presence: %s", e)

        try:
            result = await sub.history(limit=1, reverse=True)
            logging.info(result)
        except CentrifugeException as e:
            logging.error("error history: %s", e)

        logging.info("all done, connection is still alive, press Ctrl+C to exit")

    asyncio.ensure_future(run())
    loop = asyncio.get_event_loop()

    async def shutdown(received_signal):
        logging.info(f"Received exit signal {received_signal.name}...")
        await client.disconnect()

        tasks = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
        for task in tasks:
            task.cancel()

        logging.info("Cancelling outstanding tasks")
        await asyncio.gather(*tasks, return_exceptions=True)
        loop.stop()

    signals = (signal.SIGTERM, signal.SIGINT)
    for s in signals:
        loop.add_signal_handler(s, lambda received_signal=s: asyncio.create_task(shutdown(received_signal)))

    try:
        loop.run_forever()
    finally:
        loop.close()
        logging.info("successfully completed service shutdown")


if __name__ == '__main__':
    run_example()
