"""The annotation lifecycle: from a labelled pool to a finished, versioned eval set.

This module checks and cuts. It never labels and never adjudicates: annotators (agents or
people) write label files, the adjudicator writes rulings, and this reads them, says what
is left to do, and — once nothing is — cuts the set. ``eval/ANNOTATION_FORMAT.md`` is the
reference for the files; ``eval/annotate.py`` is the command line over this module.

A workspace is a directory:

    workspace.json       format, task, and the task's own keys
    pool.jsonl           the items, carrying their own text; ``id`` is never an article id
    participants.json    who labels and who adjudicates
    assignments.jsonl    every item once, with the annotators who label it
    labels/<who>.jsonl   one file per annotator: a label, or ``unsure`` with its reason
    adjudication.jsonl   rulings: a gold label, or a drop with its cause

Each item's state is derived from the files every time, so there is no state to keep in
step with them:

    unlabelled        an assigned annotator has not labelled it yet
    awaiting_ruling   contested — an annotator said ``unsure``, or the annotators disagree —
                      and not yet ruled on
    agreed            uncontested and not ruled on; the annotators' label is gold
    adjudicated       ruled to a gold label
    dropped           ruled out of the set, with a cause

A ruling on an uncontested item is an **override**. It is allowed — the adjudicator may see
what every annotator missed — and counted, because a set in which the adjudicator rewrote
many agreed labels is measuring the adjudicator rather than the annotators.

The drop rate has to be captured here (rubric §7.5): a dropped item is not in the set, so
the harness can never count it. The cut records drops by cause in the manifest, and the
harness reads them from there on every run.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Final, Protocol

from meridian_contract.taxonomy import TAXONOMY_VERSION, Topic

from eval.evalset import (
    CLASSIFICATION_DROP_CAUSES,
    MANIFEST_FILE,
    RAW_LABELS_FILE,
    ROWS_FILE,
    UNSURE,
    jsonl_lines,
)

#: The version of the workspace layout this module reads. ``workspace.json`` must name it.
FORMAT: Final = 1

WORKSPACE_FILE: Final = "workspace.json"
POOL_FILE: Final = "pool.jsonl"
PARTICIPANTS_FILE: Final = "participants.json"
ASSIGNMENTS_FILE: Final = "assignments.jsonl"
LABELS_DIR: Final = "labels"
ADJUDICATION_FILE: Final = "adjudication.jsonl"

_PARTICIPANT_ID = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}")
_ITEM_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")
_SHA256 = re.compile(r"[0-9a-f]{64}")
_SET_NAME = re.compile(r"([a-z_]+)/v[1-9][0-9]*")


class WorkspaceError(Exception):
    """The workspace's files are malformed or inconsistent. Carries every problem found."""

    def __init__(self, problems: Sequence[str]) -> None:
        self.problems = tuple(problems)
        super().__init__("\n".join(self.problems))


class CutRefused(Exception):
    """The workspace is valid but the set cannot be cut from it yet, or not to that name."""


class State(StrEnum):
    UNLABELLED = "unlabelled"
    AWAITING_RULING = "awaiting_ruling"
    AGREED = "agreed"
    ADJUDICATED = "adjudicated"
    DROPPED = "dropped"


@dataclass(frozen=True, slots=True)
class Participant:
    """An annotator or an adjudicator.

    An agent declares the model it ran and a hash of the instructions it was given. Two
    agents sharing both are one annotator run twice: their agreement measures nothing.
    """

    id: str
    kind: str
    model: str | None = None
    instructions_sha256: str | None = None

    def as_json(self) -> dict[str, object]:
        out: dict[str, object] = {"id": self.id, "kind": self.kind}
        if self.kind == "agent":
            out["model"] = self.model
            out["instructions_sha256"] = self.instructions_sha256
        return out


@dataclass(frozen=True, slots=True)
class Label:
    """One annotator's answer for one item, kept exactly as given."""

    annotator: str
    value: str
    unsure_reason: str | None
    detail: object

    @property
    def is_unsure(self) -> bool:
        return self.value == UNSURE

    def as_json(self) -> dict[str, object]:
        return {
            "annotator": self.annotator,
            "label": self.value,
            "unsure_reason": self.unsure_reason,
            "detail": self.detail,
        }


