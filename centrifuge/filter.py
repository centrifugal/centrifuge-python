"""Builders for server-side publication filtering by publication tags.

A filter is either a leaf node (a comparison such as ``ticker == "AAPL"``) or a
logical node combining child nodes with and/or/not. The server evaluates the
filter against each publication's tags and delivers only matching publications.
See https://centrifugal.dev/docs/server/publication_filtering.

Publication filtering must be enabled for the namespace on the server
(``allow_tags_filter``) and cannot be combined with delta compression.

Build leaf comparisons with the :class:`Filter` helpers and combine them with
:meth:`Filter.all` (AND), :meth:`Filter.any` (OR) and :meth:`Filter.negate`
(NOT). Pass the result via ``Client.new_subscription(..., tags_filter=...)`` or
``Subscription.set_tags_filter(...)``::

    # (ticker == "AAPL") AND (price >= "100") AND (source in ["NASDAQ", "NYSE"])
    tags_filter = Filter.all(
        Filter.eq("ticker", "AAPL"),
        Filter.gte("price", "100"),
        Filter.is_in("source", ["NASDAQ", "NYSE"]),
    )

    # NOT (source == "NYSE")
    tags_filter = Filter.negate(Filter.eq("source", "NYSE"))
"""

from dataclasses import dataclass
from typing import Any, Dict, List, Optional


@dataclass(frozen=True)
class FilterNode:
    """A node in a publication tags-filter expression tree. Build with :class:`Filter`."""

    node: Dict[str, Any]


class Filter:
    """Static builders for :class:`FilterNode` expressions.

    Comparison helpers (:meth:`eq`, :meth:`is_in`, ...) create leaf nodes;
    :meth:`all`, :meth:`any` and :meth:`negate` combine them.
    """

    @staticmethod
    def _leaf(key: str, cmp: str, val: str = "", vals: Optional[List[str]] = None) -> FilterNode:
        node: Dict[str, Any] = {"key": key, "cmp": cmp}
        if val:
            node["val"] = val
        if vals:
            node["vals"] = list(vals)
        return FilterNode(node)

    @staticmethod
    def _logical(op: str, nodes: List[FilterNode]) -> FilterNode:
        return FilterNode({"op": op, "nodes": [n.node for n in nodes]})

    @staticmethod
    def eq(key: str, val: str) -> FilterNode:
        """Tag ``key`` equals ``val``."""
        return Filter._leaf(key, "eq", val=val)

    @staticmethod
    def neq(key: str, val: str) -> FilterNode:
        """Tag ``key`` does not equal ``val``."""
        return Filter._leaf(key, "neq", val=val)

    @staticmethod
    def is_in(key: str, vals: List[str]) -> FilterNode:
        """Tag ``key`` is one of ``vals``."""
        return Filter._leaf(key, "in", vals=vals)

    @staticmethod
    def not_in(key: str, vals: List[str]) -> FilterNode:
        """Tag ``key`` is not one of ``vals``."""
        return Filter._leaf(key, "nin", vals=vals)

    @staticmethod
    def exists(key: str) -> FilterNode:
        """Tag ``key`` exists."""
        return Filter._leaf(key, "ex")

    @staticmethod
    def not_exists(key: str) -> FilterNode:
        """Tag ``key`` does not exist."""
        return Filter._leaf(key, "nex")

    @staticmethod
    def starts_with(key: str, val: str) -> FilterNode:
        """String tag ``key`` starts with ``val``."""
        return Filter._leaf(key, "sw", val=val)

    @staticmethod
    def ends_with(key: str, val: str) -> FilterNode:
        """String tag ``key`` ends with ``val``."""
        return Filter._leaf(key, "ew", val=val)

    @staticmethod
    def contains(key: str, val: str) -> FilterNode:
        """String tag ``key`` contains ``val``."""
        return Filter._leaf(key, "ct", val=val)

    @staticmethod
    def gt(key: str, val: str) -> FilterNode:
        """Numeric tag ``key`` is greater than ``val``."""
        return Filter._leaf(key, "gt", val=val)

    @staticmethod
    def gte(key: str, val: str) -> FilterNode:
        """Numeric tag ``key`` is greater than or equal to ``val``."""
        return Filter._leaf(key, "gte", val=val)

    @staticmethod
    def lt(key: str, val: str) -> FilterNode:
        """Numeric tag ``key`` is less than ``val``."""
        return Filter._leaf(key, "lt", val=val)

    @staticmethod
    def lte(key: str, val: str) -> FilterNode:
        """Numeric tag ``key`` is less than or equal to ``val``."""
        return Filter._leaf(key, "lte", val=val)

    @staticmethod
    def all(*nodes: FilterNode) -> FilterNode:
        """All ``nodes`` must match (logical AND)."""
        return Filter._logical("and", list(nodes))

    @staticmethod
    def any(*nodes: FilterNode) -> FilterNode:
        """At least one of ``nodes`` must match (logical OR)."""
        return Filter._logical("or", list(nodes))

    @staticmethod
    def negate(node: FilterNode) -> FilterNode:
        """Inverts ``node`` (logical NOT)."""
        return Filter._logical("not", [node])
