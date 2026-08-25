"""RSS and Atom into items, with no I/O (FR-I1).

Pure: bytes in, items out. That is what lets the fiddly half of discovery — two formats, a
dozen date spellings, publishers who emit invalid XML — be tested against a fixture file with
no network and no database.

This module records what the feed said and judges none of it. Canonicalising the URL, stripping
the HTML, detecting the language and hashing the content all belong to the normalize stage,
which reads the record rather than the feed.
"""

import calendar
import datetime as dt
from dataclasses import dataclass

import feedparser


@dataclass(frozen=True)
class FeedItem:
    """One article a feed claims exists.

    ``summary`` and ``content`` are deliberately separate and must not be merged. A
    ``<description>``/``<summary>`` is a teaser — measured across the v1 roster at a median of
    120 characters against ~3,900 in the article — while ``content:encoded``/``<content>`` is
    the article itself, on the rare feed that ships one. Treating the first as a body puts
    teasers in ``body_text``, where every later stage reads them as articles.
    """

    guid: str
    link: str
    title: str
    published_at: dt.datetime | None
    #: The teaser, raw. ``None`` where the feed offers none of its own.
    summary: str | None
    #: The full article body, raw. ``None`` on all but a tier-1 feed.
    content: str | None


@dataclass(frozen=True)
class ParsedFeed:
    items: tuple[FeedItem, ...]
    #: Entries dropped for having no usable title or link. A feed where this is not zero is
    #: either malformed or being read wrongly, and either way the count is the only thing that
    #: would say so.
    skipped: int


class FeedUnreadable(Exception):
    """The bytes could not be read as a feed at all."""


def _published(entry: feedparser.util.FeedParserDict) -> dt.datetime | None:
    """The item's timestamp, as an aware UTC datetime.

    feedparser normalises every date spelling it recognises to UTC ``struct_time``; anything it
    does not recognise is simply absent, which is why this is nullable rather than defaulted.
    Falling back to ``updated`` matters for Atom, where ``published`` is optional.
    """
    for field in ("published_parsed", "updated_parsed"):
        parsed = entry.get(field)
        if parsed is not None:
            return dt.datetime.fromtimestamp(calendar.timegm(parsed), tz=dt.UTC)
    return None


def _texts(entry: feedparser.util.FeedParserDict) -> tuple[str | None, str | None]:
    """The teaser and the body, kept apart.

    ⚠️ feedparser copies ``content`` into ``summary`` when an entry has content and no summary
    of its own — verified, not assumed. Read naively that yields a "teaser" holding the entire
    article, which is the one way this module could poison ``lede`` system-wide. When the two
    are identical the feed offered no teaser, and saying so is more useful than repeating the
    body into a second column.
    """
    contents = entry.get("content") or []
    content = contents[0].get("value") if contents else None
    summary = entry.get("summary") or None
    if content is not None and summary == content:
        summary = None
    return summary, content or None


def _is_http(link: str) -> bool:
    return link.startswith(("http://", "https://"))


def parse(raw: bytes) -> ParsedFeed:
    """Read a feed body. Raises ``FeedUnreadable`` if the bytes are not a feed.

    A feed that is malformed but still yields entries is used as-is. feedparser is deliberately
    lenient and real publishers are routinely slightly invalid — refusing those would drop
    working sources to no benefit.

    ⚠️ The test for "is this a feed at all" is ``version``, not ``bozo``. An HTML error page
    served with status 200 — a feed URL that has rotted into a soft 404, which is live on the
    v1 roster — parses with ``bozo`` unset, no entries and an empty ``version``. Trusting
    ``bozo`` reports that as an empty feed, so the poll is recorded as healthy and discovery
    quietly finds nothing from that publisher for as long as nobody looks. ``version`` is the
    format feedparser actually recognised, and is empty exactly when it recognised none.
    """
    parsed = feedparser.parse(raw)
    entries = parsed.get("entries") or []
    if not parsed.get("version"):
        raise FeedUnreadable(str(parsed.get("bozo_exception") or "not a feed"))
    if not entries:
        return ParsedFeed(items=(), skipped=0)

    items: list[FeedItem] = []
    skipped = 0
    for entry in entries:
        link = (entry.get("link") or "").strip()
        title = (entry.get("title") or "").strip()
        if not title or not _is_http(link):
            # Both are NOT NULL on the record, and an item with nowhere fetchable to point is
            # not an article any later stage can do anything with.
            #
            # ⚠️ The scheme check is not cosmetic. An RSS <guid> is a permalink unless the feed
            # says otherwise, so feedparser fills a missing <link> from an opaque id like
            # "urn:x:1" — which would then be stored as the article's URL and fetched later.
            skipped += 1
            continue
        summary, content = _texts(entry)
        items.append(
            FeedItem(
                # RSS <guid> and Atom <id> both normalise to `id`. Falling back to the link is
                # what the RSS spec itself suggests, and a feed with neither is rare enough
                # that inventing an identity would create duplicates rather than prevent them.
                guid=(entry.get("id") or link).strip(),
                link=link,
                title=title,
                published_at=_published(entry),
                summary=summary,
                content=content,
            )
        )
    return ParsedFeed(items=tuple(items), skipped=skipped)