@dataclass(frozen=True, slots=True)
class Ruling:
    """The adjudicator's decision: a gold label, or a drop with its cause."""

    by: str
    gold: str | None
    cause: str | None
    note: str | None


@dataclass(frozen=True, slots=True)
class Item:
    """One item and everything recorded about it, with its state derived."""

    id: str
    #: The task's row fields, as the cut set will carry them.
    fields: Mapping[str, object]
    #: Where the item came from, for the adjudicator to open the source. Never cut into the
    #: set: a set carries its own text, and an origin is a way back to the database.
    origin: object
    annotators: tuple[str, ...]
    labels: tuple[Label, ...]
    ruling: Ruling | None
    contested: bool

    @property
    def state(self) -> State:
        # Labelling comes first: until every assigned annotator has answered, an item is
        # neither contested nor agreed, whatever has been ruled.
        if len(self.labels) < len(self.annotators):
            return State.UNLABELLED
        if self.ruling is not None:
            return State.DROPPED if self.ruling.cause is not None else State.ADJUDICATED
        return State.AWAITING_RULING if self.contested else State.AGREED

    @property
    def override(self) -> bool:
        """A ruling that changes the outcome of an item every annotator agreed on: a drop, or
        a gold label other than the agreed one. A ruling that repeats the agreed label is a
        confirmation and changes nothing, so it is not counted."""
        if self.ruling is None or self.contested or self.state is State.UNLABELLED:
            return False
        # A drop carries no gold, so it always differs from the agreed label.
        return self.ruling.gold != self.labels[0].value

    @property
    def gold(self) -> str | None:
        state = self.state
        if state is State.AGREED:
            return self.labels[0].value
        if state is State.ADJUDICATED:
            assert self.ruling is not None
            return self.ruling.gold
        return None


class Problems:
    """Collects every problem in a workspace, so one check reports all of them."""

    def __init__(self) -> None:
        self.found: list[str] = []

    def add(self, where: str, message: str) -> None:
        self.found.append(f"{where}: {message}")


class Task(Protocol):
    """What differs between annotation tasks. The lifecycle around it is shared.

    Each task owns its row format, its label vocabulary, its ``unsure`` reasons and its
    closed set of drop causes. Faithfulness (Phase 1.2 §7) is the second task; its rows
    are recorded per claim, so it is not a classification row bent to fit.
    """

    name: str
    #: The task's own keys in ``workspace.json``, copied into the manifest.
    meta_keys: tuple[str, ...]
    drop_causes: tuple[str, ...]
    unsure_reasons: tuple[str, ...]
    #: The share of items that must be labelled by two annotators or more. Below it,
    #: ``check`` warns: agreement cannot be computed on the sample the guide asks for.
    min_double_share: float

    def check_meta(self, meta: Mapping[str, object], problems: Problems) -> None: ...

    def parse_item(
        self, obj: Mapping[str, object], where: str, problems: Problems
    ) -> dict[str, object] | None:
        """Validate a pool line's task fields. Returns the fields a cut row carries."""
        ...

    def parse_label(self, value: object, where: str, problems: Problems) -> str | None:
        """Validate an annotator's label. ``unsure`` is handled by the lifecycle."""
        ...

    def parse_detail(self, value: object, where: str, problems: Problems) -> object: ...

    def parse_gold(self, value: object, where: str, problems: Problems) -> str | None:
        """Validate an adjudicated gold label. ``unsure`` is never one."""
        ...

    def agree(self, labels: Sequence[Label]) -> bool:
        """Whether a complete set of labels, none of them unsure, needs no ruling."""
        ...


def _optional_text(
    obj: Mapping[str, object], key: str, where: str, problems: Problems
) -> str | None:
    value = obj.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        problems.add(where, f"'{key}' must be a string or null")
        return None
    # Whitespace-only is absence, the same reading the harness gives a set row.
    return value if value.strip() else None


