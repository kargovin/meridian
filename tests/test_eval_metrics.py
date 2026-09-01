"""KR3's arithmetic, measured against a fixture small enough to check by hand.

The whole point of the fixture is that the expected numbers are derived on paper, not from
running the code and writing down whatever came out. On ``classification/v1``:

    8 rows whose gold label is a real topic   (fx-001 .. fx-008)
    2 genuine Other                           (fx-009, fx-010)
    2 unsure                                  (fx-011, fx-012)

``SCRIPT`` below assigns a real topic to 7 of the 8, gets 6 of those right, and abstains on
the eighth — so coverage is 7/8 and accuracy is 6/7, whatever the code does.
"""

from __future__ import annotations

import pytest
from meridian_contract.taxonomy import Topic

from eval.evalset import EvalRow, load
from eval.metrics import ClassificationMetrics, Prediction, Predictions, score_classification

FIXTURE = "classification/v1"

#: Chosen so both numbers are non-trivial and disagree with each other.
SCRIPT: dict[str, Topic] = {
    "fx-001": Topic.WORLD,  # correct
    "fx-002": Topic.NATION_POLITICS,  # correct
    "fx-003": Topic.BUSINESS,  # correct
    "fx-004": Topic.TECHNOLOGY,  # correct
    "fx-005": Topic.SCIENCE,  # correct
    "fx-006": Topic.HEALTH,  # correct
    "fx-007": Topic.BUSINESS,  # WRONG — gold is sports. Assigned, not correct.
    "fx-008": Topic.OTHER,  # abstained. Costs coverage, not accuracy.
    "fx-009": Topic.OTHER,  # gold Other, correctly left alone
    "fx-010": Topic.SPORTS,  # gold Other, wrongly assigned — the diagnostic
    "fx-011": Topic.OTHER,  # unsure: outside both numbers entirely
    "fx-012": Topic.SPORTS,  # unsure: outside both numbers entirely
}


def _predictions(script: dict[str, Topic]) -> Predictions:
    return {row_id: Prediction(topic=topic, confidence=0.9) for row_id, topic in script.items()}


@pytest.fixture
def scored() -> ClassificationMetrics:
    return score_classification(load(FIXTURE).rows, _predictions(SCRIPT))


def test_coverage_is_seven_of_eight(scored: ClassificationMetrics) -> None:
    assert scored.assigned == 7
    assert scored.scorable_rows == 8
    assert scored.coverage == pytest.approx(0.875)


def test_accuracy_on_assigned_is_six_of_seven(scored: ClassificationMetrics) -> None:
    assert scored.correct == 6
    assert scored.accuracy_on_assigned == pytest.approx(6 / 7)


def test_abstaining_costs_coverage_and_not_accuracy() -> None:
    """The trade the second number exists to expose.

    fx-008 was abstained on. Assigning it *wrongly* instead leaves coverage higher and
    accuracy lower — if a change moves both in the same direction, the population is wrong.
    """
    rows = load(FIXTURE).rows
    abstained = score_classification(rows, _predictions(SCRIPT))
    guessed = score_classification(rows, _predictions({**SCRIPT, "fx-008": Topic.HEALTH}))

    assert abstained.accuracy_on_assigned is not None
    assert guessed.accuracy_on_assigned is not None
    assert guessed.coverage > abstained.coverage
    assert guessed.accuracy_on_assigned < abstained.accuracy_on_assigned


def test_genuine_other_rows_are_outside_both_numbers() -> None:
    """The rev-20 denominator.

    Counting uncategorisable articles means the metric moves when the *source roster*
    changes — reporting a regression in a classifier that did not change. Adding two more
    crosswords must leave both KR3 numbers untouched.
    """
    rows = list(load(FIXTURE).rows)
    extra = [
        EvalRow(id="fx-900", title="Sudoku No. 91", body=None, gold=Topic.OTHER),
        EvalRow(id="fx-901", title="Today's weather", body=None, gold=Topic.OTHER),
    ]
    widened_script = {**SCRIPT, "fx-900": Topic.OTHER, "fx-901": Topic.OTHER}

    baseline = score_classification(rows, _predictions(SCRIPT))
    widened = score_classification(rows + extra, _predictions(widened_script))

    assert widened.scorable_rows == baseline.scorable_rows
    assert widened.coverage == baseline.coverage
    assert widened.accuracy_on_assigned == baseline.accuracy_on_assigned


