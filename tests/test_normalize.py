"""Normalization's two judgements: where text ends, and whether it is English (FR-I4, FR-I7).

No database — these are pure functions, which is the point of them being in their own module.
"""

import pytest

from meridian.ingest.normalize import (
    LanguageVerdict,
    detect_language,
    language_input,
    strip_html,
)

# --------------------------------------------------------------------------- strip_html


def test_a_block_boundary_becomes_a_space() -> None:
    """AC2. ⚠️ The falsifier for the whole module.

    Strip the tags without emitting a separator and this reads "here.New" — one malformed
    sentence where there were two. Nothing raises, the text still looks almost right, and
    every later stage that splits on sentence boundaries is silently given bad input. Verified
    by removing ``_BLOCK_TAGS`` from the start/end handlers: only this test fails.
    """
    assert strip_html("<p>Ends here.</p><p>New sentence.</p>") == "Ends here. New sentence."


@pytest.mark.parametrize(
    "raw",
    [
        "<div>One.</div><div>Two.</div>",
        "<ul><li>One.</li><li>Two.</li></ul>",
        "One.<br>Two.",
        "<h2>One.</h2>Two.",
        "<table><tr><td>One.</td><td>Two.</td></tr></table>",
    ],
)
def test_every_block_element_separates(raw: str) -> None:
    """One list, several spellings. A feed picks whichever its CMS emits."""
    assert strip_html(raw) == "One. Two."


def test_an_inline_tag_does_not_split_a_word() -> None:
    """The other direction: separating on <b> would break words apart mid-token."""
    assert strip_html("<p>Ferry operators have <b>sus</b>pended sailings.</p>") == (
        "Ferry operators have suspended sailings."
    )


def test_entities_are_resolved() -> None:
    assert strip_html("Smith &amp; Sons said &quot;no&quot;") == 'Smith & Sons said "no"'


def test_a_non_breaking_space_collapses_like_a_space() -> None:
    """&nbsp; becomes \\xa0, which reads as a space and is not one to a naive splitter."""
    assert strip_html("a&nbsp;&nbsp;b") == "a b"


def test_script_content_is_not_text() -> None:
    assert strip_html("<script>var x = 1;</script>Real text") == "Real text"
    assert strip_html("<style>.a{color:red}</style>Real text") == "Real text"


def test_markup_with_no_text_is_absent_rather_than_empty() -> None:
    """So "no teaser" and "an empty div" are one absence downstream, not two."""
    assert strip_html("<div></div>") is None
    assert strip_html("   ") is None
    assert strip_html(None) is None


def test_stripping_is_idempotent() -> None:
    """Re-running the stage on a record must not degrade what it already wrote."""
    once = strip_html("<p>Ends here.</p><p>New sentence.</p>")
    assert strip_html(once) == once


def test_malformed_markup_still_yields_its_text() -> None:
    """Publishers ship unclosed tags; a teaser is not worth failing an article over."""
    assert strip_html("<p>Unclosed <b>bold text") == "Unclosed bold text"


# --------------------------------------------------------------------------- detect_language


@pytest.mark.parametrize(
    "text",
    [
        "Storm Bertha closes ports across the south coast",
        "Markets fall",
        "Zelensky Macron Starmer",
        "2026 Q3 GDP 4.1%",
        "WATCH: FLOODS HIT COASTAL TOWNS",
        "Breaking",
    ],
)
def test_english_survives_however_short(text: str) -> None:
    """AC4, and the direction that matters.

    ⚠️ These are real headline shapes, and the confidences behind them are low — "Zelensky
    Macron Starmer" scores 0.38, "Markets fall" 0.59. A rule of "keep only what is confidently
    English" passes every other test in this file and permanently deletes all of these.
    """
    assert detect_language(text).drop is False


@pytest.mark.parametrize(
    "text",
    [
        "Por que Cuba no produce suficiente comida para alimentar a su poblacion",
        "Le mysterieux voyage express du directeur de la CIA en Russie",
        "Feuer-Katastrophe in einer Entbindungsstation in Pakistan",
    ],
)
def test_a_confident_foreign_verdict_drops(text: str) -> None:
    verdict = detect_language(text)
    assert verdict.drop is True
    assert verdict.language != "en"


@pytest.mark.parametrize(
    "text",
    [
        "الحكومة تعلن عن إجراءات اقتصادية جديدة لاحتواء التضخم",
        "政府宣布新的经济措施以遏制该国的通货膨胀",
        # Cyrillic below is deliberate: RUF001 flags the confusable characters that are
        # the entire point of the fixture.
        "Правительство объявило о новых экономических мерах",  # noqa: RUF001
        "सरकार ने नए आर्थिक उपायों की घोषणा की",
    ],
)
def test_non_latin_script_drops(text: str) -> None:
    """⚠️ The case the script gate exists for, and it is not a nicety.

    The detector is restricted to Latin-script languages, so for these it returns *no opinion*
    — and no opinion is what the drop rule treats as a reason to keep. Delete ``_is_latin_script``
    and every one of these is kept as possibly-English, with nothing anywhere reporting it.
    """
    verdict = detect_language(text)
    assert verdict.drop is True
    assert verdict.language is None


def test_text_with_no_letters_is_not_evidence_of_a_foreign_language() -> None:
    """A headline of digits and punctuation is a bad headline, not a foreign one."""
    assert detect_language("... 2026 ...") == LanguageVerdict(language=None, drop=False)
    assert detect_language("").drop is False
    assert detect_language("   ").drop is False


def test_an_undetermined_language_is_stored_as_null_not_guessed() -> None:
    """NULL and "en" are different claims; only one of them is true here."""
    assert detect_language("...").language is None


# --------------------------------------------------------------------------- language_input


def test_the_teaser_joins_the_title_when_there_is_one() -> None:
    assert language_input("Storm closes ports", "Ferries are cancelled.") == (
        "Storm closes ports Ferries are cancelled."
    )


def test_the_title_is_the_whole_evidence_when_there_is_no_teaser() -> None:
    """Two of the five publishers we can currently read ship no teaser at all."""
    assert language_input("Storm closes ports", None) == "Storm closes ports"
    assert language_input("Storm closes ports", "") == "Storm closes ports"
