"""The run harness: one config in, one recorded run out.

Everything that differs between two runs — which set, which predictor, its parameters, the
seed — is read from a config file. Nothing is edited to change what a run does, because a
run tagged with a commit is making a claim: *this code produced this number*. If behaviour
can change without the commit changing, the tag is false and two runs stop being
comparable — silently, and only discovered much later when a result will not reproduce.
``git_dirty`` covers the remaining gap, where the tree has uncommitted changes.

Scoring and recording are separate functions on purpose: ``execute`` needs no tracking
server, so the harness can be exercised end to end without one.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tomllib
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from meridian_contract.taxonomy import TAXONOMY_VERSION

from eval.evalset import EvalSet, EvalSetError, load
from eval.metrics import ClassificationMetrics, score_classification
from eval.predictors import build


@dataclass(frozen=True, slots=True)
class RunConfig:
    """The whole of what varies between two runs."""

    eval_set: str
    experiment: str
    predictor: str
    predictor_params: dict[str, Any]
    jira: str
    sets_root: Path | None

    def as_mlflow_params(self) -> dict[str, str]:
        """Every input to the run, as strings, so a results table can be filtered on any."""
        params = {
            "eval_set": self.eval_set,
            "predictor": self.predictor,
            "taxonomy_version": TAXONOMY_VERSION,
            # A set name alone does not identify a corpus: the same name under a different
            # root is different bytes. The hash makes that detectable; this makes it
            # resolvable, by saying which tree the rows were read from.
            "sets_root": str(self.sets_root) if self.sets_root is not None else "default",
        }
        params.update({f"predictor.{k}": str(v) for k, v in sorted(self.predictor_params.items())})
        return params


@dataclass(frozen=True, slots=True)
class RunResult:
    eval_set: EvalSet
    metrics: ClassificationMetrics


def load_config(path: Path) -> RunConfig:
    """Read a run config. Unknown keys are refused rather than ignored.

    A misspelled key that is silently dropped means the run did something other than what
    its config appears to say, and the config is the only record of what was intended.
    """
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise EvalSetError(f"no such config: {path}") from None
    except tomllib.TOMLDecodeError as exc:
        raise EvalSetError(f"{path}: not valid TOML — {exc}") from exc

    known = {"eval_set", "experiment", "predictor", "jira", "sets_root"}
    unknown = raw.keys() - known
    if unknown:
        raise EvalSetError(f"{path}: unknown key(s) {', '.join(sorted(unknown))}")

    # ``jira`` is required: a run nobody can attribute cannot be found again when its number
    # is questioned, and it is what groups the runs of one experiment into a cohort.
    for required in ("eval_set", "experiment", "jira"):
        if not isinstance(raw.get(required), str):
            raise EvalSetError(f"{path}: '{required}' is required and must be a string")

    predictor = raw.get("predictor")
    if not isinstance(predictor, dict) or not isinstance(predictor.get("name"), str):
        raise EvalSetError(f"{path}: [predictor] must declare a string 'name'")

    # Refused rather than dropped. The unknown-key check above catches a misspelled *name*;
    # a key of the wrong *type* would otherwise fall through to the default and the run would
    # score something other than what its config appears to say — which is the same failure,
    # reached differently.
    sets_root = raw.get("sets_root")
    if sets_root is not None and not isinstance(sets_root, str):
        raise EvalSetError(f"{path}: 'sets_root' must be a string path")

    return RunConfig(
        eval_set=raw["eval_set"],
        experiment=raw["experiment"],
        predictor=predictor["name"],
        predictor_params={k: v for k, v in predictor.items() if k != "name"},
        jira=raw["jira"],
        sets_root=Path(sets_root) if sets_root is not None else None,
    )


def execute(config: RunConfig) -> RunResult:
    """Load, predict, score. No tracking server, no side effects."""
    eval_set = load(config.eval_set, root=config.sets_root)
    predictor = build(config.predictor, **config.predictor_params)
    predictions = predictor.predict(eval_set.rows)
    return RunResult(eval_set=eval_set, metrics=score_classification(eval_set.rows, predictions))


#: The checkout this harness was loaded from. Provenance is asked of *this* repository, not
#: of whatever directory the command happened to be run in — otherwise a run started from
#: anywhere else records "unknown" for both tags and looks entirely healthy doing it, which
#: is the exact failure the tags exist to prevent.
_REPO = Path(__file__).resolve().parent.parent


def _git(*args: str) -> str | None:
    """Run a git command against the harness's own checkout.

    Returns ``None`` if git is missing or the directory is not a repository — which is
    honest for an installed copy, and is why the tag says ``unknown`` rather than lying.
    """
    try:
        done = subprocess.run(
            ["git", "-C", str(_REPO), *args],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return done.stdout.strip() if done.returncode == 0 else None


def provenance() -> dict[str, str]:
    """Tags identifying the code that produced a run.

    ``git_dirty`` is what keeps ``git_sha`` honest: a commit hash on a run made from a tree
    with uncommitted edits names code that never existed anywhere.
    """
    sha = _git("rev-parse", "HEAD")
    status = _git("status", "--porcelain")
    return {
        "git_sha": sha or "unknown",
        "git_dirty": "unknown" if status is None else str(bool(status)).lower(),
    }


#: A run must name where it is being recorded. ⚠ Left unset, MLflow does *not* fail — it
#: silently resolves to ``sqlite:///$CWD/mlflow.db`` and reports success, so a run that
#: reached nothing but a scratch file in whatever directory it started from is
#: indistinguishable from one that reached the shared server (measured, mlflow-skinny 3.15).
#: Worse, run from the repository root that database is an untracked file, and every later
#: run then tags itself ``git_dirty=true`` on an otherwise clean checkout.
TRACKING_URI_ENV = "MLFLOW_TRACKING_URI"


def without_credentials(uri: str) -> str:
    """Strip any username and password from a tracking address.

    The address is recorded as a run parameter so a reader can tell which server a number
    came from. ⚠️ ``mlflow.get_tracking_uri()`` returns credentials embedded in the URL
    verbatim, so logging it unfiltered writes the password into MLflow itself, in plain text,
    where anyone who can read the experiment can read it. The same shape as ``str(engine.url)``
    on a SQLAlchemy engine: a value that prints harmlessly in one place and leaks in another.

    Addresses with no host part — ``sqlite:///mlflow.db`` and the like — pass through unchanged.
    """
    parts = urlsplit(uri)
    if "@" not in parts.netloc:
        return uri
    # rsplit: a password may itself contain "@", and only the last one separates host.
    return urlunsplit(parts._replace(netloc=parts.netloc.rsplit("@", 1)[1]))


def log_to_mlflow(config: RunConfig, result: RunResult) -> None:
    """Record the run. The only part of the harness that talks to a tracking server.

    Refuses to run unless ``MLFLOW_TRACKING_URI`` is set, for the reason above. A deliberate
    local run is still available by saying so — ``MLFLOW_TRACKING_URI=sqlite:///mlflow.db``.
    """
    import mlflow

    if not os.environ.get(TRACKING_URI_ENV, "").strip():
        raise EvalSetError(
            f"{TRACKING_URI_ENV} is not set. Unset, MLflow writes to a local scratch file "
            "and reports success, which is indistinguishable from reaching the tracking "
            "server. Name the server, or say sqlite:///mlflow.db to keep the run local."
        )

    mlflow.set_experiment(config.experiment)
    with mlflow.start_run():
        mlflow.set_tags({**provenance(), "jira": config.jira})
        mlflow.log_params(config.as_mlflow_params())
        # The set is identified by its hash as well as its name: two sets can share a name
        # across machines, but a number is only comparable against identical bytes.
        mlflow.log_param("eval_set_sha256", result.eval_set.sha256)
        mlflow.log_param("eval_set_rows", len(result.eval_set))
        tracking_uri = mlflow.get_tracking_uri()
        if without_credentials(tracking_uri) != tracking_uri:
            # Said out loud rather than silently cleaned: the caller is passing a secret
            # somewhere it does not belong, and the next tool they use may not strip it.
            print(
                "warning: credentials in MLFLOW_TRACKING_URI were kept out of the run record. "
                "Pass MLFLOW_TRACKING_USERNAME and MLFLOW_TRACKING_PASSWORD instead.",
                file=sys.stderr,
            )
        mlflow.log_param("tracking_uri", without_credentials(tracking_uri))
        mlflow.log_metrics(result.metrics.as_mlflow_metrics())


def format_report(result: RunResult) -> str:
    """A human-readable summary. The harness reports; it does not assert."""
    m = result.metrics
    accuracy = (
        "n/a (nothing assigned)"
        if m.accuracy_on_assigned is None
        else f"{m.accuracy_on_assigned:.3f}"
    )
    plural = "" if m.misassigned_other == 1 else "s"
    misassigned = (
        "n/a (no Other rows)"
        if m.misassigned_other_rate is None
        else f"{m.misassigned_other_rate:.3f} ({m.misassigned_other} row{plural})"
    )
    return "\n".join(
        [
            f"eval set              {result.eval_set.name}  ({len(result.eval_set)} rows, "
            f"sha256 {result.eval_set.sha256[:12]})",
            f"scorable rows         {m.scorable_rows}",
            "",
            f"coverage              {m.coverage:.3f}   ({m.assigned}/{m.scorable_rows})",
            f"accuracy on assigned  {accuracy}   ({m.correct}/{m.assigned})",
            "",
            # The shortfall behind coverage, split by cause: uncertainty is answered by a
            # better model or a moved threshold, confident-but-wrong Other by the taxonomy
            # or the labels. One number cannot tell you which you are looking at.
            f"not assigned          {m.fallback} below threshold, {m.confident_other} judged Other",
            f"fallback rate         {m.fallback_rate:.3f}",
            "",
            f"body coverage         {m.body_coverage:.3f}",
            f"median text chars     {m.median_text_chars:.1f}",
            f"misassigned Other     {misassigned}",
        ]
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run one evaluation and record it.")
    parser.add_argument("config", type=Path, help="path to a run config (TOML)")
    parser.add_argument(
        "--no-log", action="store_true", help="score and print without recording the run"
    )
    args = parser.parse_args(argv)

    try:
        config = load_config(args.config)
        result = execute(config)
    except (EvalSetError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print(format_report(result))
    if args.no_log:
        return 0

    # The numbers are already printed, so the work is not lost — but an unrecorded run does
    # not exist for comparison, which is the whole point of running one. Report the cause
    # plainly and exit non-zero, rather than dying in a traceback under a healthy report.
    try:
        log_to_mlflow(config, result)
    except Exception as exc:
        print(f"error: scored, but could not record the run: {exc}", file=sys.stderr)
        print(
            "hint: set MLFLOW_TRACKING_URI to the tracking server, or to a local database "
            "such as sqlite:///mlflow.db",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