class ClassificationTask:
    """Topic labelling (Phase 1.1, the labelling rubric)."""

    name: str = "classification"
    meta_keys: tuple[str, ...] = ("taxonomy", "rubric_rev")
    drop_causes: tuple[str, ...] = CLASSIFICATION_DROP_CAUSES
    #: The three occasions rubric §7.2 gives for marking ``unsure``.
    unsure_reasons: tuple[str, ...] = (
        "collision_without_rule",
        "rule_does_not_fit",
        "unrecoverable_record",
    )
    #: Rubric §8.1: at least 10% of the set is labelled independently by two annotators.
    min_double_share: float = 0.10

    _ITEM_KEYS = frozenset({"id", "title", "lede", "body", "publisher", "origin"})

    def check_meta(self, meta: Mapping[str, object], problems: Problems) -> None:
        if meta.get("taxonomy") != TAXONOMY_VERSION:
            problems.add(
                WORKSPACE_FILE,
                f"'taxonomy' is {meta.get('taxonomy')!r}; this harness labels against "
                f"{TAXONOMY_VERSION!r}",
            )
        rev = meta.get("rubric_rev")
        if not isinstance(rev, int) or isinstance(rev, bool) or rev < 1:
            problems.add(WORKSPACE_FILE, "'rubric_rev' must be the rubric revision, a whole number")

    def parse_item(
        self, obj: Mapping[str, object], where: str, problems: Problems
    ) -> dict[str, object] | None:
        unknown = obj.keys() - self._ITEM_KEYS
        if unknown:
            problems.add(where, f"unknown key(s) {', '.join(sorted(unknown))}")
            return None
        ok = True
        for key in ("title", "publisher"):
            value = obj.get(key)
            if not isinstance(value, str) or not value.strip():
                problems.add(where, f"'{key}' must be a non-empty string")
                ok = False
        before = len(problems.found)
        lede = _optional_text(obj, "lede", where, problems)
        body = _optional_text(obj, "body", where, problems)
        if not ok or len(problems.found) > before:
            return None
        return {"title": obj["title"], "lede": lede, "body": body, "publisher": obj["publisher"]}

    def _topic(self, value: object, what: str, where: str, problems: Problems) -> str | None:
        if isinstance(value, str):
            try:
                return str(Topic(value))
            except ValueError:
                pass
        known = ", ".join(sorted(t.value for t in Topic))
        problems.add(where, f"{what} {value!r} is not a topic. Known: {known}")
        return None

    def parse_label(self, value: object, where: str, problems: Problems) -> str | None:
        return self._topic(value, "label", where, problems)

    def parse_detail(self, value: object, where: str, problems: Problems) -> object:
        # Free-form for classification: carried into the raw labels as given, for reading
        # disagreements by rule (rubric §8.2). Faithfulness defines its own.
        if value is not None and not isinstance(value, dict):
            problems.add(where, "'detail' must be a JSON object")
        return value

    def parse_gold(self, value: object, where: str, problems: Problems) -> str | None:
        return self._topic(value, "gold", where, problems)

    def agree(self, labels: Sequence[Label]) -> bool:
        return len({label.value for label in labels}) == 1


TASKS: Final[Mapping[str, Task]] = {"classification": ClassificationTask()}


