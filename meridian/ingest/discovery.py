"""One discovery cycle: poll every pollable feed, record what is new (FR-I1).

⚠️ Unlike the repositories, this owns its transaction and commits per feed. That is what makes
a dead feed survivable: a failure mid-feed rolls that feed back and the next one starts clean,
where a single cycle-wide transaction would lose the whole cycle's work to one bad publisher.
Re-polling is idempotent, so a rolled-back feed simply reappears next cycle.

A feed's items are inserted and enqueued together in that one transaction. A record without its
work row is an article that has stopped moving with no error, no retry and no dead-letter row
(RFC §6.2) — the failure the stage machinery exists to make impossible.
"""

import logging
import time
from collections import defaultdict
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from meridian_contract import ENTRY_STATE, AcquisitionTier, DiscoveryMethod, owed_stage
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from meridian.db import feeds as feeds_repo
from meridian.db import poll_state
from meridian.db.models import CanonicalRecord, Feed, PipelineWork, Source
from meridian.ingest.fetch import DEFAULT_USER_AGENT, Fetcher
from meridian.ingest.parse import FeedItem, FeedUnreadable, parse

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class CycleReport:
    """What one cycle did. Every field is a count a human would ask for during an incident."""

    polled: int = 0
    not_modified: int = 0
    failed: int = 0
    #: Records created. Steady state is a handful; zero is normal and not a fault.
    discovered: int = 0
    #: Feed entries with no usable title or link.
    skipped_items: int = 0
    #: Feeds the registry permits but this cycle cannot read — a discovery method we do not
    #: implement yet. Not a failure and not a success; it must not read as either.
    skipped_feeds: int = 0
    #: Wall time of the cycle. Reported because it is a term in the freshness budget and not a
    #: constant: an article's real wait is the interval plus its feed's position in the cycle,
    #: and the cycle grows with the feed count and with how politely each publisher is polled.
    duration_seconds: float = 0.0

    def __add__(self, other: "CycleReport") -> "CycleReport":
        return CycleReport(
            polled=self.polled + other.polled,
            not_modified=self.not_modified + other.not_modified,
            failed=self.failed + other.failed,
            discovered=self.discovered + other.discovered,
            skipped_items=self.skipped_items + other.skipped_items,
            skipped_feeds=self.skipped_feeds + other.skipped_feeds,
            duration_seconds=self.duration_seconds + other.duration_seconds,
        )


def _body_text(feed: Feed, item: FeedItem) -> str | None:
    """The article body, when the feed genuinely carries one.

    Gated on the registered tier rather than on the content merely being present, because the
    tier is a human determination about what this feed ships and the presence of a ``<content>``
    element is not. On the v1 roster this returns None every time — no mainstream publisher puts
    article text in its feed, so tier 1 has no members and extraction is the default path. The
    branch exists so that ``1_full_feed`` is a value the registry can act on rather than one an
    operator can set to no effect.

    Rights are deliberately not consulted. What we may publish is read at the point of use from
    the registry (RFC §5.2, rev 20); a copy taken here would answer for the rights held at
    acquisition and keep answering after a downgrade.
    """
    if feed.acquisition_tier is not AcquisitionTier.FULL_FEED:
        return None
    return item.content


def _insert(session: Session, feed: Feed, item: FeedItem) -> bool:
    """Create the record and its work row, or do nothing. True if it was new.

    Idempotency is the schema's, not this function's: ``UNIQUE(source_id, guid)`` and
    ``UNIQUE(url_canonical)`` are what stop the same item being stored twice, and DO NOTHING
    covers both. An item seen in twenty consecutive polls is inserted once and enqueued once,
    because the enqueue is conditional on the insert having happened.

    ``url_canonical`` receives the link exactly as the feed gave it, tracking parameters and
    all. Canonicalising belongs to the normalize stage, which runs seconds later and rewrites
    it; nothing reads the column before then.
    """
    article_id = session.scalar(
        insert(CanonicalRecord)
        .values(
            source_id=feed.source_id,
            feed_id=feed.feed_id,
            guid=item.guid,
            url_canonical=item.link,
            title=item.title,
            lede=item.summary,
            body_text=_body_text(feed, item),
            published_at=item.published_at,
            pipeline_state=ENTRY_STATE,
        )
        .on_conflict_do_nothing()
        .returning(CanonicalRecord.article_id)
    )
    if article_id is None:
        return False
    stage = owed_stage(ENTRY_STATE)
    if stage is None:  # pragma: no cover - the entry state owes a stage by construction
        raise RuntimeError(f"{ENTRY_STATE} owes no stage; the article chain is empty")
    session.add(PipelineWork(article_id=article_id, stage=stage))
    return True


def _poll_one(session: Session, feed: Feed, source: Source, fetcher: Fetcher) -> CycleReport:
    """Fetch, parse and store one feed. Commits."""
    headers = poll_state.validators(poll_state.get(session, feed.feed_id))
    # ⚠️ Commit before going to the network. The read above opens a transaction, and a
    # transaction held open across a fetch pins the database's oldest xmin — so VACUUM cannot
    # reclaim dead tuples anywhere in the database for as long as the slowest publisher takes
    # to answer. The volume this runs on cannot be expanded (T3), which is what turns a slow
    # publisher into a storage problem for everything else.
    session.commit()

    result = fetcher(
        feed.url,
        user_agent=source.user_agent or DEFAULT_USER_AGENT,
        headers=headers,
    )

    if result.not_modified:
        poll_state.record(session, feed.feed_id, status=304)
        session.commit()
        return CycleReport(polled=1, not_modified=1)

    body = result.body
    if result.status != 200 or body is None:
        poll_state.record(
            session, feed.feed_id, status=result.status, error=result.error or "no body"
        )
        session.commit()
        log.warning(
            "feed %d (%s) poll failed: status=%s %s",
            feed.feed_id,
            feed.url,
            result.status,
            result.error,
        )
        return CycleReport(polled=1, failed=1)

    try:
        parsed = parse(body)
    except FeedUnreadable as exc:
        poll_state.record(session, feed.feed_id, status=result.status, error=f"unreadable: {exc}")
        session.commit()
        log.warning("feed %d (%s) is unreadable: %s", feed.feed_id, feed.url, exc)
        return CycleReport(polled=1, failed=1)

    discovered = sum(_insert(session, feed, item) for item in parsed.items)
    poll_state.record(
        session,
        feed.feed_id,
        status=result.status,
        etag=result.etag,
        last_modified=result.last_modified,
    )
    session.commit()
    return CycleReport(polled=1, discovered=discovered, skipped_items=parsed.skipped)


