"""Normalization's two judgements: where text ends, and whether it is English (FR-I4, FR-I7).

No database — these are pure functions, which is the point of them being in their own module.
"""

from pathlib import Path

import pytest

from meridian.ingest.normalize import (
    LanguageVerdict,
    _is_latin_script,
    body_from_feed,
    body_from_html,
    body_from_plain_text,
    content_hash,
    detect_language,
    language_input,
    strip_html,
)

GLOBAL_VOICES_BODY = (
    Path(__file__).parent / "fixtures" / "global_voices_content_encoded.html"
).read_text()

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


def test_stripping_ordinary_prose_is_a_fixpoint() -> None:
    """The common case: a second pass over plain text changes nothing."""
    once = strip_html("<p>Ends here.</p><p>New sentence.</p>")
    assert strip_html(once) == once


def test_stripping_escaped_markup_is_NOT_a_fixpoint() -> None:
    """⚠️ The limit, asserted rather than assumed — and the reason this is written down.

    An earlier version of this test used an entity-free input and claimed idempotence, which
    is the shape of test that passes while asserting nothing about the property it names.

    A teaser that *describes* markup decays one pass at a time: ``&lt;redacted&gt;`` becomes a
    real ``<redacted>``, which the next pass reads as a tag and deletes. Harmless while the
    stage runs once per record; a silent content deletion the moment RFC §9's replay rewinds
    ``pipeline_state`` and re-enqueues.
    """
    once = strip_html("The company said &lt;redacted&gt; had been removed.")
    assert once == "The company said <redacted> had been removed."
    assert strip_html(once) == "The company said had been removed."

    # Entities decay one level per pass, for the same reason.
    assert (
        strip_html("Profits rose 5 &amp;amp; margins held") == "Profits rose 5 &amp; margins held"
    )


def test_malformed_markup_still_yields_its_text() -> None:
    """Publishers ship unclosed tags; a teaser is not worth failing an article over."""
    assert strip_html("<p>Unclosed <b>bold text") == "Unclosed bold text"


# --------------------------------------------------------------------------- body_from_html


def test_a_body_keeps_each_block_on_its_own_line() -> None:
    """Where ``strip_html`` puts a space, a body puts a line break: a tier-3 extraction keeps
    paragraphs apart, and a tier-1 body should read the same way."""
    assert body_from_html("<p>Ends here.</p><p>New sentence.</p>") == "Ends here.\nNew sentence."


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
def test_every_block_element_starts_a_line(raw: str) -> None:
    assert body_from_html(raw) == "One.\nTwo."


def test_an_inline_tag_does_not_split_a_body_line() -> None:
    assert body_from_html("<p>Ferry operators have <b>sus</b>pended sailings.</p>") == (
        "Ferry operators have suspended sailings."
    )


def test_a_newline_in_the_source_is_whitespace_not_a_line_break() -> None:
    """⚠️ HTML source wraps long paragraphs, and to HTML a newline is a space. Breaking lines
    on the newlines the text already holds, rather than on block boundaries, splits one
    paragraph into several."""
    assert body_from_html("<p>Ferry operators\n  have suspended\tsailings.</p>") == (
        "Ferry operators have suspended sailings."
    )


def test_nested_and_empty_blocks_leave_no_empty_lines() -> None:
    raw = "<div>\n<p>One.</p>\n</div>\n\n<div><p> &nbsp;</p></div><p>Two.</p>"
    assert body_from_html(raw) == "One.\nTwo."


def test_a_body_resolves_entities_and_non_breaking_spaces() -> None:
    assert body_from_html("<p>Smith &amp;&nbsp;&nbsp;Sons said &quot;no&quot;</p>") == (
        'Smith & Sons said "no"'
    )


def test_script_content_is_not_body_text() -> None:
    assert body_from_html("<p>Real text</p><script>var x = 1;</script>") == "Real text"
    assert body_from_html("<style>.a{color:red}</style><p>Real text</p>") == "Real text"


LITERAL_TEXT_ELEMENTS = ["textarea", "iframe", "noembed", "noframes", "xmp", "title", "plaintext"]


@pytest.mark.parametrize("tag", LITERAL_TEXT_ELEMENTS)
def test_an_element_parsed_as_literal_text_is_not_body_text(tag: str) -> None:
    """``HTMLParser`` returns these elements' contents as literal text, tags and all."""
    assert body_from_html(f"<p>Real text</p><{tag}><b>embed code</b></{tag}>") == "Real text"


@pytest.mark.parametrize("tag", LITERAL_TEXT_ELEMENTS)
def test_a_teaser_mentioning_one_of_those_elements_keeps_the_rest_of_its_text(tag: str) -> None:
    """The teaser is read as HTML even when the feed declared it plain text; skipping the
    element's contents there would delete everything after the word."""
    assert strip_html(f"Use <{tag}> for input. The rest.") == "Use for input. The rest."


def test_a_body_that_is_only_markup_is_absent() -> None:
    assert body_from_html('<div><img src="a.jpg"/></div>') is None
    assert body_from_html("   ") is None
    assert body_from_html(None) is None