@dataclass(frozen=True, slots=True)
class Workspace:
    """A checked workspace. Building one validates every file; see ``open_workspace``."""

    path: Path
    task: Task
    meta: Mapping[str, object]
    participants: Mapping[str, Participant]
    items: tuple[Item, ...]

    def in_state(self, state: State) -> tuple[Item, ...]:
        return tuple(item for item in self.items if item.state is state)

    @property
    def ready(self) -> bool:
        """Nothing left to label and nothing left to rule on."""
        return not self.in_state(State.UNLABELLED) and not self.in_state(State.AWAITING_RULING)

    @property
    def drops(self) -> dict[str, int]:
        """Dropped items by cause — every cause, zero included."""
        counts = Counter(
            item.ruling.cause
            for item in self.in_state(State.DROPPED)
            if item.ruling is not None and item.ruling.cause is not None
        )
        return {cause: counts.get(cause, 0) for cause in self.task.drop_causes}

    @property
    def overrides(self) -> int:
        return sum(1 for item in self.items if item.override)

    @property
    def double_labelled(self) -> int:
        return sum(1 for item in self.items if len(item.annotators) >= 2)

    def warnings(self) -> list[str]:
        """Things a cut does not refuse, but a reader of the numbers should know."""
        out: list[str] = []
        by_pair: dict[tuple[str | None, str | None], list[str]] = {}
        for participant in self.participants.values():
            if participant.kind == "agent":
                key = (participant.model, participant.instructions_sha256)
                by_pair.setdefault(key, []).append(participant.id)
        for (model, _), ids in sorted(by_pair.items(), key=lambda kv: kv[1]):
            if len(ids) > 1:
                out.append(
                    f"{', '.join(sorted(ids))} share a model ({model}) and an instructions "
                    "hash: copies of one configuration are not independent annotators, and "
                    "agreement between them measures nothing"
                )
        for item in self.in_state(State.UNLABELLED):
            if item.ruling is not None:
                answered = {label.annotator for label in item.labels}
                owed = ", ".join(a for a in item.annotators if a not in answered)
                # Kept, not refused: an unrecoverable record may be dropped before every label
                # is in. But a ruling made before the labels it rules on was made blind to them.
                out.append(
                    f"{item.id} is ruled on but still owed a label from {owed}; the ruling was "
                    "made without it, and stands whatever it says"
                )
        if self.items:
            share = self.double_labelled / len(self.items)
            if share < self.task.min_double_share:
                out.append(
                    f"{self.double_labelled} of {len(self.items)} items ({share:.1%}) are "
                    f"labelled by two annotators; the {self.task.name} guide asks for at "
                    f"least {self.task.min_double_share:.0%}"
                )
        return out


def _read_json(path: Path, problems: Problems) -> object:
    where = path.name
    if not path.is_file():
        problems.add(where, "missing")
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except UnicodeDecodeError as exc:
        problems.add(where, f"not UTF-8 — {exc}")
        return None
    except json.JSONDecodeError as exc:
        problems.add(where, f"not valid JSON — {exc}")
        return None


def _read_jsonl(
    path: Path, problems: Problems, *, label: str, required: bool = True
) -> list[tuple[str, dict[str, object]]]:
    if not path.is_file():
        if required:
            problems.add(label, "missing")
        return []
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        problems.add(label, f"not UTF-8 — {exc}")
        return []
    out: list[tuple[str, dict[str, object]]] = []
    for number, line in enumerate(jsonl_lines(text), start=1):
        if not line.strip():
            continue
        where = f"{label} line {number}"
        try:
            obj = json.loads(line)
        except json.JSONDecodeError as exc:
            problems.add(where, f"not valid JSON — {exc}")
            continue
        if not isinstance(obj, dict):
            problems.add(where, f"expected a JSON object, got {type(obj).__name__}")
            continue
        out.append((where, obj))
    return out


def _check_keys(
    obj: Mapping[str, object],
    where: str,
    problems: Problems,
    *,
    required: set[str],
    optional: frozenset[str] = frozenset(),
) -> bool:
    missing = required - obj.keys()
    unknown = obj.keys() - required - optional
    if missing:
        problems.add(where, f"missing {', '.join(sorted(missing))}")
    if unknown:
        # Refused rather than ignored: a misspelled key that is dropped means the file says
        # something other than what was meant, and nothing reports it.
        problems.add(where, f"unknown key(s) {', '.join(sorted(unknown))}")
    return not missing and not unknown


