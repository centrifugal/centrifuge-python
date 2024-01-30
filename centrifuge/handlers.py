from centrifuge.contexts import (
    ConnectedContext,
    ConnectingContext,
    DisconnectedContext,
    ErrorContext,
    JoinContext,
    LeaveContext,
    PublicationContext,
    ServerJoinContext,
    ServerLeaveContext,
    ServerPublicationContext,
    ServerSubscribedContext,
    ServerSubscribingContext,
    ServerUnsubscribedContext,
    SubscribedContext,
    SubscribingContext,
    SubscriptionErrorContext,
    UnsubscribedContext,
)


class ClientEventHandler:
    """ClientEventHandler is a set of callbacks called on various Client-level events."""

    async def on_connecting(self, ctx: ConnectingContext):
        """Called when connecting. This may be initial connecting, or
        temporary loss of connection with automatic reconnect
        """

    async def on_connected(self, ctx: ConnectedContext) -> None:
        """Called when connected."""

    async def on_disconnected(self, ctx: DisconnectedContext) -> None:
        """Called when disconnected."""

    async def on_error(self, ctx: ErrorContext) -> None:
        """Called when there's an error."""

    async def on_subscribed(self, ctx: ServerSubscribedContext) -> None:
        """Called when subscribed on server-side subscription."""

    async def on_subscribing(self, ctx: ServerSubscribingContext) -> None:
        """Called when subscribing to server-side subscription."""

    async def on_unsubscribed(self, ctx: ServerUnsubscribedContext) -> None:
        """Called when unsubscribed from server-side subscription."""

    async def on_publication(self, ctx: ServerPublicationContext) -> None:
        """Called when there's a publication coming from a server-side subscription."""

    async def on_join(self, ctx: ServerJoinContext) -> None:
        """Called when some client joined channel in server-side subscription."""

    async def on_leave(self, ctx: ServerLeaveContext) -> None:
        """Called when some client left channel in server-side subscription."""


class SubscriptionEventHandler:
    """SubscriptionEventHandler is a set of callbacks called on various
    Subscription-level events."""

    async def on_subscribing(self, ctx: SubscribingContext) -> None:
        """Called when subscribing. This may be initial subscribing attempt,
        or temporary loss with automatic resubscribe
        """

    async def on_subscribed(self, ctx: SubscribedContext) -> None:
        """Called when subscribed."""

    async def on_unsubscribed(self, ctx: UnsubscribedContext) -> None:
        """Called when unsubscribed. No auto re-subscribing will happen after this"""

    async def on_publication(self, ctx: PublicationContext) -> None:
        """Called when there's a publication coming from a channel"""

    async def on_join(self, ctx: JoinContext) -> None:
        """Called when some client joined channel (join/leave must be enabled on server side)."""

    async def on_leave(self, ctx: LeaveContext) -> None:
        """Called when some client left channel (join/leave must be enabled on server side)"""

    async def on_error(self, ctx: SubscriptionErrorContext) -> None:
        """Called when various subscription async errors happen.

        In most cases this is only for logging purposes
        """
