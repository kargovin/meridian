"""Classification metrics — the pure core of the harness.

Nothing here touches a file, a network or MLflow. Give it rows and predictions, get numbers
back. That is what lets a metric be checked against a fixture small enough to verify on
paper, which is the difference between a metric the team has *agreed on* and a metric that
is merely a word three people each implemented slightly differently.

KR3 is a pair (PRD §9, FR-C3), and neither half means anything alone:

* **coverage** — of the articles that deserved a topic, how many got one. Beaten alone by
  guessing on everything.
* **accuracy on assigned** — of the articles that got one, how many were right. Beaten
  alone by never answering.

Both are computed over the same population: rows whose *gold* label is a real topic.
``Other`` and ``unsure`` rows are excluded from the numerator and the denominator of both.
That population is load-bearing. Counting genuinely-uncategorisable articles in the
denominator makes the metric move when the *source roster* changes — reporting a regression
in a classifier that did not change. Folding ``unsure`` in is worse: those rows cluster on
exactly the ambiguous articles a classifier finds hardest, so including them inflates
apparent coverage precisely where the truth matters most.

The population choice is also what makes the two floors compose: coverage x accuracy is
then the fraction of classifiable articles carrying a correct real topic (0.90 x 0.85 =
0.765), which is the commitment KR3 actually encodes.
"""

from __future__ import annotations

import statistics
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from meridian_contract.taxonomy import Topic

from eval.evalset import EvalRow


@dataclass(frozen=True, slots=True)
class Prediction:
    """One classifier output.

    ``confidence`` is carried even though no KR3 number reads it: the published contract
    always returns it, and calibration error needs it later. A seam that acquires a field
    later is a seam every existing predictor has to be revisited for.
    """

    topic: Topic
    confidence: float

    def __post_init__(self) -> None:
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError(f"confidence must be in [0, 1], got {self.confidence}")

    @property
    def is_assigned(self) -> bool:
        """Whether this counts as placing the article in a topic.

        ``Other`` is both the genuine catch-all and the low-confidence fallback (FR-C2).
        The distinction is invisible to a reader — under either, the article does not appear
        in a topic they browse — so it is not drawn here.
        """
        return self.topic is not Topic.OTHER


#: Predictions are matched back to rows **by id**, never by position. The published classify
#: contract does the same thing for the same reason: a predictor that returns rows in a
#: different order would misalign every pairing silently, and every resulting number would
#: still look entirely plausible.
type Predictions = Mapping[str, Prediction]


@dataclass(frozen=True, slots=True)
class ClassificationMetrics:
    """One run's numbers.

    The counts are reported alongside the ratios on purpose: ``0.875`` means something
    different over 8 rows than over 800, and a reader of a results table cannot tell which
    they are looking at from the ratio alone.
    """

    coverage: float
    accuracy_on_assigned: float | None
    scorable_rows: int
    assigned: int
    correct: int
    body_coverage: float
    median_text_chars: float
    misassigned_other: int
    misassigned_other_rate: float | None

    def as_mlflow_metrics(self) -> dict[str, float]:
        """Flatten for logging. Undefined values are omitted rather than sent as zero.

        An accuracy of ``0.0`` claims the model got everything wrong; a model that assigned
        nothing made no claims at all. Logging the first for the second is a lie a results
        table cannot recover from.
        """
        out: dict[str, float] = {
            "coverage": self.coverage,
            "scorable_rows": float(self.scorable_rows),
            "assigned": float(self.assigned),
            "correct": float(self.correct),
            "body_coverage": self.body_coverage,
            "median_text_chars": self.median_text_chars,
            "misassigned_other": float(self.misassigned_other),
        }
        if self.accuracy_on_assigned is not None:
            out["accuracy_on_assigned"] = self.accuracy_on_assigned
        if self.misassigned_other_rate is not None:
            out["misassigned_other_rate"] = self.misassigned_other_rate
        return out


def score_classification(
    rows: Sequence[EvalRow], predictions: Predictions
) -> ClassificationMetrics:
    """Compute KR3's pair, plus the run-level text statistics and one diagnostic.

    Raises ``ValueError`` if the set has no scorable rows, or if a row has no prediction —
    both are defects in the caller, and a metric quietly computed over a subset is worse
    than no metric.
    """
    scorable = [row for row in rows if row.gold_is_real_topic]
    if not scorable:
        raise ValueError(
            "no rows whose gold label is a real topic — nothing to measure. "
            "A set of only Other/unsure rows cannot produce a KR3 number."
        )

    missing = [row.id for row in rows if row.id not in predictions]
    if missing:
        shown = ", ".join(missing[:5])
        more = f" (+{len(missing) - 5} more)" if len(missing) > 5 else ""
        raise ValueError(f"no prediction for {len(missing)} row(s): {shown}{more}")

    assigned = [row for row in scorable if predictions[row.id].is_assigned]
    correct = [row for row in assigned if predictions[row.id].topic == row.gold]

    lengths = [len(row.text) for row in scorable]
    with_body = sum(1 for row in scorable if row.has_body)

    # Not part of KR3, and deliberately reported anyway. A classifier that files crosswords
    # under Sports is invisible to both KR3 numbers — those rows are outside the population
    # by design — so the failure would never appear in a results table without this.
    gold_other = [row for row in rows if row.gold is Topic.OTHER]
    misassigned = [row for row in gold_other if predictions[row.id].is_assigned]

    return ClassificationMetrics(
        coverage=len(assigned) / len(scorable),
        accuracy_on_assigned=(len(correct) / len(assigned)) if assigned else None,
        scorable_rows=len(scorable),
        assigned=len(assigned),
        correct=len(correct),
        body_coverage=with_body / len(scorable),
        median_text_chars=float(statistics.median(lengths)),
        misassigned_other=len(misassigned),
        misassigned_other_rate=(len(misassigned) / len(gold_other)) if gold_other else None,
    )
