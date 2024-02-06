from dataclasses import dataclass
from typing import Optional, Any

from centrifuge.types import ClientInfo, Publication, StreamPosition


@dataclass
class ConnectedContext:
    """ConnectedContext is a context passed to on_connected callback.

    Attributes
    ----------
        client: client ID.
        version: server version.
        data: optional data returned from server on connect (i.e. can be None).
    """

    client: str
    version: str
    data: Optional[Any]


@dataclass
class ConnectingContext:
    """ConnectingContext is a context passed to on_connecting callback.

    Attributes
    ----------
        code: code of state transition.
        reason: reason of state transition.
    """

    code: int
    reason: str


@dataclass
class DisconnectedContext:
    """DisconnectedContext is a context passed to on_disconnected callback.

    Attributes
    ----------
        code: code of state transition.
        reason: reason of state transition.
    """

    code: int
    reason: str


@dataclass
class ErrorContext:
    """ErrorContext is a context passed to on_error callback."""

    code: int
    error: Exception


@dataclass
class ServerSubscribingContext:
    """ServerSubscribingContext is a context
    passed to on_subscribing callback for server-side subscriptions.
    """

    channel: str


@dataclass
class ServerSubscribedContext:
    """ServerSubscribedContext is a context
    passed to on_subscribed callback for server-side subscriptions.
    """

    channel: str
    recoverable: bool
    positioned: bool
    stream_position: Optional[StreamPosition]
    was_recovering: bool
    recovered: bool
    data: Optional[Any]


@dataclass
class ServerUnsubscribedContext:
    """ServerUnsubscribedContext is a context
    passed to on_unsubscribed callback for server-side subscriptions.
    """

    channel: str


@dataclass
class ServerPublicationContext:
    """ServerPublicationContext is a context passed to on_publication callback for server-side
    subscriptions.

    Attributes
    ----------
        channel: channel from which publication received.
        pub: Publication object.
    """

    channel: str
    pub: Publication


@dataclass
class ServerJoinContext:
    """ServerJoinContext is a context passed to on_join callback for server-side subscriptions."""

    channel: str
    info: ClientInfo


@dataclass
class ServerLeaveContext:
    """
    ServerLeaveContext is a context passed to on_leave callback for server-side subscriptions.
    """

    channel: str
    info: ClientInfo


@dataclass
class SubscribingContext:
    """SubscribingContext is a context passed to on_subscribing callback."""

    code: int
    reason: str


@dataclass
class SubscribedContext:
    """SubscribedContext is a context passed to on_subscribed callback."""

    channel: str
    recoverable: bool
    positioned: bool
    stream_position: Optional[StreamPosition]
    was_recovering: bool
    recovered: bool
    data: Optional[Any]


@dataclass
class UnsubscribedContext:
    """UnsubscribedContext is a context passed to on_unsubscribed callback."""

    code: int
    reason: str


@dataclass
class PublicationContext:
    """PublicationContext is a context passed to on_publication callback."""

    pub: Publication


@dataclass
class JoinContext:
    """JoinContext is a context passed to on_join callback."""

    info: ClientInfo


@dataclass
class LeaveContext:
    """LeaveContext is a context passed to on_leave callback."""

    info: ClientInfo


@dataclass
class SubscriptionErrorContext:
    """SubscriptionErrorContext is a context passed to on_error callback of subscription."""

    code: int
    error: Exception
