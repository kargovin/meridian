"""The dedup stage: an article another publisher already ran is folded into the one we hold
(FR-I5, RFC §5.2).

Each claimed article goes down one ladder:

1. No body — headline-only, which is most of the roster. Nothing to compare; it continues.
2. Fingerprint the body and store it, so later arrivals can be compared against this one.
3. Look for a match **from another publisher**: identical ``content_hash`` first, then a
   fingerprint within ``hamming_bits``. A match is a second copy of a story we already hold,
   and the arrival is collapsed into it (``work_queue.collapse``) — the record goes, the
   publisher is kept as an ``AlternateCopy``.
4. Otherwise look for a match from **the same publisher**, and only log it. Two bodies from one
   publisher that match are a boilerplate signal (a shared press-release footer), not a story
   run twice; collapsing them would merge distinct articles.
5. Otherwise it is a new story and continues.

Other publishers are asked first, not the nearest match overall: a same-publisher boilerplate
twin must not hide a real cross-publisher copy from the collapse.

⚠️ The arrival always collapses into the record already held, never the other way round. A
representative has passed this stage, and swapping would mean collapsing a record whose
downstream work (classification, cluster membership) is already done.

⚠️ Near matching is not transitive. A within 3 bits of B and B within 3 bits of C does not put
A within 3 of C, so the representative an arrival lands on depends on which records already
passed. Each arrival is compared only against representatives; it is never re-examined when a
later record arrives.

Candidates are records that have *passed* this stage, keyed on ``pipeline_state`` rather than
``simhash IS NOT NULL``: a body too short to shingle legitimately has no fingerprint and can
still be matched exactly. Two copies claimed in the same batch see each other only because
each article is committed before the next is handled — the second finds the first already
passed. That holds with one dedup process; two concurrent processes can each pass their copy
before seeing the other's.
"""

import datetime as dt
import logging
import os
import socket
from dataclasses import dataclass, fields

import sqlalchemy as sa
from meridian_contract import ARTICLE_CHAIN, STATE_AFTER_STAGE, PipelineState, Stage
from sqlalchemy.dialects.postgresql import BIT
from sqlalchemy.orm import Session

from meridian.db import work_queue
from meridian.db.models import CanonicalRecord, PipelineWork
from meridian.db.simhash import simhash_to_db
from meridian.dedup.fingerprint import BITS, fingerprint

log = logging.getLogger(__name__)

STAGE = Stage.DEDUP


def _states_from(state: PipelineState) -> list[PipelineState]:
    chain = [after for _, after in ARTICLE_CHAIN]
    return chain[chain.index(state) :]


#: States of a record that has been through this stage — the candidates an arrival is compared
#: with. Derived from the chain, so a stage added after dedup is covered without an edit here.
_PASSED = _states_from(STATE_AFTER_STAGE[STAGE] or PipelineState.DEDUPED)


def worker_name() -> str:
    """Who claimed a row. Host and pid, so a stuck claim points at a process."""
    return f"dedup@{socket.gethostname()}:{os.getpid()}"


@dataclass(frozen=True)
class DedupReport:
    """What one batch did.

    Each claimed row lands in exactly one of ``advanced``, ``collapsed``, ``failed`` or
    ``stale``. ``collapsed`` splits into ``exact`` and ``near``; of the advanced,
    ``no_body`` had nothing to compare and ``same_publisher`` matched only its own publisher.
    """

    claimed: int = 0
    #: Articles handed to classify as stories of their own.
    advanced: int = 0
    #: Articles folded into a record from another publisher.
    collapsed: int = 0
    #: Rows that raised. Given back with backoff, and dead-lettered on the attempt
    #: ``MAX_ATTEMPTS`` names.
    failed: int = 0
    #: Rows another worker had already completed — an expired lease, not a fault.
    stale: int = 0
    #: Headline-only articles, passed straight through.
    no_body: int = 0
    #: Collapses on an identical ``content_hash``.
    exact: int = 0
    #: Collapses on a fingerprint within the distance, bodies not identical.
    near: int = 0
    #: Of the collapsed, copies that already had a note, dropped without a second one — a copy
    #: discovery re-inserted while it was being collapsed.
    already_noted: int = 0
    #: Of the failed, rows given up on: the article stops here, marked failed.
    dead_lettered: int = 0
    #: Matches within one publisher, logged and not collapsed. A steady count from one
    #: publisher is boilerplate in its bodies.
    same_publisher: int = 0

    def __add__(self, other: "DedupReport") -> "DedupReport":
        return DedupReport(
            **{f.name: getattr(self, f.name) + getattr(other, f.name) for f in fields(self)}
        )


