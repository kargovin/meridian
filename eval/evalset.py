"""Versioned, immutable evaluation sets.

A set is a directory holding ``rows.jsonl`` and ``manifest.json``, addressed by a name that
includes its version (``classification/v2``). It is never edited in place — a change
produces a new version.

⚠ **This module holds classification's row format, not a universal one.** A dedup row is a
*pair* of articles, a clustering row is a whole day's articles, a faithfulness row is a
summary and its sources — none of them is a ``ClassificationRow``, and none should be bent
into one. What generalises to those tasks is the manifest-and-hash pattern around the rows:
address by version, hash the bytes on disk, refuse on disagreement. When the second task
arrives, factor that envelope out against two real examples rather than guessing now which
parts of this shape were the general ones.

Two properties do the work:

* **The rows carry their own text.** A set storing article ids and resolving them against
  the database at run time is not frozen at all: the same file, at the same commit, under
  the same config, scores differently once a column fills in. Nothing errors, because
  nobody edited anything.
* **The manifest is checked on every load.** The hash covers ``rows.jsonl`` as bytes on
  disk, not a re-serialization of the parsed rows — hashing your own output means a change
  to the serializer changes the hash of a file nobody touched.

A hash that disagrees with the manifest raises. It is not a warning: a run against an
unknown set produces a number that will sit in a table next to numbers it cannot be
compared with.

Two more refusals keep a set honest about how it was made (rubric §7.5, the drop rate). Both
read what ``eval/annotate.py cut`` writes:

* **A row whose gold is ``unsure`` refuses the set.** ``unsure`` is an unmade decision, not
  a label. Excluded quietly, it takes the hardest articles out of both KR3 denominators
  and the number rises with nothing failing.
* **A manifest without drop counts refuses the set.** An article adjudication could not
  resolve leaves the set, so the harness cannot see it — only the count the cut recorded.
  A set with no counts would score as if nothing had been dropped.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from meridian_contract.taxonomy import REAL_TOPICS, Topic

#: An annotator could not place the article. Deliberately *not* a ``Topic`` member: it is
#: something an annotator says, not something a classifier can emit, and a taxonomy that
#: could express it would let it reach the database and the wire. It never reaches a
#: finished set: ``load`` refuses a row that still carries it.
UNSURE: Final = "unsure"

#: Why an article left the classification set (rubric §7.5). A closed set, reported apart:
#: a rise in the first says two topic definitions are blurring, a rise in the second says
#: acquisition is not delivering readable articles. Summed, they cancel.
CLASSIFICATION_DROP_CAUSES: Final = ("genuine_disagreement", "unrecoverable_record")

#: A drop rate above this, on either cause, is tagged on the run for review. A chosen
#: review threshold (rubric §7.5), not a target, and never a failed run.
DROP_RATE_REVIEW_TRIGGER: Final = 0.03

ROWS_FILE: Final = "rows.jsonl"
RAW_LABELS_FILE: Final = "raw_labels.jsonl"
MANIFEST_FILE: Final = "manifest.json"

#: Sets ship with the harness rather than being fetched, so a run needs no network.
DEFAULT_ROOT: Final = Path(__file__).parent / "sets"


class EvalSetError(Exception):
    """A set could not be loaded, or does not match its manifest."""


def jsonl_lines(text: str) -> list[str]:
    """Split JSONL into its lines, on ``\\n`` alone.

    ⚠ Not ``str.splitlines()``: it also splits on U+2028, U+2029 and U+0085, which JSON
    written with ``ensure_ascii=False`` carries raw inside strings, and which scraped news
    text does contain. It would cut one valid row into two invalid halves.
    """
    return text.split("\n")


def _read_text(path: Path, *, name: str) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise EvalSetError(f"{name}: {path.name} is not UTF-8 — {exc}") from exc


@dataclass(frozen=True, slots=True)
class ClassificationRow:
    """One labelled article.

    ``lede`` and ``body`` are ``None`` rather than ``""`` when absent, so presence is a
    single unambiguous condition. A body is absent for every headline-only publisher, so
    many rows hold a title and a lede of roughly a hundred characters.
    """

    id: str
    title: str
    body: str | None
    gold: Topic
    lede: str | None = None
    publisher: str | None = None

    @property
    def has_body(self) -> bool:
        """Derived, never stored.

        A recorded flag can disagree with the text beside it, and then the set reports
        something no longer true of itself.
        """
        return self.body is not None

    @property
    def text(self) -> str:
        """The full text the set makes available for this row.

        A predictor may choose to read less (title only, say); this is the ceiling, and it
        is what run-level text statistics are measured over — a property of the set, not of
        whichever model happened to run against it.
        """
        parts = (self.title, self.lede, self.body)
        return "\n\n".join(part for part in parts if part is not None)

    @property
    def gold_is_real_topic(self) -> bool:
        """True when a human placed this article in a topic a reader can browse.

        The population both KR3 numbers are computed over. ``Other`` is excluded because it
        is not a topic anyone follows.
        """
        # The isinstance is load-bearing beyond the membership test: a row built by hand
        # can carry a bare string, and one equal to a topic's value satisfies `in` against
        # a StrEnum.
        return isinstance(self.gold, Topic) and self.gold in REAL_TOPICS


@dataclass(frozen=True, slots=True)
class EvalSet:
    """A loaded set, verified against its manifest."""

    name: str
    rows: tuple[ClassificationRow, ...]
    sha256: str
    #: Articles adjudication could not resolve, by cause, from the manifest. They are not
    #: in ``rows``; this is the only trace of them the harness has.
    drops: dict[str, int]

    def __len__(self) -> int:
        return len(self.rows)

    @property
    def candidates(self) -> int:
        """The articles the set was cut from: every row, plus every drop."""
        return len(self.rows) + sum(self.drops.values())

    @property
    def drop_rates(self) -> dict[str, float]:
        """Each cause's drops as a share of the candidates — the rate rubric §7.5 reports."""
        return {cause: count / self.candidates for cause, count in self.drops.items()}

    @property
    def drop_rates_for_review(self) -> tuple[str, ...]:
        """The causes whose rate is above the review trigger. Reported, never asserted."""
        return tuple(
            cause for cause, rate in self.drop_rates.items() if rate > DROP_RATE_REVIEW_TRIGGER
        )

    @property
    def scorable(self) -> tuple[ClassificationRow, ...]:
        """The rows whose gold label is a real topic."""
        return tuple(row for row in self.rows if row.gold_is_real_topic)


