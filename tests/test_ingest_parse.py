"""Reading RSS and Atom (FR-I1). No network, no database — the parser is pure."""

import datetime as dt

import pytest

from meridian.ingest.parse import FeedUnreadable, parse

RSS = b"""<?xml version="1.0"?>
<rss version="2.0" xmlns:content="http://purl.org/rss/1.0/modules/content/">
<channel><title>Example</title>
  <item>
    <title>Floods displace thousands</title>
    <link>https://x.example/1?utm_source=rss</link>
    <guid>urn:x:1</guid>
    <pubDate>Tue, 25 Aug 2026 09:14:02 GMT</pubDate>
    <description>Heavy rain overnight has forced evacuations.</description>
  </item>
</channel></rss>"""

ATOM = b"""<?xml version="1.0"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>Example</title>
  <entry>
    <id>urn:a:1</id>
    <title>Floods displace thousands</title>
    <link href="https://x.example/1"/>
    <published>2026-08-25T09:14:02Z</published>
    <summary>Heavy rain overnight.</summary>
  </entry>
</feed>"""


def test_rss_yields_the_four_fields_discovery_needs() -> None:
    item = parse(RSS).items[0]
    assert item.guid == "urn:x:1"
    assert item.link == "https://x.example/1"  # canonical: the tracking parameter is gone
    assert item.title == "Floods displace thousands"
    assert item.published_at == dt.datetime(2026, 8, 25, 9, 14, 2, tzinfo=dt.UTC)


def test_atom_yields_the_same_shape_from_different_tags() -> None:
    item = parse(ATOM).items[0]
    assert item.guid == "urn:a:1"
    assert item.link == "https://x.example/1"
    assert item.published_at == dt.datetime(2026, 8, 25, 9, 14, 2, tzinfo=dt.UTC)


def test_the_teaser_is_read_as_a_summary_not_as_a_body() -> None:
    item = parse(RSS).items[0]
    assert item.summary == "Heavy rain overnight has forced evacuations."
    assert item.content is None


def test_a_feed_carrying_both_keeps_them_apart() -> None:
    raw = RSS.replace(
        b"</description>",
        b"</description><content:encoded><![CDATA[<p>THE WHOLE ARTICLE</p>]]></content:encoded>",
    )
    item = parse(raw).items[0]
    assert item.summary == "Heavy rain overnight has forced evacuations."
    assert item.content is not None
    assert "THE WHOLE ARTICLE" in item.content


def test_content_mirrored_into_summary_is_not_reported_as_a_teaser() -> None:
    """⚠️ feedparser copies content into summary when an entry has no summary of its own.

    Read naively that yields a "teaser" holding the entire article, and every such article
    would carry its whole body in ``lede``. The falsifier is the assertion on ``summary``:
    delete the identity check in ``parse._texts`` and it holds the body instead of None.
    """
    raw = ATOM.replace(
        b"<summary>Heavy rain overnight.</summary>",
        b'<content type="html">THE WHOLE ARTICLE, thousands of words</content>',
    )
    item = parse(raw).items[0]
    assert item.content == "THE WHOLE ARTICLE, thousands of words"
    assert item.summary is None


def test_an_item_with_no_guid_falls_back_to_its_canonical_link() -> None:
    """Canonical, not raw. Otherwise a per-feed tracking parameter defeats the guid uniqueness
    constraint as well as the URL one, and one article lands twice under both.
    """
    raw = RSS.replace(b"<guid>urn:x:1</guid>", b"")
    item = parse(raw).items[0]
    assert item.guid == "https://x.example/1"


def test_an_undated_item_is_kept_with_no_timestamp() -> None:
    raw = RSS.replace(b"<pubDate>Tue, 25 Aug 2026 09:14:02 GMT</pubDate>", b"")
    assert parse(raw).items[0].published_at is None


def test_atom_falls_back_to_updated_when_there_is_no_published() -> None:
    raw = ATOM.replace(
        b"<published>2026-08-25T09:14:02Z</published>",
        b"<updated>2026-08-25T11:30:00Z</updated>",
    )
    assert parse(raw).items[0].published_at == dt.datetime(2026, 8, 25, 11, 30, tzinfo=dt.UTC)


def test_an_item_with_no_title_is_skipped_and_counted() -> None:
    """The column is NOT NULL, and a headline is the one thing every reader surface shows."""
    parsed = parse(RSS.replace(b"<title>Floods displace thousands</title>", b""))
    assert parsed.items == ()
    assert parsed.skipped == 1


def test_an_opaque_guid_is_not_accepted_as_the_articles_url() -> None:
    """⚠️ An RSS guid is a permalink unless the feed says otherwise, so feedparser fills a
    missing <link> from it. Without the scheme check "urn:x:1" is stored as the article's URL
    and something tries to fetch it later.
    """
    parsed = parse(RSS.replace(b"<link>https://x.example/1?utm_source=rss</link>", b""))
    assert parsed.items == ()
    assert parsed.skipped == 1


def test_a_slightly_invalid_feed_that_still_yields_entries_is_used() -> None:
    """Real publishers emit invalid XML routinely; refusing it drops working sources."""
    raw = RSS.replace(b"<title>Example</title>", b"<title>Example & Co</title>")
    assert len(parse(raw).items) == 1


def test_an_html_error_page_served_as_200_is_refused_not_read_as_empty() -> None:
    """⚠️ feedparser reports this with bozo unset and no entries, so a bozo-based check calls
    it an empty feed and the poll is recorded as healthy. A rotted feed URL then discovers
    nothing, indefinitely, with nothing anywhere saying so.
    """
    with pytest.raises(FeedUnreadable):
        parse(b"<html><body>404 Not Found</body></html>")


def test_bytes_that_are_not_markup_at_all_are_refused() -> None:
    with pytest.raises(FeedUnreadable):
        parse(b"Not Found")


def test_an_empty_but_valid_feed_is_not_an_error() -> None:
    raw = b'<?xml version="1.0"?><rss version="2.0"><channel><title>E</title></channel></rss>'
    assert parse(raw).items == ()
