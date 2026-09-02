"""Config loading, the predictors, and the reproducibility property AC1 rests on."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import textwrap
from functools import partial
from pathlib import Path

import pytest

from eval import run as run_module
from eval.evalset import DEFAULT_ROOT, EvalSetError, load
from eval.metrics import score_classification
from eval.predictors import Oracle, SeededStub, TopicClassifier, build
from eval.run import (
    execute,
    format_report,
    load_config,
    log_to_mlflow,
    main,
    provenance,
    without_credentials,
)

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
    assert isinstance(Oracle(), TopicClassifier)
    assert isinstance(SeededStub(seed=1), TopicClassifier)


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


def test_the_threshold_changes_what_is_reported_and_not_what_is_drawn() -> None:
    """The property a threshold sweep needs.

    Raising the bar must only reclassify rows as fallbacks — it must not change the topic or
    the confidence the model produced. If the draw moved with the bar, two runs differing
    only in the threshold would not be comparable row by row, and a sweep would be measuring
    two things at once.
    """
    rows = load(FIXTURE).rows
    open_bar = SeededStub(seed=7, min_confidence=0.0).predict(rows)
    high_bar = SeededStub(seed=7, min_confidence=0.6).predict(rows)

    assert any(p.fallback for p in high_bar.values()), "bar too low to punt on anything"
    assert not any(p.fallback for p in open_bar.values())
    for row in rows:
        assert high_bar[row.id].confidence == open_bar[row.id].confidence
        if not high_bar[row.id].fallback:
            assert high_bar[row.id].topic == open_bar[row.id].topic


def test_a_higher_bar_never_raises_coverage() -> None:
    """Punting can only remove assignments. Coverage rising with the threshold would mean
    the fallback path was assigning topics, which is the one thing it must not do."""
    rows = load(FIXTURE).rows
    scores = [
        score_classification(rows, SeededStub(seed=7, min_confidence=bar).predict(rows)).coverage
        for bar in (0.0, 0.4, 0.7, 1.0)
    ]
    assert scores == sorted(scores, reverse=True)
    assert scores[-1] == 0.0


def test_the_threshold_reaches_the_predictor_from_config(tmp_path: Path) -> None:
    """A parameter accepted by the config and dropped on the floor would leave every run
    reporting the default while its params say otherwise."""
    path = _write_config(
        tmp_path / "run.toml",
        f'eval_set = "{FIXTURE}"\nexperiment = "t"\njira = "MER-27"\n'
        '[predictor]\nname = "seeded"\nseed = 7\nmin_confidence = 0.6\n',
    )
    config = load_config(path)
    assert config.as_mlflow_params()["predictor.min_confidence"] == "0.6"
    assert execute(config).metrics.fallback > 0


def test_a_misspelled_predictor_parameter_is_refused() -> None:
    """Silently ignored, it would leave the run scoring at a threshold nobody chose while
    its logged params name one that was never applied."""
    with pytest.raises(ValueError, match="unknown parameter"):
        build("seeded", seed=7, min_confidence_=0.6)


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
        f'eval_set = "{FIXTURE}"\nexperiment = "t"\njira = "MER-27"\nseed = 7\n'
        '[predictor]\nname = "oracle"\n',
    )
    with pytest.raises(EvalSetError, match="unknown key\\(s\\) seed"):
        load_config(path)


def test_a_config_without_a_jira_key_is_refused(tmp_path: Path) -> None:
    """A run nobody can attribute cannot be found again when its number is questioned, and
    the tag is what groups the runs of one experiment into a cohort."""
    path = _write_config(
        tmp_path / "run.toml",
        f'eval_set = "{FIXTURE}"\nexperiment = "t"\n[predictor]\nname = "oracle"\n',
    )
    with pytest.raises(EvalSetError, match="'jira' is required"):
        load_config(path)


def test_a_wrongly_typed_config_value_is_refused_not_dropped(tmp_path: Path) -> None:
    """The unknown-key check catches a misspelled *name*; this catches a wrong *type*.

    Dropped instead, ``sets_root = 42`` would fall through to the default and the run would
    score a different corpus than the one its config names — the same failure the key check
    exists to prevent, reached from the other side.
    """
    path = _write_config(
        tmp_path / "run.toml",
        f'eval_set = "{FIXTURE}"\nexperiment = "t"\njira = "MER-27"\nsets_root = 42\n'
        '[predictor]\nname = "oracle"\n',
    )
    with pytest.raises(EvalSetError, match="'sets_root' must be a string path"):
        load_config(path)


def test_the_corpus_root_is_logged(tmp_path: Path) -> None:
    """The set hash makes a different corpus *detectable*; this makes it *resolvable*.
    Without it two rows differ by a hash nobody can trace back to a directory."""
    root = tmp_path / "sets"
    shutil.copytree(DEFAULT_ROOT, root)
    path = _write_config(
        tmp_path / "run.toml",
        f'eval_set = "{FIXTURE}"\nexperiment = "t"\njira = "MER-27"\nsets_root = "{root}"\n'
        '[predictor]\nname = "oracle"\n',
    )
    assert load_config(path).as_mlflow_params()["sets_root"] == str(root)
    assert load_config(_smoke_config(tmp_path)).as_mlflow_params()["sets_root"] == "default"


def test_a_config_without_a_predictor_is_refused(tmp_path: Path) -> None:
    path = _write_config(
        tmp_path / "run.toml", f'eval_set = "{FIXTURE}"\nexperiment = "t"\njira = "MER-27"\n'
    )
    with pytest.raises(EvalSetError, match="must declare a string 'name'"):
        load_config(path)


# --------------------------------------------------------------------------- runs


def test_two_runs_of_one_config_produce_identical_metrics(tmp_path: Path) -> None:
    """Within one process. Necessary, and on its own it does not reach the property — see
    the cross-process test below."""
    config = load_config(_smoke_config(tmp_path))
    assert execute(config).metrics == execute(config).metrics


_METRICS_PROBE = textwrap.dedent("""
    import json, sys
    from eval.run import execute, load_config
    from pathlib import Path
    m = execute(load_config(Path(sys.argv[1]))).metrics
    print(json.dumps({"coverage": m.coverage, "accuracy": m.accuracy_on_assigned,
                      "assigned": m.assigned, "correct": m.correct, "fallback": m.fallback}))
