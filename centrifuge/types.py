from dataclasses import dataclass
from typing import Dict, List, Optional, Any


@dataclass
class StreamPosition:
    """StreamPosition represents a position in stream."""

    offset: int
    epoch: str


@dataclass
class ClientInfo:
    """ClientInfo represents information about client connection.

    Attributes
    ----------
        client: client ID.
        user: user ID.
        conn_info: optional connection information (i.e. may be None).
        chan_info: optional channel information (i.e. may be None).
    """

    client: str
    user: str
    conn_info: Optional[Any]
    chan_info: Optional[Any]


@dataclass
class Publication:
    """Publication represents a data published to channel.

    Attributes
    ----------
        offset: publication offset in channel stream.
        data: published data.
        info: optional client information (i.e. may be None).
        delta: whether this publication is a delta message or not
    """

    offset: int
    data: Any
    info: Optional[ClientInfo]
    tags: Dict[str, str]
    delta: bool


@dataclass
class PublishResult:
    """PublishResult is a result of publish operation."""


@dataclass
class RpcResult:
    """RpcResult is a result of RPC operation."""

    data: Any


@dataclass
class PresenceResult:
    """PresenceResult is a result of presence operation."""

    clients: Dict[str, ClientInfo]


@dataclass
class PresenceStatsResult:
    """PresenceStatsResult is a result of presence stats operation."""

    num_clients: int
    num_users: int


@dataclass
class HistoryResult:
    """HistoryResult is a result of history operation."""

    publications: List[Publication]
    offset: int
    epoch: str