def test_a_real_feed_body_converts_block_for_block() -> None:
    """Global Voices' own markup: a caption ``div`` round an image, italics nested round
    links, a blockquote wrapping a paragraph, a byline built from spans."""
    assert body_from_html(GLOBAL_VOICES_BODY) == "\n".join(
        [
            "Online vs offline protest, how effective it is?",
            "Originally published on Global Voices",
            "Image by Gerd Altmann from Pixabay. Used under a Pixabay license.",
            "This post is part of Global Voices\u2019 September 2026 Spotlight series, “Protest in"
            " Democracy.” With this Spotlight, we seek to explore the many forms of protest, the"
            " tactics states use to delegitimize and suppress them, and the complex relationship"
            " between protest and democracy. You can support this coverage by donating here.",
            "The growing popularity and expanding use of social media have influenced the ways"
            " people express themselves. While protests once involved taking to the streets with"
            " banners and chants, such activities can now be carried out in the virtual realm."
            " Messages and slogans are now conveyed through hashtags, digital posters, or videos"
            " posted across various social media channels. Events such as the Arab Spring and the"
            " Occupy Movement have further reinforced the phenomenon of online protest.",
            "Both of them are effective, no?",
            "I contend that, setting aside the issue of unequal access, digital networks are not"
            " egalitarian networks where citizens have equal opportunities to participate in"
            " public discourse. First and foremost, the internet is never inherently egalitarian."
            " Instead, the structure of the internet exhibits the characteristics of a scale-free"
            " network—a network in which the degree distribution follows a power law.",
            "To address the disparity in internet access, CSOs must continue to conduct"
            " face-to-face educational sessions and discussions that reach individuals with poor"
            " internet connectivity. Ultimately, even if access remains unequal, both online and"
            " offline activism can contribute meaningfully to a movement and complement each"
            " other.",
            "Written by Juliana Harsianti",
        ]
    )


def test_line_breaks_do_not_move_the_exact_duplicate_hash() -> None:
    """Dedup reads words, not layout: the body and the same HTML flattened to one run hash
    alike, and the raw HTML does not."""
    body = body_from_html(GLOBAL_VOICES_BODY)
    flat = strip_html(GLOBAL_VOICES_BODY)
    assert body is not None and flat is not None
    assert "\n" in body and "\n" not in flat
    assert content_hash(body) == content_hash(flat)
    assert content_hash(body) != content_hash(GLOBAL_VOICES_BODY)


# --------------------------------------------------------------------------- body_from_feed


def test_plain_text_keeps_what_an_html_parser_would_delete() -> None:
    """Atom ``type="text"`` arrives unescaped. Read as HTML, "<b and c>" is a tag and goes."""
    assert body_from_feed("Para one.\n\nPara two: a<b and c>d, AT&T.", "text/plain") == (
        "Para one.\nPara two: a<b and c>d, AT&T."
    )


def test_plain_text_breaks_on_a_blank_line_whatever_the_line_endings() -> None:
    assert body_from_plain_text("P1\r\n\r\nP2") == "P1\nP2"


def test_plain_text_breaks_on_a_line_holding_only_whitespace() -> None:
    assert body_from_plain_text("P1\n \nP2") == "P1\nP2"


def test_plain_text_breaks_on_blank_lines_not_on_wrapping() -> None:
    assert body_from_plain_text("One line\nwrapped.\n \n\n  Two.  \n") == "One line wrapped.\nTwo."
    assert body_from_plain_text(" \n\n ") is None
    assert body_from_plain_text(None) is None


@pytest.mark.parametrize(
    "content_type",
    [
        "text/html",
        "application/xhtml+xml",
        "text/html; charset=utf-8",
        "text/html ; charset=utf-8",
        "TEXT/HTML",
    ],
)
def test_markup_types_are_converted(content_type: str) -> None:
    assert body_from_feed("<p>One.</p><p>Two.</p>", content_type) == "One.\nTwo."


@pytest.mark.parametrize("content_type", ["text/markdown", "text/plain; charset=utf-8"])
def test_plain_text_types_are_read_as_written(content_type: str) -> None:
    assert body_from_feed("a<b and c>d", content_type) == "a<b and c>d"


@pytest.mark.parametrize(
    "content_type",
    ["text/xml", "text/html-sandboxed", "image/png", "application/octet-stream", "", None],
)
def test_content_of_any_other_type_is_no_body(content_type: str | None) -> None:
    """``text/xml`` included: it can hold escaped HTML, handed back as markup."""
    assert body_from_feed("hello", content_type) is None


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


