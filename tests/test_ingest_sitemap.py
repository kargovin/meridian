"""Reading Google News sitemaps (FR-I1). No network, no database — the parser is pure.

The fixtures are real captures, trimmed, each naming its URL and date in a comment. NPR's is
served from ``googlecrawl.npr.org``, not the publisher's home host, and lists member stations'
own articles beside NPR's.
"""

import datetime as dt
from pathlib import Path
from xml.sax.saxutils import escape

import pytest

from meridian.ingest.parse import FeedUnreadable
from meridian.ingest.sitemap import parse_sitemap

FIXTURES = Path(__file__).parent / "fixtures"

NEWS_NS = (
    'xmlns="http://www.sitemaps.org/schemas/sitemap/0.9" '
    'xmlns:news="http://www.google.com/schemas/sitemap-news/0.9"'
)


def sitemap(*entries: str) -> bytes:
    return f'<?xml version="1.0"?><urlset {NEWS_NS}>{"".join(entries)}</urlset>'.encode()


def entry(
    loc: str = "https://x.example/a",
    title: str | None = "Floods displace thousands",
    date: str | None = "2026-10-01T07:50:02-04:00",
) -> str:
    news = "<news:publication><news:name>X</news:name><news:language>en</news:language>"
    news += "</news:publication>"
    if date is not None:
        news += f"<news:publication_date>{date}</news:publication_date>"
    if title is not None:
        news += f"<news:title>{title}</news:title>"
    return f"<url><loc>{escape(loc)}</loc><news:news>{news}</news:news></url>"


def fixture(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


# --------------------------------------------------------------------------- real captures


def test_global_voices_reads_every_entry_with_its_title_link_and_date() -> None:
    parsed = parse_sitemap(fixture("global_voices_sitemap_news.xml"))

    assert parsed.skipped == 0
    assert [item.title for item in parsed.items] == [
        "West-to-east energy, east-to-west computing: "
        "The Uyghur costs of China\u2019s digital order",
        "In Brazil\u2019s capital, residents grow their own heat-fighting solutions",
        "Who can speak on behalf of the people? The new frontiers of protest in the Sahel",
    ]
    first = parsed.items[0]
    assert first.link == (
        "https://globalvoices.org/2026/10/01/"
        "west-to-east-energy-east-to-west-computing-the-uyghur-costs-of-chinas-digital-order/"
    )
    assert first.published_at == dt.datetime(2026, 10, 1, 7, 34, 18, tzinfo=dt.UTC)


def test_npr_served_from_another_host_is_read_whole_including_station_articles() -> None:
    """The parser records what the sitemap lists. Whose articles they are is discovery's call,
    made against the registry — the parser has no publisher to compare with."""
    parsed = parse_sitemap(fixture("npr_sitemap_news.xml"))

    assert len(parsed.items) == 5
    assert parsed.items[0].link.startswith("https://www.wshu.org/")
    # -04:00 normalised to UTC.
    assert parsed.items[1].published_at == dt.datetime(2026, 10, 1, 11, 37, 10, tzinfo=dt.UTC)
    assert parsed.items[1].title == (
        "Passengers recount chaos on Tel Aviv flight. And, FBI probes employee-data hack"
    )


def test_the_conversation_reads_a_zulu_date() -> None:
    parsed = parse_sitemap(fixture("the_conversation_ca_sitemap_news.xml"))

    assert len(parsed.items) == 3
    assert parsed.items[0].published_at == dt.datetime(2026, 9, 30, 19, 37, 12, tzinfo=dt.UTC)


def test_a_sitemap_item_has_no_teaser_and_no_body() -> None:
    """FeedItem's two text fields stay empty: a sitemap has neither, and filling either from
    the title would put a headline where every later stage expects a teaser or an article."""
    for item in parse_sitemap(fixture("global_voices_sitemap_news.xml")).items:
        assert (item.summary, item.content, item.content_type) == (None, None, None)


# --------------------------------------------------------------------------- identity


def test_the_guid_is_the_canonical_link() -> None:
    parsed = parse_sitemap(sitemap(entry(loc="https://X.example/a?utm_source=gn&b=1#top")))

    (item,) = parsed.items
    assert item.link == "https://x.example/a?b=1"
    assert item.guid == item.link


def test_a_link_is_canonicalised_exactly_as_an_rss_link_is() -> None:
    from meridian.ingest.urls import canonicalize

    raw = "https://Www.Example.org:443/story/?traffic_source=rss&z=2&a=1"
    (item,) = parse_sitemap(sitemap(entry(loc=raw))).items
    assert item.link == canonicalize(raw)


# --------------------------------------------------------------------------- dates


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("2026-10-01T07:50:02-04:00", dt.datetime(2026, 10, 1, 11, 50, 2, tzinfo=dt.UTC)),
        ("2026-09-30T19:37:12Z", dt.datetime(2026, 9, 30, 19, 37, 12, tzinfo=dt.UTC)),
        ("2026-09-30T19:37+01:00", dt.datetime(2026, 9, 30, 18, 37, tzinfo=dt.UTC)),
        ("2026-09-30T19:37:12.250Z", dt.datetime(2026, 9, 30, 19, 37, 12, 250000, tzinfo=dt.UTC)),
        ("2026-09-30", dt.datetime(2026, 9, 30, tzinfo=dt.UTC)),
        ("2026-09-30T19:37:12", dt.datetime(2026, 9, 30, 19, 37, 12, tzinfo=dt.UTC)),
        ("Tue, 30 Sep 2026 19:37:12 GMT", None),
        ("", None),
    ],
    ids=["offset", "zulu", "no seconds", "fraction", "date only", "no zone", "rfc822", "empty"],
)
def test_publication_dates(raw: str, expected: dt.datetime | None) -> None:
    (item,) = parse_sitemap(sitemap(entry(date=raw))).items
    assert item.published_at == expected
    assert item.published_at is None or item.published_at.tzinfo is dt.UTC