def _read_meta(path: Path, problems: Problems) -> tuple[Task | None, dict[str, object]]:
    raw = _read_json(path / WORKSPACE_FILE, problems)
    if raw is None:
        return None, {}
    if not isinstance(raw, dict):
        problems.add(WORKSPACE_FILE, "expected a JSON object")
        return None, {}
    # `type is`, not `==`: True == 1 and 1.0 == 1 in Python, and neither is a format version.
    if type(raw.get("format")) is not int or raw.get("format") != FORMAT:
        problems.add(WORKSPACE_FILE, f"'format' is {raw.get('format')!r}; this tool reads {FORMAT}")
    name = raw.get("task")
    task = TASKS.get(name) if isinstance(name, str) else None
    if task is None:
        problems.add(WORKSPACE_FILE, f"unknown task {name!r}. Known: {', '.join(sorted(TASKS))}")
        return None, raw
    _check_keys(
        raw,
        WORKSPACE_FILE,
        problems,
        required={"format", "task", *task.meta_keys},
        optional=frozenset({"description"}),
    )
    description = raw.get("description")
    if description is not None and not isinstance(description, str):
        problems.add(WORKSPACE_FILE, "'description' must be a string")
    task.check_meta(raw, problems)
    return task, raw


def _read_participants(path: Path, problems: Problems) -> dict[str, Participant]:
    raw = _read_json(path / PARTICIPANTS_FILE, problems)
    if raw is None:
        return {}
    if not isinstance(raw, dict) or not isinstance(raw.get("participants"), list):
        problems.add(PARTICIPANTS_FILE, "expected an object with a 'participants' list")
        return {}
    out: dict[str, Participant] = {}
    for index, entry in enumerate(raw["participants"]):
        where = f"{PARTICIPANTS_FILE} entry {index}"
        if not isinstance(entry, dict):
            problems.add(where, "expected a JSON object")
            continue
        kind = entry.get("kind")
        if kind == "agent":
            ok = _check_keys(
                entry, where, problems, required={"id", "kind", "model", "instructions_sha256"}
            )
        elif kind == "human":
            ok = _check_keys(entry, where, problems, required={"id", "kind"})
        else:
            problems.add(where, f"'kind' must be 'agent' or 'human', got {kind!r}")
            continue
        if not ok:
            continue
        pid = entry["id"]
        if not isinstance(pid, str) or not _PARTICIPANT_ID.fullmatch(pid):
            # It names a file under labels/, so it is held to a filename-safe shape.
            problems.add(where, f"id {pid!r} must be lower-case letters, digits, '-' or '_'")
            continue
        if pid in out:
            problems.add(where, f"duplicate participant {pid!r}")
            continue
        model = entry.get("model")
        digest = entry.get("instructions_sha256")
        if kind == "agent":
            if not isinstance(model, str) or not model.strip():
                problems.add(where, "an agent's 'model' must be a non-empty string")
                continue
            if not isinstance(digest, str) or not _SHA256.fullmatch(digest):
                problems.add(where, "'instructions_sha256' must be 64 lower-case hex digits")
                continue
        out[pid] = Participant(
            id=pid,
            kind=kind,
            model=model if kind == "agent" else None,
            instructions_sha256=digest if kind == "agent" else None,
        )
    return out


def _read_pool(
    path: Path, task: Task, problems: Problems
) -> dict[str, tuple[dict[str, object], object]]:
    out: dict[str, tuple[dict[str, object], object]] = {}
    for where, obj in _read_jsonl(path / POOL_FILE, problems, label=POOL_FILE):
        item_id = obj.get("id")
        if not isinstance(item_id, str) or not _ITEM_ID.fullmatch(item_id):
            problems.add(where, f"'id' {item_id!r} must be a short string without spaces")
            continue
        if item_id.isdigit():
            # An item id that reads as an integer reads as an article id. A set carries its
            # own text and never a key back into the database.
            problems.add(where, f"'id' {item_id!r} looks like an article id; give the item its own")
            continue
        if item_id in out:
            problems.add(where, f"duplicate id {item_id!r}")
            continue
        fields = task.parse_item(obj, where, problems)
        if fields is not None:
            out[item_id] = (fields, obj.get("origin"))
    if not out and not problems.found:
        problems.add(POOL_FILE, "holds no items")
    return out


