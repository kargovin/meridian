"""The annotation lifecycle: a workspace's states, the refusals, the cut, and the round trip.

The fixture workspace is ``eval/fixtures/classification-v2``, and the shipped set
``classification/v2`` is its cut. On paper:

    12 items; fx-001, 005, 009, 011, 012 labelled by both agents, the rest by agent-a
    fx-005    the agents disagree (science / world)       → ruled science
    fx-010    agent-a alone says sports, uncontested       → overridden to other
    fx-011    agent-a unsure, agent-b world                → dropped, genuine_disagreement
    fx-012    both unsure, unrecoverable                   → dropped, unrecoverable_record
    the rest  agreed

so 8 agreed, 2 adjudicated (one of them an override), 2 dropped, and 10 rows.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from eval import annotate
from eval.annotation import (
    CutRefused,
    State,
    WorkspaceError,
    cut,
    open_workspace,
    write_cut,
)
from eval.evalset import DEFAULT_ROOT, EvalSetError, load

FIXTURE_WORKSPACE = Path(__file__).resolve().parents[1] / "eval" / "fixtures" / "classification-v2"
FIXTURE_SET = "classification/v2"


@pytest.fixture
def ws(tmp_path: Path) -> Path:
    """A writable copy of the fixture workspace."""
    path = tmp_path / "ws"
    shutil.copytree(FIXTURE_WORKSPACE, path)
    return path


def _lines(path: Path) -> list[dict[str, object]]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def _write_lines(path: Path, rows: list[dict[str, object]]) -> None:
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))


def _edit(path: Path, item_id: str, **changes: object) -> None:
    """Change one line of a JSONL file, by item id. A value of ``...`` removes the key."""
    rows = _lines(path)
    (row,) = [r for r in rows if r["id"] == item_id]
    for key, value in changes.items():
        if value is ...:
            row.pop(key, None)
        else:
            row[key] = value
    _write_lines(path, rows)


def _drop_line(path: Path, item_id: str) -> None:
    _write_lines(path, [r for r in _lines(path) if r["id"] != item_id])


def _problems(path: Path) -> str:
    with pytest.raises(WorkspaceError) as caught:
        open_workspace(path)
    return "\n".join(caught.value.problems)


# --------------------------------------------------------------------------- states


def test_the_fixture_reads_as_worked_on_paper() -> None:
    workspace = open_workspace(FIXTURE_WORKSPACE)
    states = {item.id: item.state for item in workspace.items}
    assert states["fx-005"] is State.ADJUDICATED
    assert states["fx-010"] is State.ADJUDICATED
    assert states["fx-011"] is State.DROPPED
    assert states["fx-012"] is State.DROPPED
    assert [i for i, s in states.items() if s is State.AGREED] == [
        "fx-001", "fx-002", "fx-003", "fx-004", "fx-006", "fx-007", "fx-008", "fx-009",
    ]  # fmt: skip
    assert workspace.drops == {"genuine_disagreement": 1, "unrecoverable_record": 1}
    assert workspace.overrides == 1
    assert workspace.ready
    assert workspace.warnings() == []


def test_a_ruling_on_an_agreed_item_is_an_override_and_only_that() -> None:
    """fx-010 is uncontested, so ruling on it is an override; fx-005 was contested, so
    ruling on it is not."""
    items = {item.id: item for item in open_workspace(FIXTURE_WORKSPACE).items}
    assert items["fx-010"].override
    assert not items["fx-005"].override
    assert not items["fx-011"].override


def test_without_its_ruling_an_overridden_item_reverts_to_the_agreed_label(ws: Path) -> None:
    """The state is derived from the files every time; there is none stored to go stale."""
    _drop_line(ws / "adjudication.jsonl", "fx-010")
    workspace = open_workspace(ws)
    item = {i.id: i for i in workspace.items}["fx-010"]
    assert item.state is State.AGREED
    assert item.gold == "sports"
    assert workspace.overrides == 0


def test_an_item_missing_one_assigned_label_is_unlabelled_even_if_ruled(ws: Path) -> None:
    """fx-005 is ruled, but agent-b's label is gone: until every assigned annotator has
    answered, the item is not finished, whatever has been ruled."""
    _drop_line(ws / "labels" / "agent-b.jsonl", "fx-005")
    item = {i.id: i for i in open_workspace(ws).items}["fx-005"]
    assert item.state is State.UNLABELLED


def test_unsure_contests_an_item_even_when_every_annotator_says_it(ws: Path) -> None:
    """Two annotators agreeing on ``unsure`` have not agreed on a label."""
    _drop_line(ws / "adjudication.jsonl", "fx-012")
    item = {i.id: i for i in open_workspace(ws).items}["fx-012"]
    assert item.contested
    assert item.state is State.AWAITING_RULING


def test_a_single_annotators_unsure_contests_the_item(ws: Path) -> None:
    _edit(ws / "labels" / "agent-a.jsonl", "fx-003", label="unsure",
          unsure_reason="rule_does_not_fit")  # fmt: skip
    item = {i.id: i for i in open_workspace(ws).items}["fx-003"]
    assert item.state is State.AWAITING_RULING


# --------------------------------------------------------------------------- the cut


def test_cutting_the_fixture_reproduces_the_shipped_set_byte_for_byte(tmp_path: Path) -> None:
    """The shipped v2 is this tool's output, and the cut is deterministic: the same
    workspace cut twice is the same bytes, so the same hashes."""
    result = cut(open_workspace(FIXTURE_WORKSPACE), FIXTURE_SET)
    target = write_cut(result, tmp_path, mlflow_run=None)
    shipped = DEFAULT_ROOT / FIXTURE_SET
    for name in ("rows.jsonl", "raw_labels.jsonl", "manifest.json"):
        assert (target / name).read_bytes() == (shipped / name).read_bytes(), name


def test_the_cut_set_loads_and_carries_its_drops(tmp_path: Path) -> None:
    """The lifecycle's counts reach the harness through the manifest (AC3)."""
    write_cut(cut(open_workspace(FIXTURE_WORKSPACE), "classification/v7"), tmp_path,
              mlflow_run=None)  # fmt: skip
    loaded = load("classification/v7", root=tmp_path)
    assert len(loaded) == 10
    assert loaded.candidates == 12
    assert loaded.drops == {"genuine_disagreement": 1, "unrecoverable_record": 1}
    assert {row.id: row.gold.value for row in loaded.rows}["fx-005"] == "science"
    assert {row.id: row.gold.value for row in loaded.rows}["fx-010"] == "other"


