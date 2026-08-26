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

    # `attrs` is unused: this is HTMLParser's signature, which it calls positionally.
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

    ⚠️ **Not a fixpoint, and the exception matters for replay.** Stripping already-plain text
    is a no-op for ordinary prose, but not for text that *describes* markup: a teaser
    containing ``&lt;redacted&gt;`` unescapes to ``<redacted>`` on the first pass and is read
    as a tag and deleted on the second. ``&amp;amp;`` likewise decays one level per pass.

    That is safe today only because the stage rewrites ``lede`` in place and the work row is
    deleted, so nothing re-runs it. It stops being safe the moment a replay rewinds
    ``pipeline_state`` and re-enqueues (RFC §9) — a second pass silently deletes content. The
    fix is either a fixpoint or keeping the publisher's original alongside; both are larger
    than this stage and belong with the work-queue story that owns replay.
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
#: Latin-script only, and **not to keep the model small** — ``_is_latin_script`` runs first and
#: returns before the detector is built, so this only ever receives Latin-script text. A
#: non-Latin member here would be unreachable. (Adding eight of them costs 0.1 MB resident,
#: measured; the 292 MB shared object is compiled with all 75 either way. Loading all 75 costs
#: 86 MB and 6x the latency, and in low-accuracy mode calls 8.9% of English title-only items
#: foreign, against none for this set.)
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

#: How sure the detector must be before FR-I7 deletes an article for good.
#:
#: ⚠️ Without this the rule is "drop on any non-English top-1", because lingua returns a
#: normalised distribution and the top candidate is never 0.0 — the lowest seen anywhere in the
#: calibration corpus is 0.23. Measured over 479 English and 240 foreign headlines at
#: title-only length, which is what the two publishers that ship no teaser give us:
#:
#:      gate          false drops (permanent)   foreign kept (cheap)
#:      none                  9  = 1.88%                0
#:      >= 0.40               1  = 0.21%                1 = 0.42%
#:      >= 0.50               0                         2 = 0.83%
#:      >= 0.60               0                         5 = 2.08%
#:      >= 0.70               0                        11 = 4.58%
#:
#: 0.60 rather than the first value that reaches zero, because the two populations separate
#: cleanly and the gap is worth standing in the middle of: every false drop observed was
#: between 0.274 and 0.427, while 92.5% of correct foreign calls came in at 0.8 or above and
#: only 2% below 0.6. The cost is bounded by the foreign share of the roster; the benefit
#: applies to every article we ingest.
#:
#: Not a runtime knob today. Making it one follows FR-C2's pattern for the classifier
#: threshold, and there is no operator need until a source mix moves.
_DROP_CONFIDENCE = 0.60


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
    possibly-English.

    ⚠️ **Listing those languages in the detector instead does not work, and the reason is that
    enumeration is open-ended.** Measured: adding eight non-Latin languages still fails to
    identify Thai, Georgian and Amharic, and loading *all seventy-five* still fails Amharic —
    there is always one more script. This closes the class instead of enumerating its members,
    because script is a fact about the codepoints and needs no model to settle.

    Accented Latin counts as Latin, so Spanish, French, German and Portuguese headlines reach
    the detector rather than exiting here; mixed script goes on the majority.

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
    classifier that will handle it badly. So anything the detector will not commit to is kept,
    and "will not commit to" means ``_DROP_CONFIDENCE``, not merely "returned something".

    Measured over 479 English and 240 foreign headlines: with the gate, no English item is
    dropped at any length down to title-only, against 2.08% of foreign items kept.

    ⚠️ Deciding the other way — keeping only what is *confidently English* — inverts the
    asymmetry and deletes real articles: "Zelensky Macron Starmer" is English at 0.38 and
    "Markets fall" at 0.59. The bar applies to the foreign call, never to the English one.
    """
    if not text.strip():
        return LanguageVerdict(language=None, drop=False)

    if not _is_latin_script(text):
        return LanguageVerdict(language=None, drop=True)

    confidences = _detector().compute_language_confidence_values(text)
    if not confidences or confidences[0].value == 0.0:
        return LanguageVerdict(language=None, drop=False)

    best = confidences[0]
    language = best.language.iso_code_639_1.name.lower()
    if language == ENGLISH:
        return LanguageVerdict(language=language, drop=False)

    # Foreign, but not confidently enough to delete an article over.
    #
    # ⚠️ The language is discarded rather than recorded here. Below the bar we are declining to
    # conclude, and writing "de" onto an English sport headline would store a wrong fact for a
    # future reader to trust. Detection is pure, so anyone debugging can re-run it; NULL is the
    # honest answer and the recoverable one.
    if best.value < _DROP_CONFIDENCE:
        return LanguageVerdict(language=None, drop=False)

    return LanguageVerdict(language=language, drop=True)


def language_input(title: str, lede: str | None) -> str:
    """The text FR-I7 is decided on: the title, plus the teaser when there is one.

    Both, because two of the five publishers we can currently read ship no teaser at all and
    the title is then the entire evidence — and a teaser more than doubles the median token
    count, which is most of the accuracy at these lengths.
    """
    return f"{title} {lede}".strip() if lede else title.strip()