""")


def test_the_same_config_scores_the_same_in_a_fresh_interpreter(tmp_path: Path) -> None:
    """The property the in-process test cannot see.

    A predictor seeded from ``hash()`` is deterministic within one process and different in
    the next, because string hashing is randomised per interpreter. Measured: swapping the
    per-row seed for a ``hash()``-based one left every in-process test green while three
    invocations of this config returned coverage 0.25, 0.5 and 0.5. Two runs of one config
    are then not comparable and nothing says so.

    Varying ``PYTHONHASHSEED`` explicitly rather than trusting two default-seeded runs to
    differ — they usually would, but a test that fails one time in N is a test people learn
    to re-run.
    """
    config_path = _smoke_config(tmp_path)

    def score(hash_seed: str) -> str:
        done = subprocess.run(
            [sys.executable, "-c", _METRICS_PROBE, str(config_path)],
            cwd=Path(__file__).resolve().parents[1],
            env={**os.environ, "PYTHONHASHSEED": hash_seed},
            capture_output=True,
            text=True,
            check=False,
        )
        assert done.returncode == 0, f"probe failed:\n{done.stderr}"
        return done.stdout.strip()

    assert score("0") == score("12345")


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
        f'eval_set = "{FIXTURE}"\nexperiment = "t"\njira = "MER-27"\nsets_root = "{root}"\n'
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
        f'eval_set = "{FIXTURE}"\nexperiment = "t"\njira = "MER-27"\n'
        '[predictor]\nname = "seeded"\nseed = 7\n',
    )
    config = load_config(path)
    result = execute(config)
    silent = result.metrics.__class__(
        coverage=0.0,
        accuracy_on_assigned=None,
        scorable_rows=8,
        assigned=0,
        correct=0,
        fallback=0,
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


def _throwaway_repo(path: Path) -> str:
    """A real git repository with one commit. Returns its HEAD sha."""
    path.mkdir(parents=True, exist_ok=True)
    run = partial(subprocess.run, cwd=path, check=True, capture_output=True, text=True)
    run(["git", "init", "-q"])
    run(["git", "config", "user.email", "t@example.invalid"])
    run(["git", "config", "user.name", "test"])
    (path / "tracked.txt").write_text("original\n")
    run(["git", "add", "tracked.txt"])
    run(["git", "commit", "-qm", "initial"])
    return subprocess.run(
        ["git", "-C", str(path), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def test_provenance_carries_the_commit_and_whether_the_tree_was_clean() -> None:
    tags = provenance()
    assert set(tags) == {"git_sha", "git_dirty"}
    assert tags["git_dirty"] in {"true", "false", "unknown"}
    assert tags["git_sha"] != "unknown"


def test_git_dirty_reports_a_clean_tree_as_clean(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Against a real repository, not just a value from the allowed set.

    Asserting only that the tag is one of three strings is satisfied by hardcoding it, by
    inverting it, and by reading the wrong thing — measured: all three passed the whole
    suite. The tag has to be checked against a tree whose state the test controls.
    """
    repo = tmp_path / "clean"
    sha = _throwaway_repo(repo)
    monkeypatch.setattr(run_module, "_REPO", repo)

    assert provenance() == {"git_sha": sha, "git_dirty": "false"}


