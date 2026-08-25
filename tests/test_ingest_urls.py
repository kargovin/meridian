"""Reducing an article URL to one stable form (RFC §5.1)."""

import pytest

from meridian.ingest.urls import canonicalize


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("https://x.example/a?utm_source=rss", "https://x.example/a"),
        ("https://x.example/a?utm_medium=x&utm_campaign=y", "https://x.example/a"),
        # BBC's families.
        ("https://x.example/a?at_medium=RSS&at_campaign=64", "https://x.example/a"),
        ("https://x.example/a?ns_mchannel=rss", "https://x.example/a"),
        # The one that made the constraint stop collapsing anything.
        ("https://x.example/a?traffic_source=rss", "https://x.example/a"),
        ("https://x.example/a?fbclid=abc", "https://x.example/a"),
        # Case-insensitive by name.
        ("https://x.example/a?CMP=share_btn", "https://x.example/a"),
    ],
    ids=lambda v: v if isinstance(v, str) and "?" in v else "",
)
def test_tracking_parameters_are_removed(raw: str, expected: str) -> None:
    assert canonicalize(raw) == expected


def test_a_parameter_that_carries_meaning_is_kept() -> None:
    """⚠️ The asymmetry this module is built around: stripping a meaningful parameter merges two
    different articles into one record and loses one silently, where failing to strip a tracking
    one merely leaves a duplicate that dedup collapses. Under-stripping is the safe direction.
    """
    assert canonicalize("https://x.example/article?id=4172") == "https://x.example/article?id=4172"
    assert (
        canonicalize("https://x.example/p?story=9&page=2") == "https://x.example/p?page=2&story=9"
    )


def test_parameter_order_does_not_change_the_result() -> None:
    """A feed link and a share link routinely disagree about order."""
    assert canonicalize("https://x.example/a?b=2&a=1") == canonicalize(
        "https://x.example/a?a=1&b=2"
    )


def test_a_fragment_is_dropped() -> None:
    """Never sent to the server, so two URLs differing only there are the same request."""
    assert canonicalize("https://x.example/a#comments") == "https://x.example/a"


def test_scheme_and_host_are_lowercased() -> None:
    assert canonicalize("HTTPS://X.Example/A") == "https://x.example/A"


def test_a_default_port_is_dropped_and_a_real_one_is_kept() -> None:
    assert canonicalize("https://x.example:443/a") == "https://x.example/a"
    assert canonicalize("https://x.example:8443/a") == "https://x.example:8443/a"


def test_a_trailing_slash_is_left_alone() -> None:
    """⚠️ Deliberate. ``/a`` and ``/a/`` are different resources to some servers, so folding
    them would be the merging kind of mistake, not the duplicating kind.
    """
    assert canonicalize("https://x.example/a/") != canonicalize("https://x.example/a")


def test_a_url_with_nothing_to_strip_is_unchanged() -> None:
    assert canonicalize("https://x.example/a/b") == "https://x.example/a/b"