def test_the_origin_never_reaches_the_set() -> None:
    """A set carries its own text and no key back into the database."""
    result = cut(open_workspace(FIXTURE_WORKSPACE), FIXTURE_SET)
    assert b"origin" not in result.rows
    assert b"example.invalid" not in result.rows
    assert b"example.invalid" not in result.raw_labels


def test_raw_labels_are_kept_as_given_for_every_candidate() -> None:
    """Agreement is computed on the labels before adjudication, dropped items included —
    they are exactly the disagreements, and leaving them out flatters agreement (AC5)."""
    result = cut(open_workspace(FIXTURE_WORKSPACE), FIXTURE_SET)
    raw = {r["id"]: r for r in (json.loads(x) for x in result.raw_labels.splitlines())}
    assert len(raw) == 12
    assert raw["fx-011"]["outcome"] == "dropped"
    assert raw["fx-011"]["cause"] == "genuine_disagreement"
    assert raw["fx-011"]["labels"] == [
        {"annotator": "agent-a", "label": "unsure", "unsure_reason": "collision_without_rule",
         "detail": {"candidates": ["world", "nation_politics"]}},
        {"annotator": "agent-b", "label": "world", "unsure_reason": None, "detail": None},
    ]  # fmt: skip
    assert raw["fx-010"]["labels"][0]["label"] == "sports"
    assert raw["fx-010"]["gold"] == "other"
    assert raw["fx-010"]["override"] is True
    assert raw["fx-005"]["adjudicator"] == "adjudicator"


def test_the_cut_refuses_while_labels_are_owed(ws: Path) -> None:
    """AC1: an unmade decision never reaches a set."""
    _drop_line(ws / "labels" / "agent-b.jsonl", "fx-001")
    with pytest.raises(CutRefused, match=r"1 item\(s\) not yet labelled"):
        cut(open_workspace(ws), "classification/v3")


def test_the_cut_refuses_while_rulings_are_owed(ws: Path) -> None:
    _drop_line(ws / "adjudication.jsonl", "fx-005")
    _drop_line(ws / "adjudication.jsonl", "fx-011")
    with pytest.raises(CutRefused, match=r"2 contested item\(s\) awaiting a ruling"):
        cut(open_workspace(ws), "classification/v3")


