from centrifuge.contexts import SubscriptionErrorContext, LeaveContext, JoinContext, PublicationContext, \
    UnsubscribedContext, SubscribedContext, SubscribingContext, ServerLeaveContext, ServerJoinContext,\
    ServerPublicationContext, ServerUnsubscribedContext, ServerSubscribingContext, ServerSubscribedContext,\
    ErrorContext, DisconnectedContext, ConnectedContext, ConnectingContext


class _ConnectionEventHandler:
    """_ConnectionEventHandler is a set of callbacks called on various client events."""
    async def on_connecting(self, ctx: ConnectingContext):
        """Called when connecting. This may be initial connecting, or
        temporary loss of connection with automatic reconnect"""
        pass

    async def on_connected(self,  ctx: ConnectedContext):
        """Called when connected."""
        pass

    async def on_disconnected(self, ctx: DisconnectedContext):
        """Called when disconnected."""
        pass

    async def on_error(self, ctx: ErrorContext):
        """Called when there's an error."""
        pass

    async def on_subscribed(self, ctx: ServerSubscribedContext):
        """Called when subscribed on server-side subscription."""
        pass

    async def on_subscribing(self, ctx: ServerSubscribingContext):
        """Called when subscribing to server-side subscription."""
        pass

    async def on_unsubscribed(self, ctx: ServerUnsubscribedContext):
        """Called when unsubscribed from server-side subscription."""
        pass

    async def on_publication(self, ctx: ServerPublicationContext):
        """Called when there's a publication coming from a server-side subscription."""
        pass

    async def on_join(self, ctx: ServerJoinContext):
        """Called when some client joined channel in server-side subscription."""
        pass

    async def on_leave(self, ctx: ServerLeaveContext):
        """Called when some client left channel in server-side subscription."""
        pass


class _SubscriptionEventHandler:
    """_SubscriptionEventHandler is a set of callbacks called on various subscription events."""
    async def on_subscribing(self, ctx: SubscribingContext):
        """Called when subscribing. This may be initial subscribing attempt,
        or temporary loss with automatic resubscribe"""
        pass

    async def on_subscribed(self, ctx: SubscribedContext):
        """Called when subscribed."""
        pass

    async def on_unsubscribed(self, ctx: UnsubscribedContext):
        """Called when unsubscribed. No auto re-subscribing will happen after this"""
        pass

    async def on_publication(self, ctx: PublicationContext):
        """Called when there's a publication coming from a channel"""
        pass

    async def on_join(self, ctx: JoinContext):
        """Called when some client joined channel (join/leave must be enabled on server side)."""
        pass

    async def on_leave(self, ctx: LeaveContext):
        """Called when some client left channel (join/leave must be enabled on server side)"""
        pass

    async def on_error(self, ctx: SubscriptionErrorContext):
        """Called when various subscription async errors happen. In most cases this is only for logging purposes"""
        pass