def _read_assignments(
    path: Path,
    pool: Mapping[str, object],
    participants: Mapping[str, Participant],
    problems: Problems,
) -> dict[str, tuple[str, ...]]:
    out: dict[str, tuple[str, ...]] = {}
    for where, obj in _read_jsonl(path / ASSIGNMENTS_FILE, problems, label=ASSIGNMENTS_FILE):
        if not _check_keys(obj, where, problems, required={"id", "annotators"}):
            continue
        item_id, annotators = obj["id"], obj["annotators"]
        if not isinstance(item_id, str) or item_id not in pool:
            problems.add(where, f"no item {item_id!r} in the pool")
            continue
        if item_id in out:
            problems.add(where, f"{item_id!r} is assigned more than once")
            continue
        if (
            not isinstance(annotators, list)
            or not annotators
            or not all(isinstance(a, str) for a in annotators)
        ):
            problems.add(where, "'annotators' must be a non-empty list of participant ids")
            continue
        if len(set(annotators)) != len(annotators):
            problems.add(where, "an annotator is listed twice")
            continue
        undeclared = [a for a in annotators if a not in participants]
        if undeclared:
            problems.add(where, f"undeclared participant(s) {', '.join(undeclared)}")
            continue
        out[item_id] = tuple(annotators)
    unassigned = sorted(set(pool) - set(out))
    if unassigned:
        shown = ", ".join(unassigned[:5]) + (" …" if len(unassigned) > 5 else "")
        problems.add(ASSIGNMENTS_FILE, f"{len(unassigned)} pool item(s) are not assigned: {shown}")
    return out


def _read_labels(
    path: Path,
    task: Task,
    assignments: Mapping[str, tuple[str, ...]],
    participants: Mapping[str, Participant],
    problems: Problems,
) -> dict[str, dict[str, Label]]:
    """Labels by item, then by annotator."""
    out: dict[str, dict[str, Label]] = {}
    directory = path / LABELS_DIR
    if not directory.exists():
        return out
    if not directory.is_dir():
        problems.add(LABELS_DIR, "must be a directory of <participant>.jsonl files")
        return out
    for file in sorted(directory.iterdir()):
        label_where = f"{LABELS_DIR}/{file.name}"
        if file.suffix != ".jsonl" or not file.is_file():
            problems.add(label_where, "labels/ holds only <participant>.jsonl files")
            continue
        annotator = file.stem
        if annotator not in participants:
            problems.add(label_where, f"no participant {annotator!r} is declared")
            continue
        seen: set[str] = set()
        for where, obj in _read_jsonl(file, problems, label=label_where):
            if not _check_keys(
                obj,
                where,
                problems,
                required={"id", "label"},
                optional=frozenset({"unsure_reason", "detail"}),
            ):
                continue
            item_id = obj["id"]
            if not isinstance(item_id, str) or annotator not in assignments.get(item_id, ()):
                problems.add(where, f"{item_id!r} is not assigned to {annotator}")
                continue
            if item_id in seen:
                # A raw label is kept as given; a second answer for the same item would
                # leave the file saying two things.
                problems.add(where, f"{item_id!r} is labelled twice")
                continue
            seen.add(item_id)
            value = obj["label"]
            reason = obj.get("unsure_reason")
            if value == UNSURE:
                if reason not in task.unsure_reasons:
                    problems.add(
                        where,
                        f"'unsure' needs an 'unsure_reason', one of "
                        f"{', '.join(task.unsure_reasons)}; got {reason!r}",
                    )
                    continue
                parsed: str | None = UNSURE
            else:
                if reason is not None:
                    problems.add(where, "'unsure_reason' is given only with the label 'unsure'")
                    continue
                parsed = task.parse_label(value, where, problems)
            before = len(problems.found)
            detail = task.parse_detail(obj.get("detail"), where, problems)
            if parsed is None or len(problems.found) > before:
                continue
            assert reason is None or isinstance(reason, str)
            out.setdefault(item_id, {})[annotator] = Label(
                annotator=annotator, value=parsed, unsure_reason=reason, detail=detail
            )
    return out


