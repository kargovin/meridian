"""Turning what a feed said into what the pipeline can use (FR-I4, FR-I7).

Pure: text in, text out. No database, no network, no configuration — which is what lets the
two fiddly judgements here (where a sentence ends, and whether this is English) be tested
against fixtures rather than against a live poll.

Discovery stores the teaser exactly as the publisher wrote it, markup included, because
``parse`` records and does not judge. This is where it is judged.
"""

import re
import unicodedata
from dataclasses import dataclass
from functools import lru_cache
from html.parser import HTMLParser

from lingua import Language, LanguageDetector, LanguageDetectorBuilder

#: Elements that end a run of text. Anything not listed here is inline and joins without a gap.
#:
#: ⚠️ This list is the whole reason the stripper is not three lines. Dropping tags without
#: putting a separator where a block ended welds the last word of one paragraph to the first
#: of the next — "...ends here.</p><p>New sentence..." becomes "...ends here.New sentence...".
#: Nothing errors, the text still reads almost right, and every downstream stage that splits
#: on sentences silently gets one long malformed one. Measured elsewhere in this codebase: the
#: same defect cost an extraction library 52 points of apparent recall before the harness was
#: fixed, and the library was never at fault.
_BLOCK_TAGS = frozenset(
    {
        "address",
        "article",
        "aside",
        "blockquote",
        "br",
        "dd",
        "div",
        "dl",
        "dt",
        "figcaption",
        "figure",
        "footer",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "header",
        "hr",
        "li",
        "main",
        "nav",
        "ol",
        "p",
        "pre",
        "section",
        "table",
        "tbody",
        "td",
        "tfoot",
        "th",
        "thead",
        "tr",
        "ul",
    }
)

#: Elements whose *content* is code, not prose. A feed should never carry these; some do.
_OPAQUE_TAGS = frozenset({"script", "style"})


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._opaque = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _OPAQUE_TAGS:
            self._opaque += 1
        elif tag in _BLOCK_TAGS:
            self.parts.append(" ")

    def handle_endtag(self, tag: str) -> None:
        if tag in _OPAQUE_TAGS:
            self._opaque = max(0, self._opaque - 1)
        elif tag in _BLOCK_TAGS:
            self.parts.append(" ")

    def handle_data(self, data: str) -> None:
        if not self._opaque:
            self.parts.append(data)


def strip_html(raw: str | None) -> str | None:
    """A feed teaser as plain text: no markup, entities resolved, whitespace collapsed.

    ``None`` in, ``None`` out. Text that is only markup collapses to ``None`` rather than to
    an empty string, so "the publisher sent no teaser" and "the teaser was an empty div" are
    the same absence downstream instead of two.

    Idempotent — stripping already-plain text is a no-op, which is what makes re-running the
    stage on a record safe.
    """
    if raw is None:
        return None
    parser = _TextExtractor()
    parser.feed(raw)
    parser.close()
    # \s matches non-breaking spaces here: the pattern is a str, so re is in Unicode mode and
    # the \xa0 that &nbsp; becomes is whitespace. Without that, entity-heavy teasers keep a
    # character that reads as a space and does not split as one.
    text = re.sub(r"\s+", " ", "".join(parser.parts)).strip()
    return text or None


# --------------------------------------------------------------------------- FR-I7

#: The languages the detector is allowed to answer with.
#:
#: Restricting it is not tidiness — it is the single biggest lever on short-text accuracy,
#: measured. Against all 75 languages in low-accuracy mode, 8.9% of English title-only items
#: were called foreign; restricted to this set, none were. Everything here is Latin-script,
#: because non-Latin text never reaches the detector — ``_is_latin_script`` has already
#: settled it, deterministically and without a model.
#:
#: The set is wider than the roster needs because an out-of-set Latin language is forced onto
#: whichever member is nearest, and Italian landed on ENGLISH before Italian was in the list.
_CANDIDATE_LANGUAGES = (
    Language.ENGLISH,
    Language.SPANISH,
    Language.FRENCH,
    Language.GERMAN,
    Language.PORTUGUESE,
    Language.ITALIAN,
    Language.DUTCH,
    Language.CATALAN,
)

ENGLISH = "en"

#: Below this share of Latin letters, the text is not in a Latin-script language.
_LATIN_SHARE_REQUIRED = 0.5


@lru_cache(maxsize=1)
def _detector() -> LanguageDetector:
    """Built on first use, not at import.

    Importing this module must not cost 20 MB of resident memory in a process that only
    wanted ``strip_html`` — the test suite, the OpenAPI generator and the import-boundary
    check all import broadly and use narrowly.
    """
    return LanguageDetectorBuilder.from_languages(*_CANDIDATE_LANGUAGES).build()


def _is_latin_script(text: str) -> bool:
    """Whether the letters in ``text`` are predominantly Latin.

    A deterministic gate in front of the statistical one, and it is load-bearing rather than
    an optimisation. A detector restricted to Latin-script languages answers *no opinion* for
    Arabic, Chinese, Russian and Hindi — and "no opinion" is exactly what the drop rule below
    treats as a reason to keep. Without this, every non-Latin article would be kept as
    possibly-English. Script is a fact about the bytes, so it needs no model to settle.

    Text with no letters at all — a headline of digits and punctuation — is not evidence of a
    foreign language, so it passes.
    """
    letters = [char for char in text if char.isalpha()]
    if not letters:
        return True
    latin = sum(unicodedata.name(char, "").startswith("LATIN") for char in letters)
    return latin / len(letters) >= _LATIN_SHARE_REQUIRED


@dataclass(frozen=True)
class LanguageVerdict:
    """What we concluded, and whether FR-I7 says to stop.

    ``language`` is ``None`` when nothing could be concluded — which is not the same as
    English, and is stored as NULL rather than guessed.
    """

    language: str | None
    drop: bool


def detect_language(text: str) -> LanguageVerdict:
    """Identify the language, and decide whether FR-I7 drops the article.

    ⚠️ The rule is deliberately asymmetric: **drop only on a confident non-English verdict.**
    The two errors do not cost the same. A wrong drop is permanent and silent — the record
    keeps a ``terminal_reason`` for good, ``owed_stage`` returns nothing for it forever, and
    no un-drop path exists — while a wrong keep merely lets one foreign article through to a
    classifier that will handle it badly. So anything the detector will not commit to is kept.

    Measured on 364 live feed items across four languages: no English item was dropped at any
    length down to title-only, against 0.4% of foreign items kept. Deciding the other way —
    keeping only what is confidently English — would have deleted real articles, because a
    headline like "Zelensky Macron Starmer" is English at 0.38 confidence and "Markets fall"
    at 0.59.
    """
    if not text.strip():
        return LanguageVerdict(language=None, drop=False)

    if not _is_latin_script(text):
        return LanguageVerdict(language=None, drop=True)

    confidences = _detector().compute_language_confidence_values(text)
    if not confidences or confidences[0].value == 0.0:
        return LanguageVerdict(language=None, drop=False)

    language = confidences[0].language.iso_code_639_1.name.lower()
    return LanguageVerdict(language=language, drop=language != ENGLISH)


def language_input(title: str, lede: str | None) -> str:
    """The text FR-I7 is decided on: the title, plus the teaser when there is one.

    Both, because two of the five publishers we can currently read ship no teaser at all and
    the title is then the entire evidence — and a teaser more than doubles the median token
    count, which is most of the accuracy at these lengths.
    """
    return f"{title} {lede}".strip() if lede else title.strip()