@dataclass(frozen=True)
class Match:
    article_id: int
    #: Hamming distance between the fingerprints; 0 for an exact match.
    distance: int
    exact: bool


def find_match(
    session: Session,
    article: CanonicalRecord,
    simhash: int | None,
    *,
    hamming_bits: int,
    same_publisher: bool,
) -> Match | None:
    """The record ``article`` is a copy of, among those already past this stage, or ``None``.

    ``simhash`` is the arrival's fingerprint as stored (signed); ``None`` skips near matching.
    ``same_publisher`` picks which side of the publisher line to search.

    Exact before near, and within each the lowest ``article_id`` — the oldest record — so a
    tie resolves the same way every time. Near matches go to the smallest distance first.
    """
    candidates = [
        CanonicalRecord.pipeline_state.in_(_PASSED),
        # A record that stopped for good after dedup is no longer a story we carry forward;
        # collapsing a copy into it would lose the story altogether.
        CanonicalRecord.terminal_reason.is_(None),
        CanonicalRecord.article_id != article.article_id,
        (CanonicalRecord.source_id == article.source_id)
        if same_publisher
        else (CanonicalRecord.source_id != article.source_id),
    ]

    if article.content_hash is not None:
        exact = session.scalar(
            sa.select(CanonicalRecord.article_id)
            .where(CanonicalRecord.content_hash == article.content_hash, *candidates)
            .order_by(CanonicalRecord.article_id)
            .limit(1)
        )
        if exact is not None:
            return Match(article_id=exact, distance=0, exact=True)

    if simhash is None:
        return None
    # ⚠️ PostgreSQL 16 has bit_count(bit) and bit_count(bytea) but no bit_count(bigint), so the
    # XOR is cast to bit(64). The cast keeps the two's-complement bit pattern, which is what
    # makes the signed storage of an unsigned fingerprint harmless here. No index serves this:
    # it reads every candidate's fingerprint.
    distance = sa.func.bit_count(
        sa.cast(CanonicalRecord.simhash.op("#")(sa.literal(simhash, sa.BigInteger)), BIT(BITS)),
        type_=sa.Integer,
    )
    row = session.execute(
        sa.select(CanonicalRecord.article_id, distance)
        .where(CanonicalRecord.simhash.is_not(None), distance <= hamming_bits, *candidates)
        .order_by(distance, CanonicalRecord.article_id)
        .limit(1)
    ).first()
    if row is None:
        return None
    return Match(article_id=row[0], distance=row[1], exact=False)


def handle(session: Session, work: PipelineWork, *, hamming_bits: int) -> DedupReport:
    """Run the stage for one work row and end it — advanced or collapsed. Commits once."""
    article = session.get(CanonicalRecord, work.article_id)
    if article is None:
        raise ValueError(f"work {work.work_id} names article {work.article_id}, which is gone")

    if article.body_text is None:
        work_queue.advance(session, work)
        session.commit()
        return DedupReport(advanced=1, no_body=1)
    if article.content_hash is None:
        # RFC §5.1: acquire writes the hash with the body. A body without one would be
        # invisible to exact matching and would pass as unique with nothing said.
        raise ValueError(f"article {article.article_id} holds a body but no content_hash")

    fp = fingerprint(article.body_text)
    stored = simhash_to_db(fp) if fp is not None else None

    match = find_match(session, article, stored, hamming_bits=hamming_bits, same_publisher=False)
    if match is not None:
        article_id = article.article_id
        noted = work_queue.collapse(session, work, into=match.article_id)
        session.commit()
        log.info(
            "article %d collapsed into %d (%s, distance %d)%s",
            article_id,
            match.article_id,
            "exact" if match.exact else "near",
            match.distance,
            "" if noted else "; its copy was already noted",
        )
        return DedupReport(
            collapsed=1,
            exact=int(match.exact),
            near=int(not match.exact),
            already_noted=int(not noted),
        )

    report = DedupReport(advanced=1)
    twin = find_match(session, article, stored, hamming_bits=hamming_bits, same_publisher=True)
    if twin is not None:
        log.info(
            "article %d matches %d from the same publisher (%s, distance %d); not collapsed",
            article.article_id,
            twin.article_id,
            "exact" if twin.exact else "near",
            twin.distance,
        )
        report += DedupReport(same_publisher=1)

    article.simhash = stored
    work_queue.advance(session, work)
    session.commit()
    return report


