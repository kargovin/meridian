"""Loading a versioned eval set, and the guards that make its version mean something."""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

import pytest
from meridian_contract.taxonomy import Topic

from eval.evalset import DEFAULT_ROOT, UNSURE, EvalSetError, load

FIXTURE = "classification/v1"


@pytest.fixture
def sets_root(tmp_path: Path) -> Path:
    """A writable copy of the shipped fixture, so a test can corrupt it."""
    root = tmp_path / "sets"
    shutil.copytree(DEFAULT_ROOT, root)
    return root


def _rewrite(
    root: Path,
    rows: list[dict[str, object]],
    *,
    refresh_hash: bool,
    row_count: int | None = None,
) -> None:
    """Replace the fixture's rows, optionally re-hashing so the manifest agrees.

    ``row_count`` defaults to the true length. Pass it explicitly to leave the manifest
    disagreeing — which is the only way to reach the count guard, since the hash otherwise
    fires first on any change to the rows.
    """
    base = root / FIXTURE
    raw = (
        "\n".join(json.dumps(r, ensure_ascii=False, sort_keys=True) for r in rows) + "\n"
    ).encode()
    (base / "rows.jsonl").write_bytes(raw)
    manifest = json.loads((base / "manifest.json").read_text())
    manifest["row_count"] = len(rows) if row_count is None else row_count
    if refresh_hash:
        manifest["sha256"] = hashlib.sha256(raw).hexdigest()
    (base / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")


def test_the_shipped_fixture_loads() -> None:
    """It is the calibration weight every metric test is measured against."""
    loaded = load(FIXTURE)
    assert len(loaded) == 12
    assert len(loaded.scorable) == 8
    assert loaded.sha256


def test_gold_labels_parse_into_topics_and_the_unsure_sentinel() -> None:
    loaded = load(FIXTURE)
    by_id = {row.id: row for row in loaded.rows}
    assert by_id["fx-001"].gold is Topic.WORLD
    assert by_id["fx-009"].gold is Topic.OTHER
    assert by_id["fx-011"].gold == UNSURE


def test_scorable_excludes_other_and_unsure() -> None:
    """The population both KR3 numbers are computed over."""
    loaded = load(FIXTURE)
    golds = {row.gold for row in loaded.scorable}
    assert Topic.OTHER not in golds
    assert UNSURE not in golds


def test_a_changed_row_is_refused(sets_root: Path) -> None:
    """The guard the whole versioning scheme rests on.

    Without it a set is edited in place and every number recorded before the edit silently
    stops being comparable — no diff, no error, no failing test.
    """
    rows_path = sets_root / FIXTURE / "rows.jsonl"
    rows_path.write_bytes(rows_path.read_bytes().replace(b"Antarctic", b"Arctic!!!"))

    with pytest.raises(EvalSetError, match="does not match the manifest"):
        load(FIXTURE, root=sets_root)


def test_a_changed_row_count_is_refused(sets_root: Path) -> None:
    """Belt and braces: the hash catches this too, but the count names what went wrong."""
    base = sets_root / FIXTURE
    rows = [json.loads(line) for line in (base / "rows.jsonl").read_text().splitlines()]
    _rewrite(sets_root, rows[:-1], refresh_hash=True, row_count=12)

    with pytest.raises(EvalSetError, match="declares 12 rows, found 11"):
        load(FIXTURE, root=sets_root)


def test_a_manifest_naming_a_different_set_is_refused(sets_root: Path) -> None:
    """Catches a directory copied to a new version without editing the manifest — which
    verifies perfectly against its own hash and reports itself under the wrong name."""
    path = sets_root / FIXTURE / "manifest.json"
    manifest = json.loads(path.read_text())
    manifest["set"] = "classification/v2"
    path.write_text(json.dumps(manifest, indent=2))

    with pytest.raises(EvalSetError, match="manifest declares set 'classification/v2'"):
        load(FIXTURE, root=sets_root)


def test_an_unknown_gold_label_is_refused(sets_root: Path) -> None:
    """A label outside the taxonomy is a broken set, not a row to skip."""
    _rewrite(
        sets_root,
        [{"id": "a", "title": "t", "body": None, "gold": "politics"}],
        refresh_hash=True,
    )
    with pytest.raises(EvalSetError, match="unknown gold label 'politics'"):
        load(FIXTURE, root=sets_root)


def test_duplicate_ids_are_refused(sets_root: Path) -> None:
    """Predictions are keyed by id, so a repeat makes the pairing ambiguous — the same
    reason the published classify contract rejects a repeated item id."""
    _rewrite(
        sets_root,
        [
            {"id": "dup", "title": "one", "body": None, "gold": "world"},
            {"id": "dup", "title": "two", "body": None, "gold": "sports"},
        ],
        refresh_hash=True,
    )
    with pytest.raises(EvalSetError, match="duplicate row ids"):
        load(FIXTURE, root=sets_root)


def test_a_missing_field_names_the_line(sets_root: Path) -> None:
    _rewrite(sets_root, [{"id": "a", "title": "t", "body": None}], refresh_hash=True)
    with pytest.raises(EvalSetError, match="line 1: missing gold"):
        load(FIXTURE, root=sets_root)


def test_whitespace_only_body_counts_as_absent(sets_root: Path) -> None:
    """Otherwise ``has_body`` counts a row the model learned nothing from, and run-level
    text statistics report coverage we do not have."""
    _rewrite(
        sets_root,
        [{"id": "a", "title": "t", "body": "   \n ", "gold": "world"}],
        refresh_hash=True,
    )
    row = load(FIXTURE, root=sets_root).rows[0]
    assert row.body is None
    assert row.has_body is False


def test_has_body_is_derived_from_the_text_beside_it() -> None:
    """Never stored. A recorded flag can disagree with the body it describes, and then the
    set reports something no longer true of itself."""
    loaded = load(FIXTURE)
    for row in loaded.rows:
        assert row.has_body == (row.body is not None)


def test_text_is_title_alone_when_there_is_no_body() -> None:
    loaded = load(FIXTURE)
    by_id = {row.id: row for row in loaded.rows}
    headline_only = by_id["fx-001"]
    with_body = by_id["fx-002"]
    assert headline_only.text == headline_only.title
    assert with_body.text.startswith(with_body.title)
    assert with_body.body is not None
    assert with_body.body in with_body.text


def test_a_missing_set_is_refused() -> None:
    with pytest.raises(EvalSetError, match="does not exist"):
        load("classification/v99")