def _parse_gold(raw: object, *, where: str) -> Topic:
    if not isinstance(raw, str):
        raise EvalSetError(f"{where}: 'gold' must be a string, got {type(raw).__name__}")
    try:
        return Topic(raw)
    except ValueError:
        known = ", ".join(sorted(t.value for t in Topic))
        raise EvalSetError(
            f"{where}: unknown gold label {raw!r}. Expected one of: {known}"
        ) from None


def _optional_text(obj: dict[str, object], key: str, *, where: str) -> str | None:
    value = obj.get(key)
    if value is not None and not isinstance(value, str):
        raise EvalSetError(f"{where}: '{key}' must be a string or null")
    # Whitespace-only is absence. Otherwise `has_body` counts a row the model learned
    # nothing from, and run-level text statistics report coverage we do not have.
    if value is not None and not value.strip():
        return None
    return value


def _is_unsure(line: str) -> bool:
    try:
        obj = json.loads(line)
    except json.JSONDecodeError:
        return False
    return isinstance(obj, dict) and obj.get("gold") == UNSURE


def _parse_drops(manifest: dict[str, object], *, name: str) -> dict[str, int]:
    drops = manifest.get("drops")
    if not isinstance(drops, dict):
        raise EvalSetError(
            f"{name}: the manifest records no drop counts. A set without them would score "
            "as if adjudication had dropped nothing; cut it with eval/annotate.py."
        )
    expected = set(CLASSIFICATION_DROP_CAUSES)
    if set(drops) != expected:
        raise EvalSetError(
            f"{name}: the manifest's drop causes are {sorted(drops)}, expected "
            f"{sorted(expected)}. Every cause is recorded, zero included."
        )
    for cause, count in drops.items():
        if not isinstance(count, int) or isinstance(count, bool) or count < 0:
            raise EvalSetError(f"{name}: drop count for {cause!r} must be a whole number")
    return {cause: drops[cause] for cause in CLASSIFICATION_DROP_CAUSES}