def test_the_cut_refuses_a_set_every_item_was_dropped_from(ws: Path) -> None:
    rulings = [
        {"id": r["id"], "by": "adjudicator", "outcome": "drop", "cause": "unrecoverable_record"}
        for r in _lines(ws / "pool.jsonl")
    ]
    _write_lines(ws / "adjudication.jsonl", rulings)
    with pytest.raises(CutRefused, match="every item was dropped"):
        cut(open_workspace(ws), "classification/v3")


@pytest.mark.parametrize("name", ["faithfulness/v3", "classification/3", "classification/v0"])
def test_the_cut_refuses_a_name_that_is_not_this_tasks_next_version(name: str) -> None:
    with pytest.raises(CutRefused, match="must be 'classification/v<N>'"):
        cut(open_workspace(FIXTURE_WORKSPACE), name)


def test_a_set_is_never_cut_over_an_existing_one(tmp_path: Path) -> None:
    result = cut(open_workspace(FIXTURE_WORKSPACE), "classification/v3")
    write_cut(result, tmp_path, mlflow_run=None)
    with pytest.raises(CutRefused, match="already exists"):
        write_cut(result, tmp_path, mlflow_run=None)
    assert not list((tmp_path / "classification").glob(".*partial")), "staging left behind"


# --------------------------------------------------------------------------- the files


def test_every_problem_is_reported_at_once(ws: Path) -> None:
    """A check that stops at the first problem is run once per problem."""
    _edit(ws / "labels" / "agent-a.jsonl", "fx-001", label="politics")
    _edit(ws / "adjudication.jsonl", "fx-011", cause="unreadable_input")
    found = _problems(ws)
    assert "label 'politics' is not a topic" in found
    assert "drop cause 'unreadable_input' is not one of" in found


def test_unsure_needs_a_reason_from_the_closed_set(ws: Path) -> None:
    _edit(ws / "labels" / "agent-a.jsonl", "fx-011", unsure_reason=...)
    assert "'unsure' needs an 'unsure_reason'" in _problems(ws)
    _edit(ws / "labels" / "agent-a.jsonl", "fx-011", unsure_reason="hard")
    assert "got 'hard'" in _problems(ws)


def test_a_reason_is_given_only_with_unsure(ws: Path) -> None:
    _edit(ws / "labels" / "agent-a.jsonl", "fx-001", unsure_reason="rule_does_not_fit")
    assert "'unsure_reason' is given only with the label 'unsure'" in _problems(ws)


def test_unsure_is_never_a_gold_label(ws: Path) -> None:
    _edit(ws / "adjudication.jsonl", "fx-005", gold="unsure")
    assert "'unsure' is never a gold label" in _problems(ws)


def test_another_tasks_drop_cause_is_refused(ws: Path) -> None:
    """``unreadable_input`` is faithfulness's name for the second cause. Accepted here, it
    would be counted under neither of classification's causes."""
    _edit(ws / "adjudication.jsonl", "fx-012", cause="unreadable_input")
    assert "drop cause 'unreadable_input' is not one of" in _problems(ws)


def test_a_label_for_an_item_not_assigned_to_its_annotator_is_refused(ws: Path) -> None:
    path = ws / "labels" / "agent-b.jsonl"
    _write_lines(path, [*_lines(path), {"id": "fx-002", "label": "world"}])
    assert "'fx-002' is not assigned to agent-b" in _problems(ws)


def test_a_second_label_for_one_item_is_refused(ws: Path) -> None:
    """A raw label is kept as given; two answers leave the file saying two things."""
    path = ws / "labels" / "agent-b.jsonl"
    _write_lines(path, [*_lines(path), {"id": "fx-001", "label": "sports"}])
    assert "'fx-001' is labelled twice" in _problems(ws)


def test_a_labels_file_for_an_undeclared_participant_is_refused(ws: Path) -> None:
    shutil.copy(ws / "labels" / "agent-b.jsonl", ws / "labels" / "agent-c.jsonl")
    assert "no participant 'agent-c' is declared" in _problems(ws)


def test_every_pool_item_is_assigned_exactly_once(ws: Path) -> None:
    _drop_line(ws / "assignments.jsonl", "fx-004")
    assert "1 pool item(s) are not assigned: fx-004" in _problems(ws)

    path = ws / "assignments.jsonl"
    _write_lines(path, [*_lines(path), {"id": "fx-003", "annotators": ["agent-b"]}])
    assert "'fx-003' is assigned more than once" in _problems(ws)


