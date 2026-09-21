"""Publisher APIs behind acquisition tier 2 (FR-I4).

For a ``2_publisher_api`` feed the article URL is a browser application and the words live at
a second URL, the publisher's own API. An adapter is the two translations that differ per
publisher — which URL to request, and where in the response the HTML is — and nothing else:
rights, robots, pacing, the fetch, extraction and the write are the shared acquire path, and
robots is read for the URL actually requested.

One publisher, one entry, chosen by the article URL's host and reached only because the
registry says the feed is tier 2. Nothing here decides a page needs a browser by looking at it.

An adapter never guesses. A URL or a response that is not the exact shape it knows yields
None; acquire counts and logs that and the record continues headline-only, so a publisher
changing its site fails loudly on every article of every cycle and writes nothing wrong. The
one change it cannot see is the HTML still arriving but saying less.
"""

import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from urllib.parse import urlsplit


@dataclass(frozen=True, slots=True)
class Adapter:
    #: The URL to request for an article, or None when the article URL is not a shape this
    #: publisher's API is known to answer for.
    request_url: Callable[[str], str | None]
    #: The HTML inside the API's response body, or None when the body is not what the API
    #: promised. Not text: it goes to ``extract`` as a fragment.
    html_from: Callable[[bytes], bytes | None]


# European Commission press corner. The web app maps detail/<lang>/<type>_<yy>_<n> to the
# document reference <TYPE>/<yy>/<n> and calls this endpoint; the reference keeps its slashes
# unescaped, as the app sends them.
_EC_DETAIL = re.compile(
    r"^/commission/presscorner/detail/(?P<lang>[a-z]{2})/(?P<type>[a-z]+)_(?P<yy>\d+)_(?P<n>\d+)$"
)
_EC_API = "https://ec.europa.eu/commission/presscorner/api/documents"


def _ec_request_url(article_url: str) -> str | None:
    match = _EC_DETAIL.match(urlsplit(article_url).path)
    if match is None:
        return None
    reference = f"{match['type'].upper()}/{match['yy']}/{match['n']}"
    return f"{_EC_API}?reference={reference}&language={match['lang']}"


def _ec_html_from(body: bytes) -> bytes | None:
    try:
        document = json.loads(body)
    except ValueError:
        return None
    if not isinstance(document, dict):
        return None
    resource = document.get("docuLanguageResource")
    if not isinstance(resource, dict):
        return None
    html = resource.get("htmlContent")
    if not isinstance(html, str) or not html:
        return None
    return html.encode()


ADAPTERS: dict[str, Adapter] = {
    "ec.europa.eu": Adapter(request_url=_ec_request_url, html_from=_ec_html_from),
}


def adapter_for(article_url: str) -> Adapter | None:
    """The adapter for the article's host, or None when no publisher API is known there."""
    host = urlsplit(article_url).hostname
    if host is None:
        return None
    return ADAPTERS.get(host)
