"""The near-duplicate fingerprint (FR-I5, 2.1.2 §3.2)."""

import subprocess
import sys

import pytest

from meridian.dedup.fingerprint import BITS, SHINGLE_WORDS, distance, fingerprint
from meridian.ingest.normalize import content_hash, normalized_tokens

#: ⚠️ Three sentences, not one, and the length is load-bearing. A fingerprint moves in
#: proportion to the share of shingles that changed, so appending a credit line to a
#: one-sentence body shifts ~9 bits while the same edit to this one shifts under 3. Real
#: bodies run to thousands of shingles, which is the regime the measured threshold comes
#: from; a short fixture would read as a broken function.
ARTICLE = (
    "The minister announced the new policy on Tuesday, citing the budget review. "
    "Officials said the measure would take effect in the spring, and that consultation "
    "with local authorities had already begun. Critics called the timetable optimistic."
)


def test_the_same_body_always_fingerprints_the_same() -> None:
    assert fingerprint(ARTICLE) == fingerprint(ARTICLE)


def test_a_rehosted_copy_stays_close() -> None:
    """The case FR-I5 exists for: one outlet's copy of another's story, lightly edited."""
    rehosted = ARTICLE + " (Reuters)"
    assert distance(_fp(ARTICLE), _fp(rehosted)) <= 3


def test_an_unrelated_article_is_far_away() -> None:
    """Unrelated bodies sit near BITS/2. The gap to the bar is what makes 3 safe."""
    other = (
        "Astronomers detected water vapour in the atmosphere of a distant exoplanet, "
        "using observations gathered over four nights. The signal was weak but consistent "
        "across instruments, and the team plans further study of the atmosphere."
    )
    assert distance(_fp(ARTICLE), _fp(other)) > 3 * 4


def test_word_order_changes_the_fingerprint() -> None:
    """Shingles, not a bag of words: the same words rearranged are a different document.

    With SHINGLE_WORDS = 1 this passes through — which is the point of asserting it.
    """
    words = ARTICLE.split()
    shuffled = " ".join(words[::-1])
    assert distance(_fp(ARTICLE), _fp(shuffled)) > 3


def test_shingle_counts_are_weights_not_just_membership() -> None:
    """A shingle seen three times must not weigh the same as one seen once.

    ⚠️ The fixture is a repeating cycle, and it has to be. Appending any new text creates new
    shingles at the join, so the two bodies would differ under *any* weighting and the test
    would pass without touching the thing it names. A cycle repeated a different number of
    times has the identical shingle **set** and different counts — the only shape where
    membership and weight can disagree. The set equality is asserted, so a change to
    SHINGLE_WORDS fails here loudly instead of quietly making this vacuous again.
    """
    twice = "alpha beta gamma " * 2
    thrice = "alpha beta gamma " * 3
    assert _shingle_set(twice) == _shingle_set(thrice)
    assert _fp(twice) != _fp(thrice)


@pytest.mark.parametrize("body", ["", "   ", "one", "one two", "\n\t "])
def test_a_body_too_short_to_shingle_has_no_fingerprint(body: str) -> None:
    """None, never 0 — a zero fingerprint is distance 0 from every other zero fingerprint,
    so two unrelated fragments would be collapsed as duplicates."""
    assert len(normalized_tokens(body)) < SHINGLE_WORDS
    assert fingerprint(body) is None


def test_exactly_enough_words_has_a_fingerprint() -> None:
    """The boundary the case above stops at."""
    assert fingerprint("one two three") is not None


def test_the_fingerprint_fits_the_declared_width() -> None:
    value = _fp(ARTICLE)
    assert 0 <= value < 1 << BITS


def test_the_fingerprint_survives_a_new_process() -> None:
    """⚠️ The built-in hash() is salted per process, so fingerprints written before a restart
    would not compare with those written after — invisible to any single-process test.

    Two interpreters, two hash seeds, one expected value.
    """
    script = f"from meridian.dedup.fingerprint import fingerprint;print(fingerprint({ARTICLE!r}))"
    values = {
        subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True,
            text=True,
            check=True,
            env={"PYTHONHASHSEED": seed, "PATH": "/usr/bin:/bin"},
        ).stdout.strip()
        for seed in ("0", "1")
    }
    assert len(values) == 1, f"the fingerprint moved between hash seeds: {values}"
    assert values == {str(_fp(ARTICLE))}


def test_content_hash_reads_the_same_tokens() -> None:
    """Both detectors normalize once. If they ever diverge, one is matching on a word the
    other never saw."""
    quirky = "Don't  stop—now… “really”?"
    assert content_hash(quirky) == content_hash(" ".join(normalized_tokens(quirky)))


def _shingle_set(body: str) -> set[str]:
    """The fixture check above only — the module keeps its shingling private."""
    tokens = normalized_tokens(body)
    return {" ".join(tokens[i : i + SHINGLE_WORDS]) for i in range(len(tokens) - SHINGLE_WORDS + 1)}


def _fp(body: str) -> int:
    value = fingerprint(body)
    assert value is not None, "fixture body is too short to fingerprint"
    return value
