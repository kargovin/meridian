"""The predictor seam, and the stubs sprint 1 scores against.

The seam is one method: rows in, predictions out, keyed by row id. Anything satisfying it
can be scored — the trivial stubs here now, a TF-IDF baseline next, a fine-tuned model and
each bake-off configuration later. Nothing else in the harness changes when they arrive.

It is a ``Protocol`` rather than a ``Callable`` alias deliberately. ``Callable[..., X]``
switches off argument checking entirely: a renamed parameter on either side of such a seam
type-checks clean and passes every existing test, and fails at the first real call.
"""

from __future__ import annotations

import random
from collections.abc import Sequence
from typing import Protocol, runtime_checkable

from meridian_contract.taxonomy import Topic

from eval.evalset import ClassificationRow
from eval.metrics import Prediction, Predictions

#: Drawn from in stub predictions. ``Other`` is included: a predictor that can never abstain
#: could not produce a coverage below 1.0, and coverage is half of what we are measuring.
_DRAWABLE: tuple[Topic, ...] = tuple(Topic)


@runtime_checkable
class TopicClassifier(Protocol):
    """Anything the harness can score."""

    def predict(self, rows: Sequence[ClassificationRow]) -> Predictions:
        """Return one prediction per row, keyed by ``row.id``."""
        ...


class SeededStub:
    """Deterministic nonsense, for exercising the plumbing before a model exists.

    Its purpose is the reproducibility property: the same seed over the same set must
    produce identical metrics, or a run's ``git_sha`` tag is not evidence of anything.

    Each row is drawn from its own stream seeded by ``(seed, row.id)`` rather than from one
    stream consumed in row order, so a prediction depends on the article alone. Reordering a
    set — or scoring a subset of it — then leaves every other prediction unchanged, which is
    what makes a difference between two runs attributable to the thing that changed.
    """

    def __init__(self, seed: int) -> None:
        self.seed = seed

    def predict(self, rows: Sequence[ClassificationRow]) -> Predictions:
        out: dict[str, Prediction] = {}
        for row in rows:
            # A str seed is hashed with sha512, so this is stable across processes and
            # unaffected by PYTHONHASHSEED — unlike anything built on hash().
            rng = random.Random(f"{self.seed}:{row.id}")
            out[row.id] = Prediction(
                topic=rng.choice(_DRAWABLE),
                confidence=round(rng.uniform(0.3, 1.0), 4),
            )
        return out


class Oracle:
    """Reads the gold label and returns it. A ceiling, not a model.

    It exists to catch a broken metric. If the oracle does not score a perfect coverage and
    accuracy, the fault is in the measurement rather than in anything being measured —
    which is otherwise a very hard failure to notice, because a plausible-looking number is
    indistinguishable from a correct one.

    Rows with no real topic (``Other``, ``unsure``) get ``Other``: there is no right answer
    to return, and abstaining is the honest output.
    """

    def predict(self, rows: Sequence[ClassificationRow]) -> Predictions:
        return {
            row.id: Prediction(
                topic=row.gold if isinstance(row.gold, Topic) else Topic.OTHER,
                confidence=1.0,
            )
            for row in rows
        }


def build(name: str, **params: object) -> TopicClassifier:
    """Construct a predictor by name, as written in a run config.

    Raises ``ValueError`` on an unknown name or an unusable parameter, rather than falling
    back to a default — a run that silently scored something other than what its config
    names is a row in a results table that cannot be trusted.
    """
    if name == "seeded":
        seed = params.get("seed")
        if not isinstance(seed, int) or isinstance(seed, bool):
            raise ValueError("predictor 'seeded' requires an integer 'seed'")
        return SeededStub(seed=seed)
    if name == "oracle":
        if params:
            raise ValueError(f"predictor 'oracle' takes no parameters, got {sorted(params)}")
        return Oracle()
    raise ValueError(f"unknown predictor {name!r}. Known: oracle, seeded")