def _check_raw_labels(
    path: Path, *, golds: dict[str, Topic], drops: dict[str, int], name: str
) -> None:
    """Hold the manifest and the rows to the per-item record the cut wrote beside them.

    Each file's hash proves only that the file is the bytes its manifest names, and a hand
    edit can re-hash. So the files are also checked against each other: drops by cause
    against the manifest, and every kept item, with its gold, against the rows.
    """
    kept: dict[str, object] = {}
    seen: set[str] = set()
    dropped = dict.fromkeys(drops, 0)
    for number, line in enumerate(jsonl_lines(_read_text(path, name=name)), start=1):
        if not line.strip():
            continue
        where = f"{name} {RAW_LABELS_FILE} line {number}"
        try:
            obj = json.loads(line)
        except json.JSONDecodeError as exc:
            raise EvalSetError(f"{where}: not valid JSON — {exc}") from exc
        if not isinstance(obj, dict) or not isinstance(obj.get("id"), str):
            raise EvalSetError(f"{where}: expected an object with a string 'id'")
        item_id = obj["id"]
        if item_id in seen:
            raise EvalSetError(f"{where}: {item_id!r} is recorded twice")
        seen.add(item_id)
        outcome, cause = obj.get("outcome"), obj.get("cause")
        if outcome == "dropped":
            if not isinstance(cause, str) or cause not in dropped:
                raise EvalSetError(f"{where}: dropped with unknown cause {cause!r}")
            dropped[cause] += 1
        elif outcome in ("agreed", "adjudicated"):
            kept[item_id] = obj.get("gold")
        else:
            raise EvalSetError(f"{where}: outcome {outcome!r} is not a finished item's")
    if dropped != drops:
        raise EvalSetError(
            f"{name}: the manifest records drops {drops}, its {RAW_LABELS_FILE} records {dropped}"
        )
    if kept.keys() != golds.keys():
        differ = sorted(kept.keys() ^ golds.keys())
        raise EvalSetError(
            f"{name}: the rows and the items {RAW_LABELS_FILE} records as kept differ: "
            f"{', '.join(differ[:5])}{' …' if len(differ) > 5 else ''}"
        )
    changed = sorted(i for i, gold in golds.items() if kept[i] != gold.value)
    if changed:
        raise EvalSetError(
            f"{name}: gold in {ROWS_FILE} differs from {RAW_LABELS_FILE} for "
            f"{', '.join(changed[:5])}{' …' if len(changed) > 5 else ''}"
        )


def _parse_row(line: str, *, where: str) -> ClassificationRow:
    try:
        obj = json.loads(line)
    except json.JSONDecodeError as exc:
        raise EvalSetError(f"{where}: not valid JSON — {exc}") from exc
    if not isinstance(obj, dict):
        raise EvalSetError(f"{where}: expected a JSON object, got {type(obj).__name__}")

    missing = {"id", "title", "gold"} - obj.keys()
    if missing:
        raise EvalSetError(f"{where}: missing {', '.join(sorted(missing))}")

    publisher = obj.get("publisher")
    if publisher is not None and not isinstance(publisher, str):
        raise EvalSetError(f"{where}: 'publisher' must be a string or null")

    return ClassificationRow(
        id=str(obj["id"]),
        title=str(obj["title"]),
        lede=_optional_text(obj, "lede", where=where),
        body=_optional_text(obj, "body", where=where),
        publisher=publisher,
        gold=_parse_gold(obj["gold"], where=where),
    )


