from enum import Enum


class _ConnectingCode(Enum):
    """Known client side codes for connection moving to connecting state.
    In most cases just for logging purposes.
    """

    CONNECT_CALLED = 0
    TRANSPORT_CLOSED = 1
    NO_PING = 2
    SUBSCRIBE_TIMEOUT = 3
    UNSUBSCRIBE_ERROR = 4


class _DisconnectedCode(Enum):
    """Known client side codes for connection moving to disconnected state.
    In most cases just for logging purposes.
    """

    DISCONNECT_CALLED = 0
    UNAUTHORIZED = 1
    BAD_PROTOCOL = 2
    MESSAGE_SIZE_LIMIT = 3


class _SubscribingCode(Enum):
    """Known client side codes for subscription moving to subscribing state.
    In most cases just for logging purposes.
    """

    SUBSCRIBE_CALLED = 0
    TRANSPORT_CLOSED = 1


class _UnsubscribedCode(Enum):
    """Known client side codes for subscription moving to unsubscribed state.
    In most cases just for logging purposes.
    """

    UNSUBSCRIBE_CALLED = 0
    UNAUTHORIZED = 1
    CLIENT_CLOSED = 2


class _ErrorCode(Enum):
    """Known client side codes for error event.
    In most cases just for logging purposes.
    """

    TIMEOUT = 1
    TRANSPORT_CLOSED = 2
    CLIENT_DISCONNECTED = 3
    CLIENT_CLOSED = 4
    CLIENT_CONNECT_TOKEN = 5
    CLIENT_REFRESH_TOKEN = 6
    SUBSCRIPTION_UNSUBSCRIBED = 7
    SUBSCRIPTION_SUBSCRIBE_TOKEN = 8
    SUBSCRIPTION_REFRESH_TOKEN = 9
    TRANSPORT_WRITE_ERROR = 10
    CONNECTION_CLOSED = 11
    BAD_CONFIGURATION = 12
    CONNECT_ERROR = 13
    SUBSCRIBE_ERROR = 14
    SUBSCRIPTION_GET_STATE = 15

    # Errors with code > 100 are errors from server.
    TOKEN_EXPIRED = 109


# Subscription feature flags — bitmask sent in SubscribeRequest flag field.
#
# Channel compaction asks the server to replace the string channel name with a
# short numeric ID in subscription pushes (bandwidth optimization). Safe to send
# unconditionally: servers that don't support or don't allow it ignore the bit
# and keep sending the full channel name.
_SUBSCRIPTION_FLAG_CHANNEL_COMPACTION = 1
_SUBSCRIPTION_FLAG_REJECT_UNRECOVERED = 2

# Server error code returned when recovery from the provided position is
# impossible (only sent when _SUBSCRIPTION_FLAG_REJECT_UNRECOVERED was requested).
_ERROR_CODE_UNRECOVERABLE_POSITION = 112

# Server-sent "state invalidated" codes. The server determines that the client's
# cached state and/or token are no longer valid and asks the client to drop them
# and re-sync. 2502 arrives in an Unsubscribe push for a single subscription (the
# client clears the subscription state and resubscribes, since it's >= 2500); 3014
# arrives for the whole connection (the client clears the connection token to force
# a fresh one via get_token, invalidates all subscriptions, reconnects).
_UNSUBSCRIBED_STATE_INVALIDATED = 2502
_DISCONNECTED_STATE_INVALIDATED = 3014