def test_a_missing_publication_date_is_none() -> None:
    (item,) = parse_sitemap(sitemap(entry(date=None))).items
    assert item.published_at is None


# --------------------------------------------------------------------------- unusable entries


def test_entries_without_a_title_or_an_http_link_are_skipped_and_counted() -> None:
    parsed = parse_sitemap(
        sitemap(
            entry(loc="https://x.example/kept"),
            entry(loc="https://x.example/untitled", title=None),
            entry(loc="https://x.example/blank", title="   "),
            entry(loc="ftp://x.example/file"),
            entry(loc=""),
        )
    )
    assert [item.link for item in parsed.items] == ["https://x.example/kept"]
    assert parsed.skipped == 4


def test_a_url_entry_without_news_markup_in_a_news_sitemap_is_skipped() -> None:
    parsed = parse_sitemap(sitemap(entry(), "<url><loc>https://x.example/section/</loc></url>"))
    assert len(parsed.items) == 1
    assert parsed.skipped == 1


def test_an_empty_urlset_is_an_empty_window_not_an_error() -> None:
    """The Conversation's global edition served exactly this on 1 Oct 2026."""
    parsed = parse_sitemap(sitemap())
    assert (parsed.items, parsed.skipped) == ((), 0)


def test_a_title_keeps_its_escaped_characters_resolved() -> None:
    (item,) = parse_sitemap(sitemap(entry(title="Q&amp;A: &#8216;Doomsday&#8217;"))).items
    assert item.title == "Q&A: \u2018Doomsday\u2019"


# --------------------------------------------------------------------------- refusals


def test_a_sitemap_index_is_refused_with_a_message_saying_what_to_register() -> None:
    with pytest.raises(FeedUnreadable, match=r"sitemap index of 2 sitemap\(s\).*register a child"):
        parse_sitemap(fixture("bbc_news_sitemap_index.xml"))


def test_a_plain_sitemap_is_refused_because_it_has_no_titles() -> None:
    plain = sitemap(
        "<url><loc>https://x.example/a</loc><lastmod>2026-10-01</lastmod></url>",
        "<url><loc>https://x.example/b</loc></url>",
    )
    with pytest.raises(FeedUnreadable, match=r"plain sitemap: none of its 2 entries"):
        parse_sitemap(plain)


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        (b"<!doctype html><html><body>Not found</body></html>", "not XML"),
        (b"<html><body>Not found</body></html>", r"root element is <html>"),
        (
            b'<?xml version="1.0"?><rss version="2.0"><channel></channel></rss>',
            r"root element is <rss>",
        ),
        (b"", "not XML"),
        (b"<urlset", "not XML"),
    ],
    ids=["html doctype", "html", "rss", "empty", "truncated"],
)
def test_what_is_not_a_sitemap_is_unreadable(raw: bytes, message: str) -> None:
    with pytest.raises(FeedUnreadable, match=message):
        parse_sitemap(raw)


BILLION_LAUGHS = b"""<?xml version="1.0"?>
<!DOCTYPE urlset [
  <!ENTITY a "aaaaaaaaaa">
  <!ENTITY b "&a;&a;&a;&a;&a;&a;&a;&a;&a;&a;">
  <!ENTITY c "&b;&b;&b;&b;&b;&b;&b;&b;&b;&b;">
]>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"
        xmlns:news="http://www.google.com/schemas/sitemap-news/0.9">
<url><loc>https://x.example/a</loc><news:news><news:title>&c;</news:title></news:news></url>
</urlset>"""

EXTERNAL_ENTITY = b"""<?xml version="1.0"?>
<!DOCTYPE urlset [<!ENTITY secret SYSTEM "file:///etc/hostname">]>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"
        xmlns:news="http://www.google.com/schemas/sitemap-news/0.9">
<url><loc>https://x.example/a</loc><news:news><news:title>&secret;</news:title></news:news></url>
</urlset>"""


@pytest.mark.parametrize("raw", [BILLION_LAUGHS, EXTERNAL_ENTITY], ids=["expansion", "external"])
def test_a_document_declaring_a_doctype_is_refused(raw: bytes) -> None:
    """Untrusted XML from a host we do not control. An internal subset is where an expansion
    bomb or an external entity lives, and a sitemap has no use for one."""
    with pytest.raises(FeedUnreadable, match="DOCTYPE"):
        parse_sitemap(raw)


@pytest.mark.parametrize("loc", ["https://x.example:80a/b", "http://[x/a"], ids=["port", "ipv6"])
def test_a_loc_urlsplit_cannot_read_is_skipped_not_raised(loc: str) -> None:
    """Raising would fail the whole poll, losing every other entry, every cycle the entry stays
    listed — about 48 hours for a news sitemap."""
    parsed = parse_sitemap(sitemap(entry(loc=loc), entry(loc="https://x.example/kept")))

    assert [item.link for item in parsed.items] == ["https://x.example/kept"]
    assert parsed.skipped == 1


def test_an_upper_case_scheme_is_read() -> None:
    (item,) = parse_sitemap(sitemap(entry(loc="HTTPS://X.example/a"))).items
    assert item.link == "https://x.example/a"
