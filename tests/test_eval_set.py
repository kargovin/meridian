"""Loading a versioned eval set, and the guards that make its version mean something."""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

import pytest
from meridian_contract.taxonomy import Topic

from eval.evalset import DEFAULT_ROOT, EvalSetError, load

FIXTURE = "classification/v2"


@pytest.fixture
def sets_root(tmp_path: Path) -> Path:
    """A writable copy of the shipped fixture, so a test can corrupt it."""
    root = tmp_path / "sets"
    shutil.copytree(DEFAULT_ROOT, root)
    return root


def _manifest(root: Path) -> dict[str, object]:
    loaded: dict[str, object] = json.loads((root / FIXTURE / "manifest.json").read_text())
    return loaded


def _write_manifest(root: Path, manifest: dict[str, object]) -> None:
    (root / FIXTURE / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")


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
    fires first on any change to the rows. The candidate count follows the rows, so the
    drops still add up.
    """
    base = root / FIXTURE
    raw = (
        "\n".join(json.dumps(r, ensure_ascii=False, sort_keys=True) for r in rows) + "\n"
    ).encode()
    (base / "rows.jsonl").write_bytes(raw)
    manifest = _manifest(root)
    manifest["row_count"] = len(rows) if row_count is None else row_count
    drops = manifest["drops"]
    assert isinstance(drops, dict)
    manifest["candidates"] = len(rows) + sum(drops.values())
    if refresh_hash:
        manifest["sha256"] = hashlib.sha256(raw).hexdigest()
    _write_manifest(root, manifest)


def test_the_shipped_fixture_loads() -> None:
    """It is the calibration weight every metric test is measured against."""
    loaded = load(FIXTURE)
    assert len(loaded) == 10
    assert len(loaded.scorable) == 8
    assert loaded.sha256


def test_gold_labels_parse_into_topics() -> None:
    loaded = load(FIXTURE)
    by_id = {row.id: row for row in loaded.rows}
    assert by_id["fx-001"].gold is Topic.WORLD
    assert by_id["fx-009"].gold is Topic.OTHER


def test_scorable_excludes_other() -> None:
    """The population both KR3 numbers are computed over."""
    loaded = load(FIXTURE)
    golds = {row.gold for row in loaded.scorable}
    assert Topic.OTHER not in golds


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
    _rewrite(sets_root, rows[:-1], refresh_hash=True, row_count=10)

    with pytest.raises(EvalSetError, match="declares 10 rows, found 9"):
        load(FIXTURE, root=sets_root)


def test_a_manifest_naming_a_different_set_is_refused(sets_root: Path) -> None:
    """Catches a directory copied to a new version without editing the manifest — which
    verifies perfectly against its own hash and reports itself under the wrong name."""
    manifest = _manifest(sets_root)
    manifest["set"] = "classification/v3"
    _write_manifest(sets_root, manifest)

    with pytest.raises(EvalSetError, match="manifest declares set 'classification/v3'"):
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


# --------------------------------------------------------------------------- unfinished sets


def test_a_set_with_unsure_rows_is_refused_and_names_the_count(sets_root: Path) -> None:
    """An unresolved ``unsure`` is an unmade decision that looks like data (rubric §7.5).

    Excluded quietly, it takes the hardest articles out of both KR3 denominators and the
    number rises with nothing failing — so the set is refused, not scored. Two rows, not
    one, so a refusal that stops at the first cannot pass.
    """
    rows = [
        json.loads(line) for line in (sets_root / FIXTURE / "rows.jsonl").read_text().splitlines()
    ]
    rows[0]["gold"] = "unsure"
    rows[3]["gold"] = "unsure"
    _rewrite(sets_root, rows, refresh_hash=True)

    with pytest.raises(EvalSetError, match=r"2 row\(s\) still carry gold 'unsure'"):
        load(FIXTURE, root=sets_root)


def test_a_manifest_without_drop_counts_is_refused(sets_root: Path) -> None:
    """A dropped article is not in the set, so its count is the only trace the harness gets.
    Without it a set would score as if adjudication had dropped nothing."""
    manifest = _manifest(sets_root)
    del manifest["drops"]
    _write_manifest(sets_root, manifest)

    with pytest.raises(EvalSetError, match="records no drop counts"):
        load(FIXTURE, root=sets_root)


@pytest.mark.parametrize(
    "drops",
    [
        {"genuine_disagreement": 1},
        {"genuine_disagreement": 1, "unrecoverable_record": 1, "unreadable_input": 0},
    ],
    ids=["a-cause-missing", "a-cause-from-another-task"],
)
def test_the_drop_causes_are_exactly_the_closed_set(sets_root: Path, drops: dict[str, int]) -> None:
    """Every cause is recorded, zero included: an absent cause cannot be told from a cause
    nobody counted."""
    manifest = _manifest(sets_root)
    manifest["drops"] = drops
    _write_manifest(sets_root, manifest)

    with pytest.raises(EvalSetError, match="drop causes are"):
        load(FIXTURE, root=sets_root)


@pytest.mark.parametrize("count", [-1, True, 1.0])
def test_a_drop_count_must_be_a_whole_number(sets_root: Path, count: object) -> None:
    manifest = _manifest(sets_root)
    manifest["drops"] = {"genuine_disagreement": count, "unrecoverable_record": 1}
    _write_manifest(sets_root, manifest)

    with pytest.raises(EvalSetError, match="must be a whole number"):
        load(FIXTURE, root=sets_root)


def test_candidates_must_be_the_rows_plus_the_drops(sets_root: Path) -> None:
    """The drop rate's denominator. A manifest whose counts do not add up has had one of
    them edited, and the rate would be computed against a number nobody recorded."""
    manifest = _manifest(sets_root)
    manifest["candidates"] = 20
    _write_manifest(sets_root, manifest)

    with pytest.raises(
        EvalSetError, match="declares 20 candidates, but its rows and drops add up to 12"
    ):
        load(FIXTURE, root=sets_root)


def test_drop_rates_are_per_cause_over_the_candidates() -> None:
    """One drop per cause out of 12 candidates. Both are above the 3% trigger, so both are
    for review — the fixture is small, not a sample of production."""
    loaded = load(FIXTURE)
    assert loaded.candidates == 12
    assert loaded.drops == {"genuine_disagreement": 1, "unrecoverable_record": 1}
    assert loaded.drop_rates == {
        "genuine_disagreement": pytest.approx(1 / 12),
        "unrecoverable_record": pytest.approx(1 / 12),
    }
    assert loaded.drop_rates_for_review == ("genuine_disagreement", "unrecoverable_record")


def test_only_a_cause_above_the_trigger_is_for_review(sets_root: Path) -> None:
    """With 44 candidates one drop is 2.3% and two are 4.5%: only the second cause is for
    review. A trigger applied to the summed rate (6.8%) would flag both."""
    manifest = _manifest(sets_root)
    manifest["drops"] = {"genuine_disagreement": 1, "unrecoverable_record": 2}
    _write_manifest(sets_root, manifest)
    rows_path = sets_root / FIXTURE / "rows.jsonl"
    rows = [json.loads(line) for line in rows_path.read_text().splitlines()]
    padded = rows + [
        {"id": f"pad-{n:02d}", "title": f"padding {n}", "body": None, "gold": "world"}
        for n in range(31)
    ]
    _rewrite(sets_root, padded, refresh_hash=True)

    loaded = load(FIXTURE, root=sets_root)
    assert loaded.candidates == 44
    assert loaded.drop_rates_for_review == ("unrecoverable_record",)


def test_changed_raw_labels_are_refused(sets_root: Path) -> None:
    """Agreement is computed on the raw labels, so they are as immutable as the rows."""
    path = sets_root / FIXTURE / "raw_labels.jsonl"
    path.write_bytes(path.read_bytes().replace(b'"label": "world"', b'"label": "sports"', 1))

    with pytest.raises(EvalSetError, match=r"raw_labels\.jsonl does not match the manifest"):
        load(FIXTURE, root=sets_root)


def test_a_set_without_its_rows_points_at_fetch(sets_root: Path) -> None:
    """A real set commits only its manifest; the refusal says how to get the rest."""
    (sets_root / FIXTURE / "raw_labels.jsonl").unlink()

    with pytest.raises(EvalSetError, match=r"eval\.annotate fetch classification/v2"):
        load(FIXTURE, root=sets_root)


# --------------------------------------------------------------------------- rows


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


@pytest.mark.parametrize("field", ["body", "lede"])
def test_whitespace_only_text_counts_as_absent(sets_root: Path, field: str) -> None:
    """Otherwise ``has_body`` counts a row the model learned nothing from, and run-level
    text statistics report coverage we do not have."""
    _rewrite(
        sets_root,
        [{"id": "a", "title": "t", field: "   \n ", "gold": "world"}],
        refresh_hash=True,
    )
    row = load(FIXTURE, root=sets_root).rows[0]
    assert getattr(row, field) is None
    assert row.text == "t"


def test_has_body_is_derived_from_the_text_beside_it() -> None:
    """Never stored. A recorded flag can disagree with the body it describes, and then the
    set reports something no longer true of itself."""
    loaded = load(FIXTURE)
    for row in loaded.rows:
        assert row.has_body == (row.body is not None)


def test_text_is_title_alone_when_there_is_no_lede_or_body() -> None:
    loaded = load(FIXTURE)
    by_id = {row.id: row for row in loaded.rows}
    headline_only = by_id["fx-001"]
    with_body = by_id["fx-002"]
    assert headline_only.text == headline_only.title
    assert with_body.text.startswith(with_body.title)
    assert with_body.body is not None
    assert with_body.body in with_body.text


def test_text_carries_the_lede() -> None:
    """A headline-only publisher's row is a title and a lede; leaving the lede out would
    hand a predictor less than the annotator read."""
    row = {r.id: r for r in load(FIXTURE).rows}["fx-010"]
    assert row.lede is not None
    assert row.text == f"{row.title}\n\n{row.lede}"


def test_the_publisher_is_carried() -> None:
    row = {r.id: r for r in load(FIXTURE).rows}["fx-001"]
    assert row.publisher == "Fixture Wire"


def test_a_missing_set_is_refused() -> None:
    with pytest.raises(EvalSetError, match="does not exist"):
        load("classification/v99")
