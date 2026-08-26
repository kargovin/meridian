"""Reading and writing the runtime config plane (RFC §9).

Four things demand change without a deploy; this module is where the first of them lives. A
knob is *declared* here as an ``IntKnob`` — key, default, bounds — and stored as a row keyed by
that name. Declaring it in code rather than trusting whatever text is in the row is what makes
the value safe to read on a hot path: the bounds are enforced on write, and re-checked on read
so that a value written by hand through psql cannot put the poller outside them either.

⚠️ Reads never raise. A knob whose row is missing, unparseable or out of bounds falls back to
its declared default and reports the reason, because the caller is the discovery heartbeat and
a config plane that can stop ingestion is worse than one that ignores a bad value (§5.6
degradation posture: degrade predictably, never fail hard).

Writes are compare-and-set against ``updated_at``, for the same reason the registry's governing
fields are: an admin page renders the current value, and one submitted after an out-of-band
change would otherwise write the stale value back. Losing a cadence change that way is silent —
nothing errors, freshness just quietly reverts.

None of these commit. The caller owns the transaction.
"""

import datetime as dt
import logging
from collections.abc import Sequence
from dataclasses import dataclass

import sqlalchemy as sa
from sqlalchemy.orm import Session

from meridian.db.models import RuntimeConfig
from meridian.db.sources import StaleWrite

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class IntKnob:
    """An integer knob, with the bounds that make a value acceptable.

    The bounds are not tidiness. ``poll_interval_seconds`` is the primary freshness lever (§7.1),
    which means the two directions fail differently: too low hammers publishers and spends the
    politeness budget FR-I3 protects, too high loses freshness with no code change, no failing
    test and no diff for anyone to review. The floor and the ceiling are the cheap half of that;
    the alarm that notices someone sitting at the ceiling is a separate story.
    """

    key: str
    default: int
    minimum: int
    maximum: int
    summary: str

    def check(self, value: int) -> None:
        if not self.minimum <= value <= self.maximum:
            raise ValueError(
                f"{self.key} must be between {self.minimum} and {self.maximum}, got {value}"
            )


#: How long discovery waits between polls. Read at the end of every cycle rather than at
#: startup, so a change takes effect on the next tick without a restart.
POLL_INTERVAL_SECONDS = IntKnob(
    key="poll_interval_seconds",
    default=300,
    minimum=60,
    maximum=3600,
    summary="Seconds between discovery polls. The largest single term in the freshness budget.",
)

#: How long the acquire stage waits between batches. Config rather than a constant for the
#: reason the poll cadence is (T6): it is a term in end-to-end freshness — an article waits
#: the discovery interval, then this one — and raising it loses freshness with no code change
#: and no failing test. The ceiling is what stops it being used to stall the pipeline.
ACQUIRE_INTERVAL_SECONDS = IntKnob(
    key="acquire_interval_seconds",
    default=30,
    minimum=5,
    maximum=300,
    summary="Seconds between acquire batches. Added to the poll interval, this is how long "
    "a discovered article waits before it is normalized.",
)

#: Every declared knob, in the order the admin surface lists them.
KNOBS: tuple[IntKnob, ...] = (POLL_INTERVAL_SECONDS, ACQUIRE_INTERVAL_SECONDS)


def get_int(session: Session, knob: IntKnob) -> int:
    """The knob's current value, or its declared default if the stored one is unusable."""
    stored = session.get(RuntimeConfig, knob.key)
    if stored is None:
        log.warning("runtime config %s has no row; using default %d", knob.key, knob.default)
        return knob.default
    try:
        value = int(stored.value)
        knob.check(value)
    except ValueError as exc:
        log.warning(
            "runtime config %s holds %r, which is unusable (%s); using default %d",
            knob.key,
            stored.value,
            exc,
            knob.default,
        )
        return knob.default
    return value


@dataclass(frozen=True)
class KnobState:
    """A knob as an operator needs to see it: what is stored, and what is actually in force.

    ⚠️ These differ whenever a stored value is unusable. Reads fall back rather than raising —
    the caller is the discovery heartbeat and a config plane that can stop ingestion is worse
    than one that ignores a bad value — but a page that then renders the stored number is
    showing a value nothing is using. For the one knob whose whole risk is that a regression
    goes unnoticed because nothing fails, that page is the only place anyone would look.
    """

    knob: IntKnob
    row: RuntimeConfig | None
    effective: int

    @property
    def rejected(self) -> bool:
        """The stored value is present and is not the one in force."""
        return self.row is not None and self.row.value != str(self.effective)

    @property
    def missing(self) -> bool:
        return self.row is None


def states(session: Session, knobs: Sequence[IntKnob] = KNOBS) -> list[KnobState]:
    """Every declared knob, with its stored row and the value actually in force."""
    stored = rows(session, knobs)
    return [
        KnobState(knob=knob, row=stored.get(knob.key), effective=get_int(session, knob))
        for knob in knobs
    ]


def row(session: Session, knob: IntKnob) -> RuntimeConfig | None:
    """The stored row, for an admin surface that needs its version token."""
    return session.get(RuntimeConfig, knob.key)


def rows(session: Session, knobs: Sequence[IntKnob] = KNOBS) -> dict[str, RuntimeConfig]:
    """Every declared knob's row, keyed by name. A knob missing from the mapping has no row."""
    keys = [knob.key for knob in knobs]
    found = session.scalars(sa.select(RuntimeConfig).where(RuntimeConfig.key.in_(keys))).all()
    return {stored.key: stored for stored in found}


def set_int(
    session: Session, knob: IntKnob, *, value: int, expected_updated_at: dt.datetime
) -> RuntimeConfig | None:
    """Write a knob, refusing a value outside its bounds or a write against a stale token."""
    knob.check(value)
    stored = session.get(RuntimeConfig, knob.key)
    if stored is None:
        return None
    # SELECT ... FOR UPDATE, so the compare and the write are one step — see sources._claim for
    # why the comparison alone is not enough.
    session.refresh(stored, with_for_update=True)
    if stored.updated_at != expected_updated_at:
        raise StaleWrite(
            f"runtime config {knob.key} changed at {stored.updated_at.isoformat()}, "
            f"after the page offering this write was rendered"
        )
    stored.value = str(value)
    session.flush()
    return stored
