import asyncio
import signal

from centrifuge import Client, \
    ConnectedContext, ConnectingContext, DisconnectedContext, ErrorContext, SubscriptionErrorContext, LeaveContext, \
    JoinContext, PublicationContext, UnsubscribedContext, SubscribedContext, SubscribingContext

# Configure logging.
import logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)

cf_logger = logging.getLogger('centrifuge')
cf_logger.setLevel(logging.DEBUG)


async def connecting_handler(ctx: ConnectingContext):
    logging.info("connecting: %s", ctx)


async def connected_handler(ctx: ConnectedContext):
    logging.info("connected: %s", ctx)


async def disconnected_handler(ctx: DisconnectedContext):
    logging.info("disconnected: %s", ctx)


async def error_handler(ctx: ErrorContext):
    logging.error("client error: %s", ctx.error)


async def subscribing_handler(ctx: SubscribingContext):
    logging.info("subscribing: %s", ctx)


async def subscribed_handler(ctx: SubscribedContext):
    logging.info("subscribed: %s", ctx)


async def unsubscribed_handler(ctx: UnsubscribedContext):
    logging.info("unsubscribed: %s", ctx)


async def publication_handler(ctx: PublicationContext):
    logging.info("publication: %s", ctx)


async def join_handler(ctx: JoinContext):
    logging.info("join: %s", ctx)


async def leave_handler(ctx: LeaveContext):
    logging.info("leave: %s", ctx)


async def subscription_error_handler(ctx: SubscriptionErrorContext):
    logging.info("subscription error: %s", ctx)


async def get_token():
    return 'eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiI0MiIsImV4cCI6MTcwNjU0NTA0MCwiaWF0IjoxNzA1OTQwMjQwfQ.' \
           'HQyladwnFFjkxkZ7L4bYteUmWTxCgh5wbx8qcnIQfAU'


async def shutdown(received_signal, current_loop, cf_client: Client):
    logging.info(f"Received exit signal {received_signal.name}...")
    await cf_client.disconnect()

    tasks = [t for t in asyncio.all_tasks() if t is not
             asyncio.current_task()]

    for task in tasks:
        task.cancel()

    logging.info("Cancelling outstanding tasks")
    await asyncio.gather(*tasks, return_exceptions=True)
    current_loop.stop()


if __name__ == '__main__':
    client = Client(
        'ws://localhost:8000/connection/websocket',
        # REPLACE with your own!
        token='eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiI0MiIsImV4cCI6MTcwNjU0NTA0MCwiaWF0IjoxNzA1OTQwMjQwfQ.'
              'HQyladwnFFjkxkZ7L4bYteUmWTxCgh5wbx8qcnIQfAU',
        # get_token=get_token,
        use_protobuf=False,
    )

    client.on_connecting(connecting_handler)
    client.on_connected(connected_handler)
    client.on_disconnected(disconnected_handler)
    client.on_error(error_handler)

    sub = client.new_subscription('channel')
    sub.on_subscribing(subscribing_handler)
    sub.on_subscribed(subscribed_handler)
    sub.on_unsubscribed(unsubscribed_handler)

    asyncio.ensure_future(client.connect())
    asyncio.ensure_future(sub.subscribe())

    loop = asyncio.get_event_loop()

    signals = (signal.SIGTERM, signal.SIGINT)
    for s in signals:
        loop.add_signal_handler(s, lambda ts=s: asyncio.create_task(shutdown(ts, loop, client)))

    try:
        loop.run_forever()
    finally:
        loop.close()
        logging.info("successfully completed service shutdown")
