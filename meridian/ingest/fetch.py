"""Fetching one feed over HTTP (FR-I1, FR-I3).

The only part of discovery that touches the network, which is what makes it the only part a
test has to replace. Everything above it takes a ``Fetcher`` as an argument.

Failures are returned, not raised. A feed that times out is an ordinary event on a roster of
real publishers, and the caller's requirement is to record it and poll the next feed — an
exception per dead feed would make the ordinary path the exceptional one.
"""

import datetime as dt
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol

import httpx2

log = logging.getLogger(__name__)

#: No contact URL, deliberately. Measured against a live publisher: the same request with
#: "(+https://…)" appended to this string went from a 200 in 0.8 s to a read timeout at 25 s,
#: reproduced three times. The most widely recommended politeness convention reads as a bot
#: signature to some edge filters, and it fails as a hang rather than a refusal — so it costs
#: the whole timeout budget and looks like an outage. Per-publisher overrides live on the
#: registry, because the value that works is found by being burned.
DEFAULT_USER_AGENT = "Meridian/0.1"

#: A feed is tens to hundreds of kilobytes. This is not a tuning knob, it is a refusal to read
#: an unbounded body from a host we do not control into a process that holds a database
#: connection.
MAX_BYTES = 5 * 1024 * 1024

DEFAULT_TIMEOUT = dt.timedelta(seconds=15)


@dataclass(frozen=True)
class FetchResult:
    """What one request produced.

    ``status`` is None when there was no response at all — DNS, connection refused, timeout —
    and ``error`` says which. A 304 carries no body by definition, so ``body`` is None there
    too; the two are told apart by the status, never by the body being empty.
    """

    status: int | None
    body: bytes | None = None
    etag: str | None = None
    last_modified: str | None = None
    error: str | None = None

    @property
    def not_modified(self) -> bool:
        return self.status == 304


class Fetcher(Protocol):
    """What discovery needs from the network. Implemented for real by ``HttpFetcher``."""

    def __call__(self, url: str, *, user_agent: str, headers: Mapping[str, str]) -> FetchResult: ...


class TooLarge(Exception):
    pass


class HttpFetcher:
    """A real HTTP client, reused across a cycle so connections are pooled.

    ``follow_redirects`` is on: feed URLs move, and a publisher answering 301 to its own new
    path is the ordinary case rather than a misconfiguration.
    """

    def __init__(
        self,
        *,
        timeout: dt.timedelta = DEFAULT_TIMEOUT,
        max_bytes: int = MAX_BYTES,
    ) -> None:
        self._client = httpx2.Client(
            timeout=timeout.total_seconds(),
            follow_redirects=True,
        )
        self._max_bytes = max_bytes

    def __call__(self, url: str, *, user_agent: str, headers: Mapping[str, str]) -> FetchResult:
        request_headers = {"User-Agent": user_agent, "Accept-Encoding": "gzip", **headers}
        try:
            with self._client.stream("GET", url, headers=request_headers) as response:
                if response.status_code == 304:
                    return FetchResult(status=304)
                body = self._read(response)
        except TooLarge:
            log.warning("feed at %s exceeded %d bytes; not read", url, self._max_bytes)
            return FetchResult(status=None, error=f"body exceeded {self._max_bytes} bytes")
        except httpx2.HTTPError as exc:
            # Includes timeouts, connection failures and protocol errors. The class name is
            # carried because "ReadTimeout" and "ConnectError" call for different responses
            # from a human and the message alone often does not distinguish them.
            return FetchResult(status=None, error=f"{type(exc).__name__}: {exc}")

        if response.status_code != 200:
            return FetchResult(status=response.status_code, error=response.reason_phrase or None)
        return FetchResult(
            status=200,
            body=body,
            etag=response.headers.get("ETag"),
            last_modified=response.headers.get("Last-Modified"),
        )

    def _read(self, response: httpx2.Response) -> bytes:
        """Read the body, refusing to grow past the cap.

        Streamed rather than read whole: ``Content-Length`` is a claim by the same host that is
        sending the body, so checking it and then reading anyway bounds nothing.
        """
        chunks: list[bytes] = []
        size = 0
        for chunk in response.iter_bytes():
            size += len(chunk)
            if size > self._max_bytes:
                raise TooLarge
            chunks.append(chunk)
        return b"".join(chunks)

    def close(self) -> None:
        self._client.close()
