"""Google News sitemaps into items, with no I/O (FR-I1).

The second discovery route beside RSS and Atom, and the same shape out: bytes in, the
``ParsedFeed`` that ``parse`` returns, so everything after the parse cannot tell the routes
apart. A news sitemap (``<urlset>`` with a ``<news:news>`` per ``<url>``) lists what a publisher
put out in roughly the last 48 hours, where a feed is a window of its newest N items — so it is
also how discovery catches up on what scrolled out of a feed while it was not polling.

What a sitemap does not carry, and what that means downstream:

- **No guid.** The guid is the canonical URL, as it already is for an RSS item without one.
- **No teaser.** ``summary`` is ``None``, so a record found only here has a NULL lede, and FR-I7
  decides its language on the title alone.
- **No body.** ``content`` is ``None`` whatever the feed's tier says.

``<news:language>`` is the publisher's declaration and is not read: FR-I7 computes the
language, and a declared one would be a second answer that can disagree with it.

⚠️ The XML is untrusted and is parsed with entity expansion, DTD loading and network access
all off, and a document that declares a DOCTYPE is refused outright — a sitemap has no use for
one, and an internal subset is where an entity-expansion bomb lives. The size is already
bounded by the fetcher (``MAX_BYTES``); a news sitemap holds at most 1,000 URLs, well inside it.
"""

import datetime as dt

from lxml import etree

from meridian.ingest.parse import FeedItem, FeedUnreadable, ParsedFeed
from meridian.ingest.urls import canonicalize


def _local(element: etree._Element) -> str:
    """The element's name without its namespace.

    Matched by local name because publishers do not agree on the namespace URI's spelling, and
    nothing else in a sitemap shares these names.
    """
    return str(etree.QName(element).localname)


def _child(element: etree._Element, name: str) -> etree._Element | None:
    for child in element:
        if _local(child) == name:
            return child
    return None


def _text(element: etree._Element | None) -> str:
    return "".join(element.itertext()).strip() if element is not None else ""


def _published(raw: str) -> dt.datetime | None:
    """``<news:publication_date>`` as an aware UTC datetime, or ``None`` if unreadable.

    The format is W3C Datetime: a date alone, or a date and time with a zone
    (``2026-10-01T07:50:02-04:00``, ``2026-09-30T19:37:12Z``). A date alone, or a time with no
    zone — which the format does not allow but a publisher may still write — is read as UTC.
    """
    if not raw:
        return None
    try:
        value = dt.datetime.fromisoformat(raw)
    except ValueError:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=dt.UTC)
    return value.astimezone(dt.UTC)


def _is_http(link: str) -> bool:
    return link.startswith(("http://", "https://"))


def parse_sitemap(raw: bytes) -> ParsedFeed:
    """Read a Google News sitemap. Raises ``FeedUnreadable`` if the bytes are not one.

    Three refusals, each with a message the admin surface shows as the feed's poll error:

    - **A sitemap index** (``<sitemapindex>``) lists other sitemaps, not articles. It is not
      followed: the registry names the sitemap to poll, and a child URL is registered directly.
    - **A plain sitemap** — ``<url>`` entries and not one ``<news:news>`` — has no titles, and a
      title is NOT NULL on the record. Finding them would mean fetching every page.
    - **Not a sitemap at all** — an HTML error page served with 200, a DOCTYPE, broken XML.

    An empty ``<urlset>`` is a sitemap with nothing in its window, which is not an error.

    In a news sitemap, an entry without ``<news:news>``, without a title, or without an http(s)
    ``<loc>`` is skipped and counted, as an unusable RSS item is.
    """
    parser = etree.XMLParser(
        resolve_entities=False,
        load_dtd=False,
        no_network=True,
        huge_tree=False,
        remove_comments=True,
        remove_pis=True,
    )
    try:
        root = etree.fromstring(raw, parser=parser)
    except etree.XMLSyntaxError as exc:
        raise FeedUnreadable(f"not XML: {exc}") from exc
    if root is None:
        raise FeedUnreadable("not XML: empty document")
    if root.getroottree().docinfo.doctype:
        raise FeedUnreadable("declares a DOCTYPE, which no sitemap needs; not read")

    kind = _local(root)
    if kind == "sitemapindex":
        raise FeedUnreadable(
            f"a sitemap index of {len(root)} sitemap(s), not a sitemap; register a child"
        )
    if kind != "urlset":
        raise FeedUnreadable(f"not a sitemap: the root element is <{kind}>")

    # Every child is an element: comments and processing instructions are dropped by the parser,
    # and entity references cannot occur without the DOCTYPE refused above.
    entries = [child for child in root if _local(child) == "url"]
    news = [(entry, _child(entry, "news")) for entry in entries]
    if entries and all(element is None for _, element in news):
        raise FeedUnreadable(
            f"a plain sitemap: none of its {len(entries)} entries has <news:news>, so none has "
            "a title; only news sitemaps are read"
        )

    items: list[FeedItem] = []
    skipped = 0
    for entry, element in news:
        link = _text(_child(entry, "loc"))
        title = _text(_child(element, "title")) if element is not None else ""
        if not title or not _is_http(link):
            skipped += 1
            continue
        link = canonicalize(link)
        items.append(
            FeedItem(
                guid=link,
                link=link,
                title=title,
                published_at=_published(_text(_child(element, "publication_date"))),
                summary=None,
                content=None,
                content_type=None,
            )
        )
    return ParsedFeed(items=tuple(items), skipped=skipped)