def _read_rulings(
    path: Path,
    task: Task,
    pool: Mapping[str, object],
    participants: Mapping[str, Participant],
    assignments: Mapping[str, tuple[str, ...]],
    problems: Problems,
) -> dict[str, Ruling]:
    out: dict[str, Ruling] = {}
    lines = _read_jsonl(path / ADJUDICATION_FILE, problems, label=ADJUDICATION_FILE, required=False)
    for where, obj in lines:
        outcome = obj.get("outcome")
        if outcome == "gold":
            ok = _check_keys(
                obj,
                where,
                problems,
                required={"id", "by", "outcome", "gold"},
                optional=frozenset({"note"}),
            )
        elif outcome == "drop":
            ok = _check_keys(
                obj,
                where,
                problems,
                required={"id", "by", "outcome", "cause"},
                optional=frozenset({"note"}),
            )
        else:
            problems.add(where, f"'outcome' must be 'gold' or 'drop', got {outcome!r}")
            continue
        if not ok:
            continue
        item_id, by, note = obj["id"], obj["by"], obj.get("note")
        if not isinstance(item_id, str) or item_id not in pool:
            problems.add(where, f"no item {item_id!r} in the pool")
            continue
        if item_id in out:
            problems.add(where, f"{item_id!r} is ruled on twice")
            continue
        if not isinstance(by, str) or by not in participants:
            problems.add(where, f"'by' {by!r} is not a declared participant")
            continue
        # Agents annotate and a person adjudicates (rubric §7.3). An agent's ruling, or an
        # annotator's ruling on an item it labelled, would turn a disagreement into whichever
        # side the ruler was already on, and the set would record it as adjudicated.
        if participants[by].kind != "human":
            problems.add(where, f"'by' {by!r} is an agent; rulings are made by a person")
            continue
        if by in assignments.get(item_id, ()):
            problems.add(where, f"{by!r} labelled {item_id!r} and cannot also rule on it")
            continue
        if note is not None and not isinstance(note, str):
            problems.add(where, "'note' must be a string")
            continue
        if outcome == "gold":
            if obj["gold"] == UNSURE:
                problems.add(where, "'unsure' is never a gold label; rule it, or drop it")
                continue
            gold = task.parse_gold(obj["gold"], where, problems)
            if gold is None:
                continue
            out[item_id] = Ruling(by=by, gold=gold, cause=None, note=note)
        else:
            cause = obj["cause"]
            if cause not in task.drop_causes:
                problems.add(
                    where,
                    f"drop cause {cause!r} is not one of {', '.join(task.drop_causes)}",
                )
                continue
            assert isinstance(cause, str)
            out[item_id] = Ruling(by=by, gold=None, cause=cause, note=note)
    return out


def open_workspace(path: Path) -> Workspace:
    """Read and check every file in the workspace at ``path``.

    Raises ``WorkspaceError`` listing every problem found, not only the first: a check that
    stops at one problem is run once per problem.
    """
    problems = Problems()
    if not path.is_dir():
        raise WorkspaceError([f"{path}: no such workspace directory"])
    task, meta = _read_meta(path, problems)
    participants = _read_participants(path, problems)
    if task is None:
        raise WorkspaceError(problems.found)
    pool = _read_pool(path, task, problems)
    assignments = _read_assignments(path, pool, participants, problems)
    labels = _read_labels(path, task, assignments, participants, problems)
    rulings = _read_rulings(path, task, pool, participants, assignments, problems)
    if problems.found:
        raise WorkspaceError(problems.found)

    items = []
    for item_id in sorted(pool):
        fields, origin = pool[item_id]
        annotators = assignments[item_id]
        given = labels.get(item_id, {})
        # In assignment order, so the raw labels read the same way every cut.
        item_labels = tuple(given[a] for a in annotators if a in given)
        contested = any(label.is_unsure for label in item_labels) or not task.agree(item_labels)
        items.append(
            Item(
                id=item_id,
                fields=fields,
                origin=origin,
                annotators=annotators,
                labels=item_labels,
                ruling=rulings.get(item_id),
                contested=contested,
            )
        )
    return Workspace(path=path, task=task, meta=meta, participants=participants, items=tuple(items))


# --------------------------------------------------------------------------- the cut


def _jsonl(rows: Sequence[Mapping[str, object]]) -> bytes:
    """One canonical line per row. The hash is taken over exactly these bytes."""
    return "".join(
        json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in rows
    ).encode("utf-8")


