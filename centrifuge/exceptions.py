class CentrifugeException(Exception):
    """
    CentrifugeException is a base exception for all other exceptions
    in this library.
    """
    pass


class ClientDisconnected(CentrifugeException):
    """
    ConnectionClosed raised when underlying websocket connection closed.
    """
    pass


class DuplicateSubscription(CentrifugeException):
    """
    DuplicateSubscription raised when trying to create a subscription for a channel which
    already has subscription in Client's internal subscription registry. Centrifuge/Centrifugo
    server does not allow subscribing on the same channel twice for the same Client.
    """
    pass


class SubscriptionUnsubscribed(CentrifugeException):
    """
    SubscriptionUnsubscribedError raised when an error subscribing on channel occurred.
    """
    pass


class Timeout(CentrifugeException):
    """
    Timeout raised every time operation times out.
    """
    pass


class ReplyError(CentrifugeException):
    """
    ReplyError raised when an error returned from server as result of presence/history/publish call.
    """
    def __init__(self, code: int, message: str, temporary: bool):
        self.code = code
        self.message = message
        self.temporary = temporary
        super().__init__(f"Error {code}: {message} (temporary: {temporary})")


class Unauthorized(CentrifugeException):
    """
    Unauthorized may be raised from user's get_token functions to indicate
    client is not able to connect or subscribe.
    """
    pass
