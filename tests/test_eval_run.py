"""Config loading, the predictors, and the reproducibility property AC1 rests on."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from eval.evalset import DEFAULT_ROOT, EvalSetError, load
from eval.metrics import score_classification
from eval.predictors import Classifier, Oracle, SeededStub, build
from eval.run import execute, format_report, load_config, main, provenance

FIXTURE = "classification/v1"


def _write_config(path: Path, body: str) -> Path:
    path.write_text(body, encoding="utf-8")
    return path


def _smoke_config(tmp_path: Path) -> Path:
    return _write_config(
        tmp_path / "run.toml",
        f'eval_set = "{FIXTURE}"\n'
        'experiment = "test"\n'
        'jira = "MER-27"\n'
        "[predictor]\n"
        'name = "seeded"\n'
        "seed = 7\n",
    )


# --------------------------------------------------------------------------- predictors


def test_both_stubs_satisfy_the_seam() -> None:
    """The seam is a Protocol, not a Callable alias: ``Callable[..., X]`` switches off
    argument checking, so a renamed parameter would type-check clean and fail on first use."""
    assert isinstance(Oracle(), Classifier)
    assert isinstance(SeededStub(seed=1), Classifier)


def test_the_oracle_scores_a_perfect_pair() -> None:
    """The ceiling check. If this is not 1.0/1.0 the fault is in the measurement, which is
    otherwise very hard to notice — a plausible number looks exactly like a correct one."""
    loaded = load(FIXTURE)
    scored = score_classification(loaded.rows, Oracle().predict(loaded.rows))
    assert scored.coverage == 1.0
    assert scored.accuracy_on_assigned == 1.0
    assert scored.misassigned_other == 0


def test_the_same_seed_gives_the_same_predictions() -> None:
    rows = load(FIXTURE).rows
    assert SeededStub(seed=7).predict(rows) == SeededStub(seed=7).predict(rows)


def test_a_different_seed_gives_different_predictions() -> None:
    """Otherwise the seed is not an input and the reproducibility test above proves nothing
    beyond the function being deterministic in the trivial sense."""
    rows = load(FIXTURE).rows
    assert SeededStub(seed=7).predict(rows) != SeededStub(seed=8).predict(rows)


def test_a_prediction_depends_on_the_article_alone_not_on_row_order() -> None:
    """Each row draws from its own stream keyed by id. Scoring a subset, or a reordered set,
    then leaves every other prediction untouched — which is what makes a difference between
    two runs attributable to the thing that changed."""
    rows = load(FIXTURE).rows
    full = SeededStub(seed=7).predict(rows)
    subset = SeededStub(seed=7).predict(rows[3:])
    assert all(subset[row.id] == full[row.id] for row in rows[3:])


def test_an_unknown_predictor_is_refused_rather_than_defaulted() -> None:
    """A run that silently scored something other than what its config names is a row in a
    results table that cannot be trusted."""
    with pytest.raises(ValueError, match="unknown predictor 'deberta'"):
        build("deberta")


def test_the_seeded_predictor_requires_its_seed() -> None:
    with pytest.raises(ValueError, match="requires an integer 'seed'"):
        build("seeded")


# --------------------------------------------------------------------------- config


def test_a_config_is_read_whole(tmp_path: Path) -> None:
    config = load_config(_smoke_config(tmp_path))
    assert config.eval_set == FIXTURE
    assert config.predictor == "seeded"
    assert config.predictor_params == {"seed": 7}
    assert config.jira == "MER-27"


def test_every_input_reaches_the_logged_params(tmp_path: Path) -> None:
    """A parameter that varies a run but is not logged makes two rows in a results table
    look identical when they are not."""
    params = load_config(_smoke_config(tmp_path)).as_mlflow_params()
    assert params["eval_set"] == FIXTURE
    assert params["predictor"] == "seeded"
    assert params["predictor.seed"] == "7"
    assert params["taxonomy_version"] == "v1"


def test_an_unknown_config_key_is_refused(tmp_path: Path) -> None:
    """A misspelled key that is silently dropped means the run did something other than what
    its config appears to say — and the config is the only record of what was intended."""
    path = _write_config(
        tmp_path / "run.toml",
        f'eval_set = "{FIXTURE}"\nexperiment = "t"\nseed = 7\n[predictor]\nname = "oracle"\n',
    )
    with pytest.raises(EvalSetError, match="unknown key\\(s\\) seed"):
        load_config(path)


def test_a_config_without_a_predictor_is_refused(tmp_path: Path) -> None:
    path = _write_config(tmp_path / "run.toml", f'eval_set = "{FIXTURE}"\nexperiment = "t"\n')
    with pytest.raises(EvalSetError, match="must declare a string 'name'"):
        load_config(path)


# --------------------------------------------------------------------------- runs


def test_two_runs_of_one_config_produce_identical_metrics(tmp_path: Path) -> None:
    """AC1's property. If this can fail, a run's commit tag is not evidence of anything and
    two runs stop being comparable — silently."""
    config = load_config(_smoke_config(tmp_path))
    assert execute(config).metrics == execute(config).metrics


def test_a_run_records_the_hash_of_what_it_scored(tmp_path: Path) -> None:
    """Two sets can share a name across machines; a number is only comparable against
    identical bytes."""
    result = execute(load_config(_smoke_config(tmp_path)))
    assert result.eval_set.sha256 == load(FIXTURE).sha256


def test_a_corrupted_set_stops_the_run(tmp_path: Path) -> None:
    """End to end: the guard reaches the exit code, so a scheduled run fails loudly instead
    of recording a number against a set nobody can identify."""
    root = tmp_path / "sets"
    shutil.copytree(DEFAULT_ROOT, root)
    rows_path = root / FIXTURE / "rows.jsonl"
    rows_path.write_bytes(rows_path.read_bytes().replace(b"crossword", b"crosswordz"))

    path = _write_config(
        tmp_path / "run.toml",
        f'eval_set = "{FIXTURE}"\nexperiment = "t"\nsets_root = "{root}"\n'
        '[predictor]\nname = "oracle"\n',
    )
    assert main([str(path), "--no-log"]) == 1


def test_a_good_run_exits_zero_and_reports(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main([str(_smoke_config(tmp_path)), "--no-log"]) == 0
    out = capsys.readouterr().out
    assert "coverage" in out
    assert "body coverage" in out


def test_the_report_names_the_set_and_its_hash(tmp_path: Path) -> None:
    result = execute(load_config(_smoke_config(tmp_path)))
    report = format_report(result)
    assert FIXTURE in report
    assert result.eval_set.sha256[:12] in report


def test_the_report_says_not_applicable_rather_than_zero_accuracy(tmp_path: Path) -> None:
    """A model that assigned nothing made no claims; printing 0.000 says it got everything
    wrong, which is a different and untrue statement."""
    path = _write_config(
        tmp_path / "run.toml",
        f'eval_set = "{FIXTURE}"\nexperiment = "t"\n[predictor]\nname = "seeded"\nseed = 7\n',
    )
    config = load_config(path)
    result = execute(config)
    silent = result.metrics.__class__(
        coverage=0.0,
        accuracy_on_assigned=None,
        scorable_rows=8,
        assigned=0,
        correct=0,
        body_coverage=0.25,
        median_text_chars=61.5,
        misassigned_other=0,
        misassigned_other_rate=None,
    )
    report = format_report(result.__class__(eval_set=result.eval_set, metrics=silent))
    assert "n/a (nothing assigned)" in report


def test_every_shipped_config_loads_and_runs() -> None:
    """A config committed to the repo that does not parse, or names a set that is not there,
    is a broken artefact nobody discovers until they try to use it."""
    shipped = sorted((Path(__file__).resolve().parents[1] / "eval" / "configs").glob("*.toml"))
    assert shipped, "no shipped configs found"
    for path in shipped:
        result = execute(load_config(path))
        assert result.metrics.scorable_rows > 0


# --------------------------------------------------------------------------- provenance


def test_provenance_carries_the_commit_and_whether_the_tree_was_clean() -> None:
    """``git_dirty`` is what keeps ``git_sha`` honest: a commit hash on a run made from a
    tree with uncommitted edits names code that never existed anywhere."""
    tags = provenance()
    assert set(tags) == {"git_sha", "git_dirty"}
    assert tags["git_dirty"] in {"true", "false", "unknown"}
    assert tags["git_sha"] != "unknown"


def test_provenance_does_not_depend_on_the_working_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Asked of the harness's own checkout, not of wherever the command was run.

    Found by running the harness from ``/tmp``: both tags recorded ``unknown`` and the run
    looked perfectly healthy — which is precisely the missing-provenance failure the tags
    exist to make impossible.
    """
    monkeypatch.chdir(tmp_path)
    assert provenance()["git_sha"] != "unknown"


def test_a_tracking_failure_is_reported_rather_than_raised(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """An unrecorded run must exit non-zero — it does not exist for comparison — but it must
    say why, under the numbers it did compute, instead of a traceback."""
    monkeypatch.setenv("MLFLOW_TRACKING_URI", str(tmp_path / "not-a-backend"))
    assert main([str(_smoke_config(tmp_path))]) == 1
    captured = capsys.readouterr()
    assert "coverage" in captured.out
    assert "could not record the run" in captured.err