@dataclass(frozen=True, slots=True)
class Cut:
    """A set ready to be written: its files' bytes, and the manifest that describes them."""

    name: str
    rows: bytes
    raw_labels: bytes
    manifest: dict[str, object] = field(default_factory=dict)


def cut(workspace: Workspace, name: str) -> Cut:
    """Build the set ``name`` from a finished workspace.

    Refuses while any item is unlabelled or awaiting a ruling: an unmade decision cut into
    a set either leaves it quietly or arrives as ``unsure``, and either way the hardest
    items leave both KR3 numbers. Refuses, too, a set that every item was dropped from.
    """
    match = _SET_NAME.fullmatch(name)
    if match is None or match.group(1) != workspace.task.name:
        raise CutRefused(f"set name {name!r} must be '{workspace.task.name}/v<N>'")

    unlabelled = workspace.in_state(State.UNLABELLED)
    awaiting = workspace.in_state(State.AWAITING_RULING)
    if unlabelled or awaiting:
        raise CutRefused(
            f"not finished: {len(unlabelled)} item(s) not yet labelled by every assigned "
            f"annotator, {len(awaiting)} contested item(s) awaiting a ruling. "
            "`python -m eval.annotate queue` lists them."
        )

    kept = [item for item in workspace.items if item.state in (State.AGREED, State.ADJUDICATED)]
    if not kept:
        raise CutRefused("every item was dropped; there is no set to cut")

    rows = _jsonl([{**item.fields, "id": item.id, "gold": item.gold} for item in kept])
    raw_labels = _jsonl(
        [
            {
                "id": item.id,
                "labels": [label.as_json() for label in item.labels],
                "contested": item.contested,
                "outcome": item.state.value,
                "gold": item.gold,
                "cause": item.ruling.cause if item.ruling is not None else None,
                "adjudicator": item.ruling.by if item.ruling is not None else None,
                "override": item.override,
            }
            for item in workspace.items
        ]
    )
    description = workspace.meta.get("description")
    manifest: dict[str, object] = {
        "set": name,
        "format": FORMAT,
        "task": workspace.task.name,
        **{key: workspace.meta[key] for key in workspace.task.meta_keys},
        "sha256": hashlib.sha256(rows).hexdigest(),
        "row_count": len(kept),
        "raw_labels_sha256": hashlib.sha256(raw_labels).hexdigest(),
        "candidates": len(workspace.items),
        "drops": workspace.drops,
        "adjudicated": len(workspace.in_state(State.ADJUDICATED)),
        "overrides": workspace.overrides,
        "double_labelled": workspace.double_labelled,
        "participants": [p.as_json() for p in workspace.participants.values()],
    }
    if description is not None:
        manifest["description"] = description
    return Cut(name=name, rows=rows, raw_labels=raw_labels, manifest=manifest)


def write_cut(result: Cut, root: Path, *, mlflow_run: Mapping[str, str] | None) -> Path:
    """Write the set under ``root``. Refuses to replace a set that already exists.

    Written into a sibling directory and renamed into place, so an interrupted cut leaves
    no half-written set for the harness to find.
    """
    target = root / result.name
    if target.exists():
        raise CutRefused(f"{target} already exists; a set is never re-cut, cut a new version")
    manifest = {**result.manifest, "mlflow": dict(mlflow_run) if mlflow_run else None}
    staging = target.with_name(f".{target.name}.partial")
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True)
    try:
        (staging / ROWS_FILE).write_bytes(result.rows)
        (staging / RAW_LABELS_FILE).write_bytes(result.raw_labels)
        (staging / MANIFEST_FILE).write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        os.rename(staging, target)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return target


def workspace_files(workspace: Workspace) -> list[Path]:
    """The files that make up the workspace, and nothing else that happens to sit beside them."""
    base = workspace.path
    files = [base / WORKSPACE_FILE, base / POOL_FILE, base / PARTICIPANTS_FILE]
    files.append(base / ASSIGNMENTS_FILE)
    if (base / ADJUDICATION_FILE).is_file():
        files.append(base / ADJUDICATION_FILE)
    labels = base / LABELS_DIR
    if labels.is_dir():
        files.extend(sorted(labels.glob("*.jsonl")))
    return files