def test_a_duplicate_pool_id_is_refused(ws: Path) -> None:
    path = ws / "pool.jsonl"
    rows = _lines(path)
    _write_lines(path, [*rows, {**rows[0], "title": "another article"}])
    assert "duplicate id 'fx-001'" in _problems(ws)


def test_an_item_id_that_reads_as_an_article_id_is_refused(ws: Path) -> None:
    _edit(ws / "pool.jsonl", "fx-001", id="7277")
    assert "'7277' looks like an article id" in _problems(ws)


@pytest.mark.parametrize("file", ["pool.jsonl", "labels/agent-a.jsonl", "adjudication.jsonl"])
def test_an_unknown_key_is_refused_not_ignored(ws: Path, file: str) -> None:
    item_id = "fx-005"
    _edit(ws / file, item_id, notes="a misspelled 'note'")
    assert "unknown key(s) notes" in _problems(ws)


def test_the_workspace_names_the_taxonomy_it_was_labelled_against(ws: Path) -> None:
    meta = json.loads((ws / "workspace.json").read_text())
    meta["taxonomy"] = "v0"
    (ws / "workspace.json").write_text(json.dumps(meta))
    assert "'taxonomy' is 'v0'" in _problems(ws)


def test_an_agent_declares_its_model_and_instructions(ws: Path) -> None:
    raw = json.loads((ws / "participants.json").read_text())
    del raw["participants"][0]["instructions_sha256"]
    (ws / "participants.json").write_text(json.dumps(raw))
    assert "missing instructions_sha256" in _problems(ws)


# --------------------------------------------------------------------------- warnings


def test_two_agents_with_one_configuration_are_flagged(ws: Path) -> None:
    """Two copies of one model with one prompt are one annotator run twice."""
    raw = json.loads((ws / "participants.json").read_text())
    raw["participants"][1]["model"] = raw["participants"][0]["model"]
    (ws / "participants.json").write_text(json.dumps(raw))
    (warning,) = open_workspace(ws).warnings()
    assert warning.startswith("agent-a, agent-b share a model (fixture-a)")


def test_a_shared_model_with_different_instructions_is_not_flagged(ws: Path) -> None:
    raw = json.loads((ws / "participants.json").read_text())
    raw["participants"][1]["model"] = raw["participants"][0]["model"]
    raw["participants"][1]["instructions_sha256"] = "0" * 64
    (ws / "participants.json").write_text(json.dumps(raw))
    assert open_workspace(ws).warnings() == []


def test_too_little_double_labelling_is_flagged(ws: Path) -> None:
    """Rubric §8.1 asks for 10%. One of 12 is 8.3%; two would be 16.7%."""
    rows = [{"id": r["id"], "annotators": ["agent-a"]} for r in _lines(ws / "assignments.jsonl")]
    rows[0]["annotators"] = ["agent-a", "agent-b"]
    _write_lines(ws / "assignments.jsonl", rows)
    path = ws / "labels" / "agent-b.jsonl"
    _write_lines(path, [r for r in _lines(path) if r["id"] == rows[0]["id"]])
    (warning,) = open_workspace(ws).warnings()
    assert warning.startswith("1 of 12 items (8.3%) are labelled by two annotators")


# --------------------------------------------------------------------------- queue


def test_a_labelling_task_is_blind(ws: Path) -> None:
    """It carries the item and nothing about anyone else's answer, nor the origin: an
    annotator labels from the record."""
    _drop_line(ws / "labels" / "agent-b.jsonl", "fx-005")
    (task,) = annotate.queue_entries(open_workspace(ws))
    assert task["task"] == "label"
    assert task["annotator"] == "agent-b"
    assert "labels" not in task
    item = task["item"]
    assert isinstance(item, dict)
    assert item["id"] == "fx-005"
    assert "origin" not in item


def test_a_ruling_task_carries_the_labels_and_the_origin(ws: Path) -> None:
    _drop_line(ws / "adjudication.jsonl", "fx-011")
    (task,) = annotate.queue_entries(open_workspace(ws))
    assert task["task"] == "adjudicate"
    item = task["item"]
    assert isinstance(item, dict)
    assert item["origin"] == {"url": "https://example.invalid/fx-011"}
    labels = task["labels"]
    assert isinstance(labels, list)
    assert [label["label"] for label in labels] == ["unsure", "world"]