def test_git_dirty_sees_an_edit_to_a_tracked_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The failure AC1 names: a commit hash on a run made from an edited tree.

    Unstaged, so a check reading only the index would report clean here.
    """
    repo = tmp_path / "edited"
    _throwaway_repo(repo)
    (repo / "tracked.txt").write_text("changed\n")
    monkeypatch.setattr(run_module, "_REPO", repo)

    assert provenance()["git_dirty"] == "true"


def test_git_dirty_sees_an_untracked_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Untracked counts. A new module the run imported but nobody committed is exactly the
    code a commit hash would fail to name."""
    repo = tmp_path / "untracked"
    _throwaway_repo(repo)
    (repo / "extra.py").write_text("x = 1\n")
    monkeypatch.setattr(run_module, "_REPO", repo)

    assert provenance()["git_dirty"] == "true"


def test_provenance_is_unknown_outside_a_repository(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Honest for an installed copy — and it must be ``unknown``, never a plausible-looking
    default that a results table would read as a real answer."""
    monkeypatch.setattr(run_module, "_REPO", tmp_path / "not-a-repo")
    (tmp_path / "not-a-repo").mkdir()

    assert provenance() == {"git_sha": "unknown", "git_dirty": "unknown"}


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


def test_an_unset_tracking_uri_refuses_to_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Unset, MLflow does not fail — it resolves to a local scratch database and reports
    success, so a run that reached nothing looks exactly like one that reached the server.

    Measured before this guard existed: running from a scratch directory with the variable
    unset exited 0 and left an 872 KB mlflow.db behind. Run from the repository root, that
    file is untracked and every later run then reports git_dirty=true.
    """
    monkeypatch.delenv("MLFLOW_TRACKING_URI", raising=False)
    monkeypatch.chdir(tmp_path)

    assert main([str(_smoke_config(tmp_path))]) == 1
    assert "MLFLOW_TRACKING_URI is not set" in capsys.readouterr().err
    assert not list(tmp_path.glob("mlflow.db")), "a scratch backend was created anyway"


def test_a_blank_tracking_uri_is_treated_as_unset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """An exported-but-empty variable is the shape an unset one takes in a shell profile.

    The message is asserted, not just the exit code: MLflow also fails on a blank URI, so a
    test checking only for a non-zero exit passes with our guard deleted — it would be
    measuring MLflow choking rather than the guard firing.
    """
    monkeypatch.setenv("MLFLOW_TRACKING_URI", "   ")
    monkeypatch.chdir(tmp_path)

    assert main([str(_smoke_config(tmp_path))]) == 1
    assert "MLFLOW_TRACKING_URI is not set" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("given", "expected"),
    [
        ("https://user:hunter2@mlflow.example.com", "https://mlflow.example.com"),
        # A password may itself contain "@"; only the last one separates the host.
        ("https://user:p@ss@mlflow.example.com", "https://mlflow.example.com"),
        ("https://user@mlflow.example.com", "https://mlflow.example.com"),
        ("https://mlflow.example.com", "https://mlflow.example.com"),
        ("sqlite:///mlflow.db", "sqlite:///mlflow.db"),
        # No host part, so the "@" is just a character in a path and must survive.
        ("sqlite:////tmp/a@b/mlflow.db", "sqlite:////tmp/a@b/mlflow.db"),
    ],
)
def test_credentials_are_stripped_from_a_tracking_address(given: str, expected: str) -> None:
    assert without_credentials(given) == expected


def test_credentials_in_the_address_never_reach_the_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """That the stripping is *used*, not merely correct.

    A helper that does the right thing and is called by nothing looks identical to one that
    works. The recorded parameter is the thing at risk, so the assertion is on what reached
    ``log_param`` — with a stand-in for the tracking server, so no real one is needed.
    """
    import mlflow

    secret = "https://user:hunter2@mlflow.example.com"
    logged: dict[str, object] = {}

    class _NullRun:
        def __enter__(self) -> _NullRun:
            return self

        def __exit__(self, *exc: object) -> None:
            # Not bool: a truthy __exit__ swallows exceptions, and mypy refuses the
            # ambiguity. This stand-in must never hide a failure in the code under test.
            return None

    monkeypatch.setenv("MLFLOW_TRACKING_URI", secret)
    monkeypatch.setattr(mlflow, "get_tracking_uri", lambda: secret)
    monkeypatch.setattr(mlflow, "set_experiment", lambda *a, **k: None)
    monkeypatch.setattr(mlflow, "start_run", lambda *a, **k: _NullRun())
    monkeypatch.setattr(mlflow, "set_tags", lambda *a, **k: None)
    monkeypatch.setattr(mlflow, "log_params", lambda *a, **k: None)
    monkeypatch.setattr(mlflow, "log_metrics", lambda *a, **k: None)
    monkeypatch.setattr(mlflow, "log_param", lambda key, value: logged.__setitem__(key, value))

    config = load_config(_smoke_config(tmp_path))
    log_to_mlflow(config, execute(config))

    assert logged["tracking_uri"] == "https://mlflow.example.com"
    assert "hunter2" not in str(logged), "the password reached the run record"
    assert "credentials in MLFLOW_TRACKING_URI" in capsys.readouterr().err


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