def test_unsure_rows_are_outside_both_numbers(scored: ClassificationMetrics) -> None:
    """Folding ``unsure`` into ``Other`` inflates coverage exactly where the classifier is
    weakest, because those rows cluster on the ambiguous articles. The fixture holds two;
    neither may appear in either denominator."""
    assert scored.scorable_rows == 8  # not 10
    assert scored.coverage == pytest.approx(7 / 8)  # not 7/10


def test_the_two_floors_compose_into_the_commitment(scored: ClassificationMetrics) -> None:
    """coverage x accuracy is the fraction of classifiable articles carrying a correct real
    topic. That only holds because both are computed over the same population — it is the
    arithmetic reason the denominator is what it is."""
    assert scored.accuracy_on_assigned is not None
    assert scored.coverage * scored.accuracy_on_assigned == pytest.approx(
        scored.correct / scored.scorable_rows
    )


def test_body_coverage_is_the_true_fraction(scored: ClassificationMetrics) -> None:
    """Two of the eight scorable rows carry a body. Hard-coding the value passes this and
    fails the all-headline case below."""
    assert scored.body_coverage == pytest.approx(0.25)


def test_an_all_headline_set_reports_zero_body_coverage() -> None:
    """The failure this exists for: a set cut while a backfill was still running holds
    headlines, its name says otherwise, and the hash agrees either way."""
    rows = [row for row in load(FIXTURE).rows if row.body is None]
    scored = score_classification(rows, _predictions(SCRIPT))
    assert scored.body_coverage == 0.0


def test_median_text_chars_is_measured_over_the_scored_rows(
    scored: ClassificationMetrics,
) -> None:
    """61.5 — the median of the eight scorable rows' text lengths, computed when the fixture
    was cut. A reader of a results table needs this beside a score, or two runs on different
    amounts of text look like two runs on the same amount."""
    assert scored.median_text_chars == pytest.approx(61.5)


def test_misassigned_other_is_reported_though_kr3_cannot_see_it(
    scored: ClassificationMetrics,
) -> None:
    """A classifier filing crosswords under Sports is invisible to both KR3 numbers — those
    rows are outside the population by design. Reported so the failure is at least visible."""
    assert scored.misassigned_other == 1
    assert scored.misassigned_other_rate == pytest.approx(0.5)


def test_scoring_is_invariant_to_row_order() -> None:
    """Predictions are paired to rows by id, never by position.

    Reversing the *rows* while handing over the same predictions is what makes this a real
    test: an implementation zipping two sequences would pair every row with a stranger's
    answer here and still report an entirely plausible number. Reversing the predictions
    instead would prove nothing at all — a mapping does not care what order it was built in.
    """
    rows = load(FIXTURE).rows
    forwards = score_classification(rows, _predictions(SCRIPT))
    reversed_rows = score_classification(tuple(reversed(rows)), _predictions(SCRIPT))
    assert reversed_rows == forwards


def test_a_missing_prediction_raises() -> None:
    """A metric quietly computed over a subset is worse than no metric."""
    partial = {k: v for k, v in _predictions(SCRIPT).items() if k != "fx-003"}
    with pytest.raises(ValueError, match="no prediction for 1 row"):
        score_classification(load(FIXTURE).rows, partial)


def test_a_set_with_nothing_scorable_raises() -> None:
    rows = [row for row in load(FIXTURE).rows if not row.gold_is_real_topic]
    with pytest.raises(ValueError, match="nothing to measure"):
        score_classification(rows, _predictions(SCRIPT))


def test_accuracy_is_none_rather_than_zero_when_nothing_was_assigned() -> None:
    """Zero claims the model got everything wrong; it made no claims at all. The difference
    survives into MLflow, where the key is omitted rather than logged as 0.0."""
    silent = dict.fromkeys(SCRIPT, Topic.OTHER)
    scored = score_classification(load(FIXTURE).rows, _predictions(silent))
    assert scored.coverage == 0.0
    assert scored.accuracy_on_assigned is None
    assert "accuracy_on_assigned" not in scored.as_mlflow_metrics()


def test_confidence_outside_the_unit_interval_is_refused() -> None:
    with pytest.raises(ValueError, match=r"confidence must be in \[0, 1\]"):
        Prediction(topic=Topic.WORLD, confidence=1.4)
