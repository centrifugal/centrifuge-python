class CentrifugeError(Exception):
    """CentrifugeError is a base exception for all other exceptions
    in this library.
    """


class ClientDisconnectedError(CentrifugeError):
    """ConnectionClosed raised when underlying websocket connection closed."""


class DuplicateSubscriptionError(CentrifugeError):
    """DuplicateSubscriptionError raised when trying to create a subscription for a channel which
    already has subscription in Client's internal subscription registry. Centrifuge/Centrifugo
    server does not allow subscribing on the same channel twice for the same Client.
    """


class SubscriptionUnsubscribedError(CentrifugeError):
    """SubscriptionUnsubscribedError raised when an error subscribing on a channel occurred."""


class OperationTimeoutError(CentrifugeError):
    """OperationTimeoutError raised every time operation time out."""


class ReplyError(CentrifugeError):
    """ReplyError raised when an error returned
    from the server as a result of presence/history/publish call.
    """

    def __init__(self, code: int, message: str, temporary: bool):
        self.code = code
        self.message = message
        self.temporary = temporary
        super().__init__(f"Error {code}: {message} (temporary: {temporary})")


class UnauthorizedError(CentrifugeError):
    """UnauthorizedError may be raised from user's get_token functions to indicate
    a client is not able to connect or subscribe.
    """
