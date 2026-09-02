"""Versioned, immutable evaluation sets.

A set is a directory holding ``rows.jsonl`` and ``manifest.json``, addressed by a name that
includes its version (``classification/v1``). It is never edited in place — a change
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
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Literal

from meridian_contract.taxonomy import REAL_TOPICS, Topic

#: An annotator could not place the article. Deliberately *not* a ``Topic`` member: it is
#: something a human says, not something a classifier can emit, and a taxonomy that could
#: express it would let it reach the database and the wire. It is excluded from both the
#: numerator and the denominator of every KR3 number (PRD §9).
UNSURE: Final = "unsure"

type GoldLabel = Topic | Literal["unsure"]

ROWS_FILE: Final = "rows.jsonl"
MANIFEST_FILE: Final = "manifest.json"

#: Sets ship with the harness rather than being fetched, so a run needs no network.
DEFAULT_ROOT: Final = Path(__file__).parent / "sets"


class EvalSetError(Exception):
    """A set could not be loaded, or does not match its manifest."""


@dataclass(frozen=True, slots=True)
class ClassificationRow:
    """One labelled article.

    ``body`` is ``None`` rather than ``""`` when absent, so presence is a single unambiguous
    condition. It is absent for most of the corpus until tier-3 acquisition lands: no
    mainstream source ships full article text in its feed, so a set cut today holds
    headlines and a lede of roughly a hundred characters.
    """

    id: str
    title: str
    body: str | None
    gold: GoldLabel

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
        return f"{self.title}\n\n{self.body}" if self.body is not None else self.title

    @property
    def gold_is_real_topic(self) -> bool:
        """True when a human placed this article in a topic a reader can browse.

        The population both KR3 numbers are computed over. ``Other`` is excluded because it
        is not a topic anyone follows, and ``unsure`` because it is not an answer.
        """
        # The isinstance is load-bearing beyond the membership test: `gold` may hold the
        # UNSURE sentinel, and a bare string equal to a topic's value would otherwise
        # satisfy `in` against a StrEnum.
        return isinstance(self.gold, Topic) and self.gold in REAL_TOPICS


@dataclass(frozen=True, slots=True)
class EvalSet:
    """A loaded set, verified against its manifest."""

    name: str
    rows: tuple[ClassificationRow, ...]
    sha256: str

    def __len__(self) -> int:
        return len(self.rows)

    @property
    def scorable(self) -> tuple[ClassificationRow, ...]:
        """The rows whose gold label is a real topic."""
        return tuple(row for row in self.rows if row.gold_is_real_topic)


def _parse_gold(raw: object, *, where: str) -> GoldLabel:
    if not isinstance(raw, str):
        raise EvalSetError(f"{where}: 'gold' must be a string, got {type(raw).__name__}")
    if raw == UNSURE:
        return UNSURE
    try:
        return Topic(raw)
    except ValueError:
        known = ", ".join(sorted(t.value for t in Topic))
        raise EvalSetError(
            f"{where}: unknown gold label {raw!r}. Expected {UNSURE!r} or one of: {known}"
        ) from None


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

    body = obj.get("body")
    if body is not None and not isinstance(body, str):
        raise EvalSetError(f"{where}: 'body' must be a string or null")
    # Whitespace-only is absence. Otherwise `has_body` counts a row the model learned
    # nothing from, and run-level text statistics report coverage we do not have.
    if body is not None and not body.strip():
        body = None

    return ClassificationRow(
        id=str(obj["id"]),
        title=str(obj["title"]),
        body=body,
        gold=_parse_gold(obj["gold"], where=where),
    )


def load(name: str, *, root: Path | None = None) -> EvalSet:
    """Load and verify the set called ``name`` (for example ``classification/v1``).

    Raises ``EvalSetError`` if the directory is missing, the manifest disagrees with the
    rows, or any row is malformed.
    """
    base = (root if root is not None else DEFAULT_ROOT) / name
    rows_path = base / ROWS_FILE
    manifest_path = base / MANIFEST_FILE

    for path in (rows_path, manifest_path):
        if not path.is_file():
            raise EvalSetError(f"{name}: {path} does not exist")

    raw = rows_path.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()

    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise EvalSetError(f"{name}: {MANIFEST_FILE} is not valid JSON — {exc}") from exc

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

    lines = [line for line in raw.decode("utf-8").splitlines() if line.strip()]
    rows = tuple(
        _parse_row(line, where=f"{name} line {number}")
        for number, line in enumerate(lines, start=1)
    )

    if manifest.get("row_count") != len(rows):
        raise EvalSetError(
            f"{name}: manifest declares {manifest.get('row_count')} rows, found {len(rows)}"
        )

    ids = [row.id for row in rows]
    if len(set(ids)) != len(ids):
        raise EvalSetError(f"{name}: duplicate row ids")

    return EvalSet(name=name, rows=rows, sha256=digest)


def digest_of(rows_path: Path) -> str:
    """The hash a manifest should carry for ``rows_path``. Used when cutting a set."""
    return hashlib.sha256(rows_path.read_bytes()).hexdigest()