@pytest.mark.parametrize(
    "text",
    [
        "C\u00f3mo puede contraatacar Canad\u00e1 a la econom\u00eda de EE.UU.",
        "Le myst\u00e9rieux voyage express du directeur de la CIA en Russie",
        "\u00dcber die Zukunft der deutschen Wirtschaft und ihrer Industrie",
        "A a\u00e7\u00e3o do governo brasileiro sobre a economia nacional",
    ],
)
def test_accented_latin_reaches_the_detector_rather_than_the_script_gate(text: str) -> None:
    """⚠️ The gate returns *before* the detector runs, so a rule that counted only unaccented
    ASCII as Latin would send every Spanish, French, German and Portuguese headline out through
    it — dropped with ``language`` NULL, which is the right outcome reached by the wrong route
    and would hide a broken detector completely.

    ⚠️ These fixtures must keep their diacritics. An earlier version of this test had them
    stripped to avoid a lint rule about confusable characters, which made it assert nothing
    about accented text at all while still being named for it.
    """
    assert any(ord(char) > 127 for char in text), "fixture lost its diacritics"
    assert _is_latin_script(text) is True

    verdict = detect_language(text)
    assert verdict.drop is True
    assert verdict.language is not None  # the detector ran and had an opinion


def test_mixed_script_goes_on_the_majority() -> None:
    """An English headline quoting another script is still an English headline."""
    verdict = detect_language(
        "Protesters chanted \u0627\u0644\u062d\u0631\u064a\u0629 in the square today"
    )
    assert verdict.drop is False
    assert verdict.language == "en"


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


# --------------------------------------------------------------------------- the drop bar


@pytest.mark.parametrize(
    "text",
    [
        "Football transfer rumours: Jean-Philippe Mateta to Aston Villa?",
        "Mercedes' upgrades & Ferrari team orders - F1 Q&A",
        "Itauma v Hrgovic & Mayer v Cameron - all you need to know",
        "Hrgovic taunts starting to wind me up - Itauma",
        "Tech Life",
    ],
)
def test_a_hesitant_foreign_call_does_not_delete_an_english_article(text: str) -> None:
    """⚠️ Every one of these is a real headline from a roster publisher, and every one was
    permanently deleted before the confidence bar existed.

    lingua returns a normalised distribution, so its top candidate is never 0.0 — the lowest
    seen anywhere in the calibration corpus is 0.23. A rule gated only on ``!= 0.0`` therefore
    drops on *any* non-English top-1, including 0.288 with a 0.009 margin over the runner-up.
    Measured at title-only length: 9 of 479 English headlines, 1.88%, gone with no trace.

    ⚠️ These live in short proper-noun-heavy headlines — sport, markets, programme names. The
    first calibration corpus was World/International only, minimum 6 tokens, and could not
    produce this input at all. A corpus that cannot produce the failing case cannot falsify
    the rule it is used to justify.
    """
    verdict = detect_language(text)
    assert verdict.drop is False


def test_a_hesitant_call_records_no_language() -> None:
    """Below the bar we are declining to conclude, so NULL rather than a wrong label.

    Writing "de" onto an English sport headline stores a false fact for a future reader to
    trust. Detection is pure, so anyone debugging can re-run it.
    """
    assert detect_language("Tech Life").language is None


@pytest.mark.parametrize(
    "text",
    [
        "Por que Cuba no produce suficiente comida para alimentar a su poblacion",
        "Le mysterieux voyage express du directeur de la CIA en Russie",
        "Feuer-Katastrophe in einer Entbindungsstation in Pakistan",
    ],
)
def test_the_bar_does_not_stop_a_confident_foreign_call(text: str) -> None:
    """The other direction: the bar must not turn FR-I7 off.

    Correct foreign calls sit far above it — 92.5% land at 0.8 or better.
    """
    assert detect_language(text).drop is True


# --------------------------------------------------------------------------- content_hash

_BODY = "The council approved the plan on Tuesday. Opponents asked for a review."


def test_the_hash_is_a_sha256_hex_digest() -> None:
    digest = content_hash(_BODY)
    assert len(digest) == 64 and int(digest, 16) >= 0


@pytest.mark.parametrize(
    "variant",
    [
        "the council approved the plan on tuesday opponents asked for a review",
        "The  council approved\nthe plan on Tuesday.\n\nOpponents asked for a review.",
        "  The council approved the plan on Tuesday — Opponents asked for a review!  ",
        "“The council approved the plan on Tuesday.” Opponents asked for a review…",
    ],
    ids=["case-and-punctuation", "whitespace", "dash-and-bang", "curly-quotes-and-ellipsis"],
)
def test_case_punctuation_and_whitespace_do_not_change_the_hash(variant: str) -> None:
    """2.1.2 §3.1's three normalizations, one at a time and together. Each is a way one wire
    story is re-hosted with a different byte sequence and the same words."""
    assert content_hash(variant) == content_hash(_BODY)


def test_different_words_hash_differently() -> None:
    assert content_hash(_BODY) != content_hash(_BODY.replace("approved", "rejected"))


def test_symbols_are_kept_because_they_carry_meaning() -> None:
    """Punctuation is stripped; symbols are not. "$5m" and "5m" are different figures."""
    assert content_hash("The deal is worth $5m.") != content_hash("The deal is worth 5m.")
    assert content_hash("A + B") != content_hash("A B")