def run_batch(
    session: Session,
    *,
    lease: dt.timedelta,
    hamming_bits: int,
    limit: int = 50,
    worker: str | None = None,
) -> DedupReport:
    """Claim up to ``limit`` due articles and run the stage on each, committing per article.

    ⚠️ Per article, not per batch: two copies of one story claimed together find each other
    only because the first is committed as passed before the second is compared.

    Each article is handled inside its own try/except so one record that provokes a bug does
    not stop the rest of the batch.
    """
    worker = worker or worker_name()
    claimed = work_queue.claim(session, stage=STAGE, worker=worker, lease=lease, limit=limit)
    report = DedupReport(claimed=len(claimed))
    for work in claimed:
        # ⚠️ Read before the try: a rollback expires the instance, and touching it afterwards
        # re-queries a row that may be gone — raising from inside the handler meant to
        # contain failures.
        work_id, article_id = work.work_id, work.article_id
        try:
            report += handle(session, work, hamming_bits=hamming_bits)
        except work_queue.StaleWork:
            session.rollback()
            log.info("work %d was already completed by another worker", work_id)
            report += DedupReport(stale=1)
        except Exception as exc:
            session.rollback()
            log.exception("article %s raised during dedup", article_id)
            dead = _record_failure(session, work_id, exc, worker=worker)
            report += DedupReport(failed=1, dead_lettered=int(dead))
    return report


def _record_failure(session: Session, work_id: int, exc: Exception, *, worker: str) -> bool:
    """Give a failed row back with backoff, or give up on it. True if it was dead-lettered.

    In a transaction of its own — the rollback took the handler's. ``claim()`` counts attempts
    and never stops, so stopping is this stage's job: without it a row that always raises is
    re-claimed every lease, forever, and the article sits at ``acquired`` with only
    ``last_error`` to say why. Unlike acquire, the article cannot continue without this stage —
    one that skipped dedup could be counted twice toward the two-source gate (FR-S6) — so the
    last attempt dead-letters rather than advancing.

    ⚠️ Ownership is checked against ``worker``, never against the row as reloaded. ``release``
    and ``dead_letter`` match on the instance's own ``claimed_by``, and a row reloaded after
    another worker reclaimed it (our lease ran out mid-article) names *them* — so the check
    compared them with themselves, and we released or dead-lettered a claim that was not ours,
    marking the article failed while they were still working on it.
    """
    try:
        row = session.get(PipelineWork, work_id)
        if row is None:
            return False
        if row.claimed_by != worker:
            log.info(
                "work %d was reclaimed by %s; its failure is not ours to record",
                work_id,
                row.claimed_by,
            )
            session.rollback()
            return False
        error = f"{type(exc).__name__}: {exc}"
        if row.attempts >= work_queue.MAX_ATTEMPTS:
            work_queue.dead_letter(session, row, error=error)
            session.commit()
            log.error("work %d dead-lettered after %d attempts: %s", work_id, row.attempts, error)
            return True
        retry_at = dt.datetime.now(dt.UTC) + work_queue.backoff_after(row.attempts)
        work_queue.release(session, row, retry_at=retry_at, error=error)
        session.commit()
    except Exception:
        session.rollback()
        log.exception("could not record the failure of work %d", work_id)
    return False
