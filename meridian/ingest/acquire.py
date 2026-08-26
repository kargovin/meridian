"""The acquire stage: a discovered record becomes a usable one (FR-I4, FR-I7).

The first stage handler in the system, and the first code that consumes a work row rather
than creating one. Discovery writes what the feed said; everything after this reads the
canonical record and never the feed, so this is the last point at which the feed's own
spelling of things matters.

⚠️ **``content_hash`` and ``simhash`` are deliberately left NULL here.** Both are specified
over the article *body* (2.1.2 §3.2), and no source on the v1 roster ships a body in its feed
— PRD §5.1 tier 1 has no members, so ``body_text`` is empty system-wide until tier-3
extraction lands. Computing them over the headline instead was measured and rejected: over
~10 tokens SimHash recognises about 6% of genuine near-duplicates against 100% over a full
article, and a SHA-256 of a headline declares two different articles byte-identical, which
collapses a real article into an ``AlternateCopy`` and inflates the distinct-source count that
FR-S6 gates summarization on. A NULL column is visibly empty; a populated one that is wrong
per-publisher is not. They are filled by the stage that obtains a body.

Retry and backoff are not here. A raising article keeps its claimed row with ``attempts``
already incremented, and is picked up again once the lease expires.
"""

import datetime as dt
import logging
import os
import socket
from dataclasses import dataclass

from meridian_contract import Stage, TerminalReason
from sqlalchemy.orm import Session

from meridian.db import work_queue
from meridian.db.models import CanonicalRecord, PipelineWork
from meridian.ingest.normalize import detect_language, language_input, strip_html

log = logging.getLogger(__name__)

STAGE = Stage.ACQUIRE


def worker_name() -> str:
    """Who claimed a row. Host and pid, so a stuck claim points at a process."""
    return f"acquire@{socket.gethostname()}:{os.getpid()}"


@dataclass(frozen=True)
class AcquireReport:
    """What one batch did. Every field is a count a human would ask for during an incident."""

    claimed: int = 0
    #: Articles normalized and handed to the next stage.
    acquired: int = 0
    #: Articles stopped by FR-I7. Steady state is small and non-zero; a spike means either a
    #: publisher changed language or the detector is being fed something it should not be.
    dropped: int = 0
    failed: int = 0

    def __add__(self, other: "AcquireReport") -> "AcquireReport":
        return AcquireReport(
            claimed=self.claimed + other.claimed,
            acquired=self.acquired + other.acquired,
            dropped=self.dropped + other.dropped,
            failed=self.failed + other.failed,
        )


def normalize(session: Session, article: CanonicalRecord) -> bool:
    """Fill in what the feed could not, and decide whether the article continues.

    Returns True to continue, False if FR-I7 stops it. Writes to the session; commits nothing.

    The lede is rewritten in place rather than kept alongside its markup. The published
    original is the publisher's; what we store is what every later stage reads, and keeping
    both would mean every reader choosing, which is how one of them chooses wrong.
    """
    article.lede = strip_html(article.lede)
    verdict = detect_language(language_input(article.title, article.lede))
    article.language = verdict.language
    return not verdict.drop


def handle(session: Session, work: PipelineWork) -> bool:
    """Run the stage for one work row and move it on. Returns True if the article continues.

    Commits once, at the end. That single commit is what makes the stage output, the state
    move, the dequeue and the successor enqueue one atomic step — split them and an article
    can end up advanced with nothing owed, or owing a stage it has already had.
    """
    article = session.get(CanonicalRecord, work.article_id)
    if article is None:
        raise ValueError(f"work {work.work_id} names article {work.article_id}, which is gone")

    if normalize(session, article):
        work_queue.advance(session, work)
        session.commit()
        return True

    work_queue.terminate(session, work, TerminalReason.DROPPED_LANGUAGE)
    session.commit()
    log.info(
        "article %d dropped: language=%s (FR-I7)",
        article.article_id,
        article.language or "undetermined",
    )
    return False


def run_batch(
    session: Session,
    *,
    lease: dt.timedelta,
    limit: int = 50,
    worker: str | None = None,
) -> AcquireReport:
    """Claim up to ``limit`` due articles and normalize each of them.

    ⚠️ Each article is handled inside its own try/except, for the reason discovery polls each
    feed inside one: a single record that provokes a bug must not stop the rest of the batch.
    Unlike discovery, the failure leaves a trace without any help — ``claim`` has already
    committed the row with ``attempts`` incremented — but the trace says only *how often*, so
    the message is written too.
    """
    claimed = work_queue.claim(
        session, stage=STAGE, worker=worker or worker_name(), lease=lease, limit=limit
    )
    report = AcquireReport(claimed=len(claimed))
    for work in claimed:
        try:
            report += (
                AcquireReport(acquired=1) if handle(session, work) else AcquireReport(dropped=1)
            )
        except Exception as exc:
            session.rollback()
            log.exception("article %s raised during acquire", work.article_id)
            _record_failure(session, work, exc)
            report += AcquireReport(failed=1)
    return report


def _record_failure(session: Session, work: PipelineWork, exc: Exception) -> None:
    """Write why a row failed, in a transaction of its own.

    ⚠️ The rollback above discards everything the handler did, and a message written inside
    that transaction would go with it — leaving the one failure class this guard exists to
    survive as the one that says nothing about itself. ``attempts`` alone reports that a row
    keeps failing and never what it fails on.

    Its own try/except, because a failure to record a failure is worth a log line rather than
    the rest of the batch.
    """
    try:
        row = session.get(PipelineWork, work.work_id)
        if row is not None:
            row.last_error = f"{type(exc).__name__}: {exc}"[:2000]
            session.commit()
    except Exception:
        session.rollback()
        log.exception("could not record the failure of work %d", work.work_id)
