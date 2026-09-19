"""Body extraction: what comes out of a page, and what counts as nothing.

Not covered here: the ``url`` handed to trafilatura. It feeds link and date heuristics that
no assertion on the returned text can see, so dropping it fails nothing in this file.
"""

from meridian.ingest.extract import MIN_BODY_CHARS, extract

URL = "https://example.org/news/one"

_PARAS = [
    "The council approved the plan on Tuesday after a debate that ran for three hours.",
    "Opponents said the cost had been understated and asked for an independent review.",
    "The first phase is expected to begin next spring, subject to funding.",
]


def _page(*, comments: str = "", table: str = "") -> bytes:
    """A realistic page: navigation, a cookie notice, the article, related links, a footer."""
    body = "".join(f"<p>{p}</p>" for p in _PARAS)
    nav = "".join(f'<li><a href="/{x.lower()}">{x}</a></li>' for x in ("Home", "News", "Sport"))
    related = (
        '<li><a href="/news/two">Council budget shortfall grows</a></li>'
        '<li><a href="/news/three">Mayor defends spending</a></li>'
    )
    return (
        '<!doctype html><html><head><meta charset="utf-8"><title>Council plan</title></head>'
        f"<body><nav><ul>{nav}</ul></nav>"
        '<div class="cookie-notice">We use cookies to improve your experience. '
        "Accept all cookies.</div>"
        f"<main><article><h1>Council approves plan</h1>{body}{table}</article>{comments}</main>"
        f"<aside><h2>Related</h2><ul>{related}</ul></aside>"
        "<footer>Copyright 2026 Example Media. All rights reserved. Privacy policy. "
        "Terms of use.</footer></body></html>"
    ).encode()


def test_the_article_comes_out_and_the_furniture_does_not() -> None:
    text = extract(_page(), URL)

    assert text is not None
    for para in _PARAS:
        assert para in text
    for furniture in ("Accept all cookies", "Privacy policy", "Council budget shortfall", "Sport"):
        assert furniture not in text


def test_a_comment_section_is_left_out() -> None:
    comments = (
        '<section id="comments" class="comments"><h2>Comments</h2>'
        '<div class="comment"><p>This is a terrible idea and the council knows it.</p></div>'
        '<div class="comment"><p>About time something was done about the junction.</p></div>'
        "</section>"
    )
    text = extract(_page(comments=comments), URL)

    assert text is not None
    assert "terrible idea" not in text
    assert "About time" not in text


def test_a_table_is_left_out() -> None:
    table = (
        "<table><tr><th>Phase</th><th>Cost</th></tr>"
        "<tr><td>One</td><td>4.2 million</td></tr>"
        "<tr><td>Two</td><td>6.8 million</td></tr></table>"
    )
    text = extract(_page(table=table), URL)

    assert text is not None
    assert "4.2 million" not in text


def test_a_page_with_no_article_is_none() -> None:
    """A JavaScript shell, a feed served as HTML, nothing at all.

    Also the falsifier for ``favor_precision``: without it the shell's ``<noscript>`` line
    comes back as a 25-character body, above the fragment floor.
    """
    shell = (
        b"<html><body><app-root></app-root>"
        b"<noscript>Please enable JavaScript.</noscript></body></html>"
    )
    feed = b"<rss><channel><item><title>Headline</title><link>https://x/1</link></item></channel></rss>"

    assert extract(shell, URL) is None
    assert extract(feed, URL) is None
    assert extract(b"", URL) is None


def test_a_fragment_is_none() -> None:
    """Degenerate markup yields a fragment rather than None; the floor turns it into None."""
    assert extract(b"<html><body><p>x</p></body></html>", URL) is None
    assert extract(b"<html><body><div class='loader'>Loading...</div></body></html>", URL) is None


def test_the_floor_is_inclusive_of_min_body_chars() -> None:
    at = "a" * (MIN_BODY_CHARS - 1) + "."
    below = "a" * (MIN_BODY_CHARS - 2) + "."
    assert len(at) == MIN_BODY_CHARS
    assert len(below) == MIN_BODY_CHARS - 1

    assert extract(f"<html><body><p>{at}</p></body></html>".encode(), URL) == at
    assert extract(f"<html><body><p>{below}</p></body></html>".encode(), URL) is None


def test_a_bare_html_fragment_works() -> None:
    """The shape a publisher API returns: article markup with no document around it."""
    fragment = "".join(f"<p>{p}</p>" for p in _PARAS).encode()

    text = extract(fragment, URL)

    assert text is not None
    assert all(p in text for p in _PARAS)


def test_the_pages_own_charset_is_honoured() -> None:
    """Bytes go in as bytes; a Latin-1 page declaring itself comes out with its accents."""
    para = "Le maire a présenté le projet à la réunion du conseil, après un débat très long."
    page = (
        '<html><head><meta charset="iso-8859-1"></head><body><article>'
        + f"<p>{para}</p>" * 3
        + "</article></body></html>"
    ).encode("iso-8859-1")

    text = extract(page, URL)

    assert text is not None
    assert para in text