# --------------------------------------------------------------------------- the command line


def test_check_exits_zero_when_ready_two_when_work_is_left_one_when_broken(
    ws: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert annotate.main(["check", str(ws)]) == 0
    assert "ready to cut" in capsys.readouterr().out

    _drop_line(ws / "adjudication.jsonl", "fx-005")
    assert annotate.main(["check", str(ws)]) == 2
    assert "awaiting ruling  1" in capsys.readouterr().out

    _edit(ws / "labels" / "agent-a.jsonl", "fx-001", label="politics")
    assert annotate.main(["check", str(ws)]) == 1
    assert "label 'politics' is not a topic" in capsys.readouterr().err


def test_an_upload_names_its_ticket(
    ws: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path / "sets"
    assert annotate.main(["cut", str(ws), "classification/v3", "--root", str(root)]) == 1
    assert "--jira is required to upload" in capsys.readouterr().err
    assert not root.exists()


def test_an_upload_refuses_an_unset_tracking_uri_and_writes_nothing(
    ws: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("MLFLOW_TRACKING_URI", raising=False)
    monkeypatch.chdir(tmp_path)
    root = tmp_path / "sets"
    args = ["cut", str(ws), "classification/v3", "--root", str(root), "--jira", "MER-28"]
    assert annotate.main(args) == 1
    assert "MLFLOW_TRACKING_URI is not set" in capsys.readouterr().err
    assert not (root / "classification" / "v3").exists()
    assert not list(tmp_path.glob("mlflow.db"))


def test_cut_then_fetch_round_trips_through_mlflow(
    ws: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Against a real local tracking store: the cut records the files, a checkout holding
    only the manifest fetches them back, and the set loads."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("MLFLOW_TRACKING_URI", f"sqlite:///{tmp_path / 'mlflow.db'}")
    root = tmp_path / "sets"
    name = "classification/v3"
    args = ["cut", str(ws), name, "--root", str(root), "--jira", "MER-28", "--experiment", "t"]
    assert annotate.main(args) == 0, capsys.readouterr().err

    manifest = json.loads((root / name / "manifest.json").read_text())
    assert manifest["mlflow"]["experiment"] == "t"
    original = load(name, root=root)

    # A fresh checkout: the manifest is committed, the rest is not.
    (root / name / "rows.jsonl").unlink()
    (root / name / "raw_labels.jsonl").unlink()
    with pytest.raises(EvalSetError, match="does not exist"):
        load(name, root=root)

    assert annotate.main(["fetch", name, "--root", str(root)]) == 0, capsys.readouterr().err
    assert load(name, root=root) == original


def test_fetch_refuses_artifacts_that_do_not_match_the_manifest(
    ws: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("MLFLOW_TRACKING_URI", f"sqlite:///{tmp_path / 'mlflow.db'}")
    root = tmp_path / "sets"
    name = "classification/v3"
    assert annotate.main(["cut", str(ws), name, "--root", str(root), "--jira", "MER-28"]) == 0
    (root / name / "rows.jsonl").unlink()
    manifest_path = root / name / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["sha256"] = "0" * 64
    manifest_path.write_text(json.dumps(manifest))
    capsys.readouterr()

    assert annotate.main(["fetch", name, "--root", str(root)]) == 1
    assert "rows.jsonl in run" in capsys.readouterr().err
    assert not (root / name / "rows.jsonl").exists()


def test_a_refused_recut_leaves_no_run_behind(
    ws: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The existing set is checked before the upload as well as at the write. Checked only
    at the write, the refusal would come after a run had been recorded for a set that was
    never written."""
    import mlflow

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("MLFLOW_TRACKING_URI", f"sqlite:///{tmp_path / 'mlflow.db'}")
    root = tmp_path / "sets"
    args = ["cut", str(ws), "classification/v3", "--root", str(root), "--jira", "MER-28",
            "--experiment", "t"]  # fmt: skip
    assert annotate.main(args) == 0
    assert annotate.main(args) == 1
    assert "already exists" in capsys.readouterr().err
    assert len(mlflow.search_runs(experiment_names=["t"], output_format="list")) == 1


def test_fetch_on_a_set_cut_without_upload_says_where_its_rows_are(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert annotate.main(["fetch", FIXTURE_SET]) == 1
    assert "names no MLflow run" in capsys.readouterr().err
