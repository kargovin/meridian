"""Tier-2 adapters: the two per-publisher translations, and the lookup that reaches them."""

import json
from pathlib import Path

import pytest

from meridian.ingest.adapters import ADAPTERS, adapter_for
from meridian.ingest.extract import extract

FIXTURE = Path(__file__).parent / "fixtures" / "ec_presscorner_document.json"
EC = ADAPTERS["ec.europa.eu"]
EC_API = "https://ec.europa.eu/commission/presscorner/api/documents"


# Every document type the feed carried on 19 Sep 2026, and the reference each maps to.
@pytest.mark.parametrize(
    ("slug", "reference"),
    [
        ("speech_26_1909", "SPEECH/26/1909"),
        ("ip_26_1900", "IP/26/1900"),
        ("statement_26_1895", "STATEMENT/26/1895"),
        ("mex_26_1904", "MEX/26/1904"),
        ("fs_26_1880", "FS/26/1880"),
        ("read_26_1902", "READ/26/1902"),
    ],
)
def test_the_commission_detail_url_maps_to_its_document_reference(
    slug: str, reference: str
) -> None:
    url = f"https://ec.europa.eu/commission/presscorner/detail/en/{slug}"

    assert EC.request_url(url) == f"{EC_API}?reference={reference}&language=en"


def test_the_language_comes_from_the_article_url() -> None:
    url = "https://ec.europa.eu/commission/presscorner/detail/fr/ip_26_1900"

    assert EC.request_url(url) == f"{EC_API}?reference=IP/26/1900&language=fr"


@pytest.mark.parametrize(
    "url",
    [
        "https://ec.europa.eu/commission/presscorner/api/rss?language=en",
        "https://ec.europa.eu/commission/presscorner/home/en",
        "https://ec.europa.eu/commission/presscorner/detail/en/ip-26-1900",
        "https://ec.europa.eu/commission/presscorner/detail/en/ip_26_1900/extra",
        "https://ec.europa.eu/commission/presscorner/detail/en/IP_26_1900",
        "https://ec.europa.eu/",
    ],
)
def test_a_url_of_any_other_shape_is_none(url: str) -> None:
    """Never guess at a reference: a link the pattern does not fit is not fetched at all."""
    assert EC.request_url(url) is None


def test_the_html_is_lifted_out_of_a_real_response() -> None:
    body = FIXTURE.read_bytes()
    expected = json.loads(body)["docuLanguageResource"]["htmlContent"]

    html = EC.html_from(body)

    assert html is not None
    assert html.decode() == expected
    assert html.startswith(b"<p>")


@pytest.mark.parametrize(
    "body",
    [
        b"",
        b"<html><body>Service unavailable</body></html>",
        b"[]",
        b'{"docuLanguageResource": null}',
        b'{"docuLanguageResource": "nope"}',
        b'{"docuLanguageResource": {}}',
        b'{"docuLanguageResource": {"htmlContent": null}}',
        b'{"docuLanguageResource": {"htmlContent": ""}}',
        b'{"docuLanguageResource": {"htmlContent": 42}}',
        b'{"document": {"htmlContent": "<p>moved</p>"}}',
    ],
    ids=[
        "empty",
        "html not json",
        "json list",
        "resource null",
        "resource string",
        "resource without html",
        "html null",
        "html empty",
        "html not a string",
        "renamed top-level key",
    ],
)
def test_a_response_of_any_other_shape_is_none(body: bytes) -> None:
    assert EC.html_from(body) is None


def test_the_lifted_html_extracts_as_a_body() -> None:
    """The adapter's output is what ``extract`` receives: a fragment, entities and all."""
    html = EC.html_from(FIXTURE.read_bytes())
    assert html is not None

    text = extract(html, "https://ec.europa.eu/commission/presscorner/detail/en/ip_26_1900")

    assert text is not None
    assert "The European Commission will today disburse €3.3 billion" in text
    assert "<p>" not in text
    assert "&euro;" not in text


def test_the_adapter_is_found_by_the_article_host() -> None:
    assert adapter_for("https://ec.europa.eu/commission/presscorner/detail/en/ip_26_1900") is EC
    assert adapter_for("https://EC.EUROPA.EU/commission/presscorner/detail/en/ip_26_1900") is EC


@pytest.mark.parametrize(
    "url",
    [
        "https://www.who.int/news/item/19-09-2026-something",
        "https://commission.europa.eu/news/some-article_en",
        "https://europa.eu/",
        "not a url",
        "",
    ],
)
def test_a_host_with_no_publisher_api_has_no_adapter(url: str) -> None:
    """Including the Commission's own other hosts: an adapter is per API, not per publisher."""
    assert adapter_for(url) is None
