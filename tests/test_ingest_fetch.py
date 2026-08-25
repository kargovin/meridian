"""Bounding one request (FR-I3, RFC §7.1). No network — the read loop is driven directly."""

import datetime as dt
from collections.abc import Iterator
from typing import Any

import pytest

from meridian.ingest.fetch import DEFAULT_USER_AGENT, HttpFetcher, TooLarge, TooSlow


class DribblingResponse:
    """A publisher that sends one byte at a time and never stops.

    ⚠️ This is what the client's own timeouts do not catch. ``read`` bounds the wait for the
    *next* chunk, so a sender that produces something inside every window keeps the connection
    for as long as it likes without ever tripping it.
    """

    def __init__(self, chunks: int = 10_000) -> None:
        self._chunks = chunks

    def iter_bytes(self) -> Iterator[bytes]:
        for _ in range(self._chunks):
            yield b"x"


class Ticker:
    """A clock that advances a fixed amount per read, standing in for elapsed time."""

    def __init__(self, step: float = 1.0) -> None:
        self.now = 0.0
        self.step = step

    def __call__(self) -> float:
        self.now += self.step
        return self.now


def _fetcher(**kw: Any) -> HttpFetcher:
    return HttpFetcher(**kw)


def test_a_dribbling_publisher_is_abandoned_at_the_deadline() -> None:
    """Left unbounded it costs a slot in a sequential cycle, and the cycle is the budget."""
    fetcher = _fetcher(max_duration=dt.timedelta(seconds=10), clock=Ticker(step=1.0))

    with pytest.raises(TooSlow):
        fetcher._read(DribblingResponse(), deadline=10.0)

    fetcher.close()


def test_a_body_over_the_cap_is_refused() -> None:
    fetcher = _fetcher(max_bytes=100, clock=Ticker(step=0.0))

    with pytest.raises(TooLarge):
        fetcher._read(DribblingResponse(chunks=500), deadline=1e9)

    fetcher.close()


def test_a_body_inside_both_bounds_is_returned() -> None:
    fetcher = _fetcher(max_bytes=1000, clock=Ticker(step=0.0))

    body = fetcher._read(DribblingResponse(chunks=50), deadline=1e9)

    assert body == b"x" * 50
    fetcher.close()


def test_the_default_user_agent_carries_no_contact_url() -> None:
    """Measured: appending a contact URL turned a 200 in 0.8 s into a read timeout at 25 s."""
    assert "http" not in DEFAULT_USER_AGENT
    assert "@" not in DEFAULT_USER_AGENT