def load(name: str, *, root: Path | None = None) -> EvalSet:
    """Load and verify the set called ``name`` (for example ``classification/v2``).

    Raises ``EvalSetError`` if the directory is missing, the manifest disagrees with the
    rows or the raw labels, the manifest records no drop counts, any row is malformed, or
    any row is still ``unsure``.
    """
    base = (root if root is not None else DEFAULT_ROOT) / name
    rows_path = base / ROWS_FILE
    raw_labels_path = base / RAW_LABELS_FILE
    manifest_path = base / MANIFEST_FILE

    if not manifest_path.is_file():
        raise EvalSetError(f"{name}: {manifest_path} does not exist")
    for path in (rows_path, raw_labels_path):
        if not path.is_file():
            # A real set's rows are not committed; only its manifest is.
            raise EvalSetError(
                f"{name}: {path} does not exist. If the manifest is here, "
                f"`python -m eval.annotate fetch {name}` restores the set from MLflow."
            )

    raw = rows_path.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()

    try:
        manifest = json.loads(_read_text(manifest_path, name=name))
    except json.JSONDecodeError as exc:
        raise EvalSetError(f"{name}: {MANIFEST_FILE} is not valid JSON — {exc}") from exc
    if not isinstance(manifest, dict):
        raise EvalSetError(f"{name}: {MANIFEST_FILE} must be a JSON object")

    declared_name = manifest.get("set")
    if declared_name != name:
        # Catches a directory copied to a new version without editing the manifest, which
        # otherwise verifies perfectly and reports itself under the wrong name.
        raise EvalSetError(f"{name}: manifest declares set {declared_name!r}")

    if manifest.get("sha256") != digest:
        raise EvalSetError(
            f"{name}: {ROWS_FILE} does not match the manifest "
            f"(manifest {manifest.get('sha256')}, file {digest}). "
            "A set is immutable; edit it by cutting a new version."
        )

    # The raw labels are what agreement is computed on, so they are held to the same
    # immutability as the rows they sit beside.
    raw_labels_digest = digest_of(raw_labels_path)
    if manifest.get("raw_labels_sha256") != raw_labels_digest:
        raise EvalSetError(
            f"{name}: {RAW_LABELS_FILE} does not match the manifest "
            f"(manifest {manifest.get('raw_labels_sha256')}, file {raw_labels_digest})."
        )

    drops = _parse_drops(manifest, name=name)

    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise EvalSetError(f"{name}: {ROWS_FILE} is not UTF-8 — {exc}") from exc
    lines = [line for line in jsonl_lines(text) if line.strip()]
    # Counted before any row is parsed, so the refusal names every unmade decision rather
    # than stopping at the first.
    unsure = sum(1 for line in lines if _is_unsure(line))
    if unsure:
        raise EvalSetError(
            f"{name}: {unsure} row(s) still carry gold {UNSURE!r}. An unadjudicated set is "
            "refused, not scored: excluding them quietly takes the hardest articles out of "
            "both KR3 numbers. Adjudicate them and cut a new version."
        )

    rows = tuple(
        _parse_row(line, where=f"{name} line {number}")
        for number, line in enumerate(lines, start=1)
    )

    if manifest.get("row_count") != len(rows):
        raise EvalSetError(
            f"{name}: manifest declares {manifest.get('row_count')} rows, found {len(rows)}"
        )

    # The cut writes both. Disagreeing, one of them has been edited, and the drop rate is
    # computed against a candidate count nobody recorded.
    candidates = len(rows) + sum(drops.values())
    if manifest.get("candidates") != candidates:
        raise EvalSetError(
            f"{name}: manifest declares {manifest.get('candidates')} candidates, but its rows "
            f"and drops add up to {candidates}"
        )

    if not rows:
        # Nothing to score, and a drop rate over zero candidates is a division by zero.
        raise EvalSetError(f"{name}: the set holds no rows")

    ids = [row.id for row in rows]
    if len(set(ids)) != len(ids):
        raise EvalSetError(f"{name}: duplicate row ids")

    _check_raw_labels(
        raw_labels_path, golds={row.id: row.gold for row in rows}, drops=drops, name=name
    )

    return EvalSet(name=name, rows=rows, sha256=digest, drops=drops)


def digest_of(rows_path: Path) -> str:
    """The hash a manifest should carry for ``rows_path``. Used when cutting a set."""
    return hashlib.sha256(rows_path.read_bytes()).hexdigest()
