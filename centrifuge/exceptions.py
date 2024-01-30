class CentrifugeError(Exception):
    """CentrifugeError is a base exception for all other exceptions
    in this library.
    """


class ClientDisconnectedError(CentrifugeError):
    """ClientDisconnectedError may be raised when presence/presence_stats/history/publish/rpc
    operations are attempted on a Client in DISCONNECTED state."""


class DuplicateSubscriptionError(CentrifugeError):
    """DuplicateSubscriptionError raised when trying to create a Subscription for
    a channel which already has subscription in Client's internal subscription registry.
    Centrifuge/Centrifugo server does not allow subscribing on the same channel twice
    for the same Client instance.
    """


class SubscriptionUnsubscribedError(CentrifugeError):
    """SubscriptionUnsubscribedError may be raised when presence/presence_stats/history/publish/rpc
    operations are attempted on a Subscription in UNSUBSCRIBED state."""


class OperationTimeoutError(CentrifugeError):
    """OperationTimeoutError raised when presence/presence_stats/history/publish/rpc operations
    timing out."""


class ReplyError(CentrifugeError):
    """ReplyError raised when an error returned
    from the server as a result of presence/presence_stats/history/publish/rpc calls.
    """

    def __init__(self, code: int, message: str, temporary: bool):
        self.code = code
        self.message = message
        self.temporary = temporary
        super().__init__(f"Error {code}: {message} (temporary: {temporary})")


class UnauthorizedError(CentrifugeError):
    """UnauthorizedError may be raised from user's get_token functions to indicate
    that a client is not able to connect or subscribe.
    """
