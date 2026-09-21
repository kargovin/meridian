"""Article body from a fetched page (FR-I4, tier 3).

trafilatura with ``favor_precision`` — scored against seven alternatives on this roster's own
pages: 94% consensus recall, a clean start on 24 of 25, boilerplate on 2, ~33 ms a page. The
flag halves boilerplate contamination at no recall cost. Tables and comments are out, as they
were in the bench.

⚠️ What this cannot do, measured: tell a short *message about the page* from a short article.
"JavaScript is required…" (115 chars) and a bot-challenge page served with a 200 (120) come out
longer than a one-sentence article (33) or a correction notice (113); an error page is longer
still (a 404 here extracted to 5,300 chars of cookie policy). Length does not separate them, and
nothing here tries to. Error pages are excluded by the status code before the bytes reach this
module, and a page that only renders in a browser is a fact about the *feed* — an acquisition
tier — not something to detect per page. ``MIN_BODY_CHARS`` is a guard against fragments only.
"""

import trafilatura

#: Below this the result is not a body. Every fragment trafilatura produced from degenerate
#: markup was 10 characters or fewer ("x", "Loading..."); every sentence was 33 or more. The
#: midpoint. Not a junk detector — see the module docstring for why none is possible by length.
MIN_BODY_CHARS = 20


def extract(html: bytes, url: str) -> str | None:
    """The article's text, or None when the page holds no body worth the name.

    Takes bytes, not text: trafilatura reads the page's own charset declaration, so decoding
    here would be a second guess layered under its first. The URL is for its link and date
    heuristics and never fetched. A bare HTML fragment — what a publisher API returns — works
    as well as a whole document (measured).
    """
    text = trafilatura.extract(
        html,
        url=url,
        favor_precision=True,
        include_comments=False,
        include_tables=False,
        output_format="txt",
    )
    if text is None or len(text) < MIN_BODY_CHARS:
        return None
    return text