def _interleave(due: Sequence[Feed]) -> list[Feed]:
    """Order feeds so consecutive requests go to different publishers.

    Politeness is per publisher, so polling one publisher's feeds back to back means waiting out
    the full gap between each — the worst possible order, and the one feed id order produces.
    Round-robin instead: by the time the cycle returns to a publisher it has already spent the
    other publishers' requests, and that time counts towards the gap.

    Measured, 8 publishers x 3 feeds at 5 requests/min: 194 s in feed order, 26 s interleaved.
    Identical politeness — every publisher still sees the same minimum spacing — for a seventh
    of the wall time, which is a seventh of this term of the freshness budget (RFC §7.1).
    """
    by_source: dict[int, list[Feed]] = defaultdict(list)
    for feed in due:
        by_source[feed.source_id].append(feed)

    ordered: list[Feed] = []
    queues = list(by_source.values())
    while queues:
        for queue in queues:
            ordered.append(queue.pop(0))
        queues = [queue for queue in queues if queue]
    return ordered


def _pace(
    source: Source,
    last_request_at: dict[int, float],
    sleep: Callable[[float], None],
    clock: Callable[[], float],
) -> None:
    """Hold off long enough that a publisher's feeds share one politeness budget (FR-I3).

    ``rate_limit_per_min`` is a promise about one host, and it lives on the publisher rather
    than the feed precisely because N feeds each honouring it independently would exceed it
    N-fold. Spacing here is the whole of that promise for discovery; the general per-domain
    limiter arrives with tier-3 acquisition, which is the path with the volume.
    """
    previous = last_request_at.get(source.source_id)
    now = clock()
    if previous is not None:
        gap = 60.0 / source.rate_limit_per_min
        remaining = gap - (now - previous)
        if remaining > 0:
            sleep(remaining)
            now = clock()
    last_request_at[source.source_id] = now


def run_cycle(
    session: Session,
    fetcher: Fetcher,
    *,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> CycleReport:
    """Poll every feed the registry permits, once.

    ⚠️ Every feed is polled inside its own try/except. One publisher timing out, answering with
    something that is not a feed, or provoking a bug in our own parsing must not stop the rest
    of the roster being polled in this cycle — a single flaky source freezing ingestion for
    everyone is the failure this shape exists to prevent (§5.6: degrade predictably).
    """
    pollable = feeds_repo.pollable(session)
    due = [feed for feed in pollable if feed.discovery_method is DiscoveryMethod.RSS]

    # A feed the registry permits but this cycle cannot read. Counted rather than dropped
    # silently: a sitemap source is registered correctly and is simply not implemented yet, and
    # without this it appears in no count, no log line and no poll-state row — indistinguishable
    # from a publisher nobody added.
    skipped = len(pollable) - len(due)
    report = CycleReport(skipped_feeds=skipped)
    if skipped:
        log.info("%d registered feed(s) skipped: discovery method not implemented", skipped)
    if not due:
        return report

    publishers = _publishers(session, due)
    last_request_at: dict[int, float] = {}
    started = clock()

    for feed in _interleave(due):
        source = publishers[feed.source_id]
        try:
            _pace(source, last_request_at, sleep, clock)
            report += _poll_one(session, feed, source, fetcher)
        except Exception as exc:
            session.rollback()
            log.exception("feed %d (%s) raised during poll", feed.feed_id, feed.url)
            _record_crash(session, feed, exc)
            report += CycleReport(polled=1, failed=1)
    return report + CycleReport(duration_seconds=clock() - started)


def _record_crash(session: Session, feed: Feed, exc: Exception) -> None:
    """Record a poll that raised, in a transaction of its own.

    ⚠️ The rollback above discards the article inserts *and* the poll-state write, because they
    share a transaction. Without this the failure class the except clause exists to survive is
    the one class that leaves no trace: ``consecutive_failures`` stays wherever it was, and a
    feed that has raised on every poll for a week still reads ``last_status=200`` on the admin
    surface. That counter exists to separate a rotted URL from an unreachable publisher, and it
    cannot count what it never sees.

    Its own try/except because this loop must not die: a failure to record a failure is worth a
    log line, not the rest of the roster.
    """
    try:
        poll_state.record(session, feed.feed_id, status=None, error=f"{type(exc).__name__}: {exc}")
        session.commit()
    except Exception:
        session.rollback()
        log.exception("could not record the failed poll of feed %d", feed.feed_id)


def _publishers(session: Session, due: Sequence[Feed]) -> dict[int, Source]:
    """The publisher of every due feed, read once rather than per feed."""
    by_source: dict[int, list[Feed]] = defaultdict(list)
    for feed in due:
        by_source[feed.source_id].append(feed)
    found = session.query(Source).filter(Source.source_id.in_(by_source)).all()
    return {source.source_id: source for source in found}
