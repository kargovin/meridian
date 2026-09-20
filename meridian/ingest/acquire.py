"""The acquire stage: a discovered record becomes a usable one (FR-I4, FR-I7).

Discovery writes what the feed said; everything after this reads the canonical record and
never the feed, so this is the last point at which the feed's own spelling of things matters.
Two things happen here. The lede is cleaned and the language decided (FR-I7), as before. And
the article's body is obtained where the registry says it can be — the only place in the
system that fetches an article page.

The body comes from a ladder of reasons *not* to fetch, cheapest first (``_obtain_body``):
a body already on the record, a stopped publisher, no body rights, no route for the feed's
tier, ``robots.txt``, a pacing wait longer than the lease. Nothing reaches the network before
the rights rung. Every rung but the network failures ends the same way — the article continues
headline-only. A body is an improvement to a record, not a precondition for one: a headline we
could not fetch the page for is still a real headline that belongs in a cluster. Only a
transient failure (a 5xx, no answer) puts the row back for later, because only there can a
retry change the answer.

``content_hash`` is computed for every body the stage ends holding — fetched here, or shipped
in a tier-1 feed and stored by discovery — so RFC §5.1's invariant
``content_hash IS NOT NULL ⟺ body_text IS NOT NULL`` holds from the moment a record leaves
this stage. ``simhash`` stays NULL: it needs features and weights, which is dedup's design.

⚠️ The fetched page is held in memory for the extraction call and nowhere else (Legal A-L1).
No column, log line or file receives it.

Retry policy beyond what a 503 needs is not here. A raising article keeps its claimed row with
``attempts`` already incremented and is picked up again once the lease expires.
"""

import datetime as dt
import logging
import os
import socket
from collections.abc import Sequence
from dataclasses import dataclass, fields

import sqlalchemy as sa
from meridian_contract import AcquisitionTier, BodyProvenance, Stage, TerminalReason
from sqlalchemy.orm import Session

from meridian.db import sources as sources_repo
from meridian.db import work_queue
from meridian.db.models import CanonicalRecord, Feed, PipelineWork, Source
from meridian.ingest.adapters import Adapter, adapter_for
from meridian.ingest.extract import extract
from meridian.ingest.fetch import DEFAULT_USER_AGENT, Fetcher
from meridian.ingest.normalize import content_hash, detect_language, language_input, strip_html
from meridian.ingest.pacing import Pacer, round_robin
from meridian.ingest.robots import RobotsCache

log = logging.getLogger(__name__)

STAGE = Stage.ACQUIRE


def worker_name() -> str:
    """Who claimed a row. Host and pid, so a stuck claim points at a process."""
    return f"acquire@{socket.gethostname()}:{os.getpid()}"


@dataclass(frozen=True)
class Network:
    """Everything the stage needs to reach a publisher.

    One of each per process, shared with discovery — the pacer's promise is per host and two
    pacers keep it separately and break it together (``pacing.py``); the robots cache fetches
    through the same pacer so that request counts against the same budget.
    """

    fetcher: Fetcher
    robots: RobotsCache
    pacer: Pacer


@dataclass(frozen=True)
class AcquireReport:
    """What one batch did. Every field is a count a human would ask for during an incident.

    The outcomes are disjoint: each claimed row lands in exactly one of ``acquired``,
    ``dropped``, ``failed``, ``stale``, ``fetch_deferred`` or ``dead_lettered``; each network
    route an acquired article took lands in exactly one of ``fetched``, ``robots_blocked``,
    ``fetch_refused``, ``extract_empty`` or ``adapter_missing``. Articles with nothing to fetch
    — a tier-1 body already present, no rights, a tier-0 feed — are in ``acquired`` alone, since
    that is their expected state and a count of it would only ever say how big the roster is.
    """

    claimed: int = 0
    #: Articles handed to the next stage, with a body or without one.
    acquired: int = 0
    #: Articles stopped by FR-I7. Steady state is small and non-zero; a spike means either a
    #: publisher changed language or the detector is being fed something it should not be.
    dropped: int = 0
    #: Articles that raised. The row keeps its claim and is retried after the lease.
    failed: int = 0
    #: Rows another worker had already completed or reclaimed — an expired lease, not a
    #: fault. Counted separately because a batch reporting these as failures sends someone
    #: hunting a bug in a record that was processed correctly.
    stale: int = 0
    #: Bodies obtained over the network and stored. Tier-1 bodies arrive with discovery and
    #: are not counted here.
    fetched: int = 0
    #: Pages ``robots.txt`` disallows for the publisher's User-Agent. Continue headline-only.
    robots_blocked: int = 0
    #: A 4xx from the page. Not a robots block and not a failure: a 403 at the edge is the
    #: publisher's decision about this request, and it is recorded as that.
    fetch_refused: int = 0
    #: Rows given back for a later batch — a 5xx, no answer, or a pacing wait past the lease.
    fetch_deferred: int = 0
    #: Transient failures that exhausted their retries. The row stays as the trace and the
    #: article is terminal.
    dead_lettered: int = 0
    #: A 200 that extraction found no body in.
    extract_empty: int = 0
    #: A tier-2 article with no adapter for its host, or a URL or response the adapter did
    #: not recognise. Climbing while ``fetched`` is flat means the publisher changed its site.
    adapter_missing: int = 0

    def __add__(self, other: "AcquireReport") -> "AcquireReport":
        return AcquireReport(
            **{f.name: getattr(self, f.name) + getattr(other, f.name) for f in fields(self)}
        )


@dataclass(frozen=True)
class _Obtained:
    """The ladder's answer for an article that continues: a body to write, or none."""

    text: str | None = None
    provenance: BodyProvenance | None = None
    counted: AcquireReport = AcquireReport()


@dataclass(frozen=True)
class _Deferred:
    """The ladder's answer for an article that must not continue in this batch."""

    error: str
    #: When the row is next due. None for a failed fetch — the caller sets it from the backoff
    #: schedule, or dead-letters — and a time for a pacing wait, which is not a failure and
    #: must never walk a row towards dead-letter however often it recurs.
    retry_at: dt.datetime | None = None


_NO_FETCH = _Obtained()


def _obtain_body(
    session: Session,
    article: CanonicalRecord,
    source: Source,
    feed: Feed | None,
    *,
    network: Network,
    lease: dt.timedelta,
) -> _Obtained | _Deferred:
    """The ladder. Every rung is a reason not to fetch; the network is reached only past all of
    them, and the transaction is closed first.

    The route is the feed's declared tier, never an inspection of the page: tier 2 is the one
    tier where the words live at a different URL from the article's, and the adapter is the
    per-publisher translation of one into the other. ``robots.txt`` is then read for the URL
    actually requested — for a tier-2 article that is the API's, not the page's.
    """
    if article.body_text is not None:
        return _NO_FETCH
    # A publisher stopped in the registry — disabled, or its permission to ingest withdrawn —
    # gets no request of any kind, not only no poll. The article it already gave us continues
    # at the level it was taken under (Legal A-L5); what stops is new collection.
    if not (source.enabled and source.permitted_to_ingest):
        log.info(
            "article %d: publisher %d is stopped; no request made",
            article.article_id,
            source.source_id,
        )
        return _NO_FETCH
    if not sources_repo.holds_body_rights(source):
        return _NO_FETCH
    if feed is None:
        log.info("article %d has no feed, so no acquisition route", article.article_id)
        return _NO_FETCH

    tier = feed.acquisition_tier
    if tier is AcquisitionTier.UNAVAILABLE:
        return _NO_FETCH
    if tier is AcquisitionTier.FULL_FEED:
        log.info("article %d: tier-1 feed %d shipped no body", article.article_id, feed.feed_id)
        return _NO_FETCH

    adapter: Adapter | None = None
    if tier is AcquisitionTier.PUBLISHER_API:
        adapter = adapter_for(article.url_canonical)
        request_url = adapter.request_url(article.url_canonical) if adapter is not None else None
        if request_url is None:
            log.warning(
                "article %d: no publisher API is known for %s",
                article.article_id,
                article.url_canonical,
            )
            return _Obtained(counted=AcquireReport(adapter_missing=1))
        provenance = BodyProvenance.TIER2_API
    else:
        request_url = article.url_canonical
        provenance = BodyProvenance.TIER3_EXTRACTED

    # ⚠️ Commit before going to the network — the robots lookup below may fetch. The reads
    # above opened a transaction, and one held open across a fetch pins the database's oldest
    # xmin, so VACUUM can reclaim nothing anywhere for as long as the slowest publisher takes
    # to answer. Nothing has been written yet, so this ends the transaction and moves nothing.
    session.commit()

    if not network.robots.allowed(request_url, source):
        log.info("article %d: robots.txt disallows %s", article.article_id, request_url)
        return _Obtained(counted=AcquireReport(robots_blocked=1))

    floor = network.robots.crawl_delay(request_url, source)
    waited = network.pacer.try_acquire(source, max_wait=lease.total_seconds(), floor=floor)
    if waited is None:
        wait = network.pacer.wait_for(source, floor=floor)
        return _Deferred(
            error=f"pacing: publisher {source.source_id}'s next slot is {wait:.0f}s away, "
            "past the lease",
            retry_at=dt.datetime.now(dt.UTC) + dt.timedelta(seconds=wait),
        )

    result = network.fetcher(
        request_url, user_agent=source.user_agent or DEFAULT_USER_AGENT, headers={}
    )
    if result.status is None or result.status >= 500:
        return _Deferred(error=f"fetch {request_url}: status={result.status} {result.error}")
    if result.status != 200 or result.body is None:
        log.warning(
            "article %d: %s answered %s %s; continuing without a body",
            article.article_id,
            request_url,
            result.status,
            result.error,
        )
        return _Obtained(counted=AcquireReport(fetch_refused=1))

    # ⚠️ Only a 200 body reaches extraction. An error page extracts to something — a 404 here
    # came out as 5,300 characters of cookie policy — and no length rule can tell it from an
    # article (``extract``'s docstring). The status code is the filter.
    html = result.body
    if adapter is not None:
        unwrapped = adapter.html_from(result.body)
        if unwrapped is None:
            log.warning(
                "article %d: %s answered 200 but not in the shape its API promised",
                article.article_id,
                request_url,
            )
            return _Obtained(counted=AcquireReport(adapter_missing=1))
        html = unwrapped

    text = extract(html, article.url_canonical)
    if text is None:
        log.info("article %d: nothing extractable at %s", article.article_id, request_url)
        return _Obtained(counted=AcquireReport(extract_empty=1))
    return _Obtained(text=text, provenance=provenance, counted=AcquireReport(fetched=1))


def handle(
    session: Session, work: PipelineWork, *, network: Network, lease: dt.timedelta
) -> AcquireReport:
    """Run the stage for one work row and move it on. Returns the row's outcome as a report.

    FR-I7 is decided first, before any request: a dropped article costs the publisher nothing.
    The decision is computed here and written only at the end, together with everything else,
    because the fetch in between can end in the row being released — and a release must leave
    the record exactly as it found it. ``strip_html`` is not a fixpoint, so a lede written
    before a release would be stripped a second time on the retry.

    Commits once for the article, at the end. That single commit is what makes the body, the
    normalization, the state move, the dequeue and the successor enqueue one atomic step —
    split them and an article can end up advanced with nothing owed, or owing a stage it has
    already had.
    """
    article = session.get(CanonicalRecord, work.article_id)
    if article is None:
        raise ValueError(f"work {work.work_id} names article {work.article_id}, which is gone")
    source = session.get(Source, article.source_id)
    if source is None:
        raise ValueError(
            f"article {article.article_id} names source {article.source_id}, which is gone"
        )
    feed = session.get(Feed, article.feed_id) if article.feed_id is not None else None

    lede = strip_html(article.lede)
    verdict = detect_language(language_input(article.title, lede))
    if verdict.drop:
        article.lede = lede
        article.language = verdict.language
        work_queue.terminate(session, work, TerminalReason.DROPPED_LANGUAGE)
        session.commit()
        log.info(
            "article %d dropped: language=%s (FR-I7)",
            article.article_id,
            article.language or "undetermined",
        )
        return AcquireReport(dropped=1)

    outcome = _obtain_body(session, article, source, feed, network=network, lease=lease)
    if isinstance(outcome, _Deferred):
        return _defer(session, work, outcome)

    article.lede = lede
    article.language = verdict.language
    if outcome.text is not None:
        article.body_text = outcome.text
        article.body_provenance = outcome.provenance
    if article.body_text is not None:
        article.content_hash = content_hash(article.body_text)
    work_queue.advance(session, work)
    session.commit()
    return AcquireReport(acquired=1) + outcome.counted


def _defer(session: Session, work: PipelineWork, deferred: _Deferred) -> AcquireReport:
    """Give the row back, or stop retrying it. Commits.

    A pacing wait carries its own retry time and is never a strike against the row. A failed
    fetch walks up the backoff schedule and, on the attempt ``MAX_ATTEMPTS`` names, is
    dead-lettered instead — the row kept as the trace, the article marked terminal.
    """
    work_id, article_id = work.work_id, work.article_id
    if deferred.retry_at is None and work.attempts >= work_queue.MAX_ATTEMPTS:
        work_queue.dead_letter(session, work, error=deferred.error)
        session.commit()
        log.warning(
            "article %s dead-lettered after %d attempts: %s",
            article_id,
            work.attempts,
            deferred.error,
        )
        return AcquireReport(dead_lettered=1)

    retry_at = deferred.retry_at or dt.datetime.now(dt.UTC) + work_queue.backoff_after(
        work.attempts
    )
    work_queue.release(session, work, retry_at=retry_at, error=deferred.error)
    session.commit()
    log.info("work %d released until %s: %s", work_id, retry_at.isoformat(), deferred.error)
    return AcquireReport(fetch_deferred=1)


def run_batch(
    session: Session,
    *,
    network: Network,
    lease: dt.timedelta,
    limit: int = 50,
    worker: str | None = None,
) -> AcquireReport:
    """Claim up to ``limit`` due articles and run the stage on each.

    ⚠️ Each article is handled inside its own try/except, for the reason discovery polls each
    feed inside one: a single record that provokes a bug must not stop the rest of the batch.
    Unlike discovery, the failure leaves a trace without any help — ``claim`` has already
    committed the row with ``attempts`` incremented — but the trace says only *how often*, so
    the message is written too.

    After each article the rows still held are heartbeated. The claim promised to finish
    within the lease; with a paced fetch per article a batch cannot keep that promise, so it
    is renewed per row and the lease bounds one article's work. The heartbeat also says which
    rows are still ours — one another worker reclaimed is dropped here rather than fetched and
    then refused by ``advance()``.
    """
    worker = worker or worker_name()
    claimed = work_queue.claim(session, stage=STAGE, worker=worker, lease=lease, limit=limit)
    report = AcquireReport(claimed=len(claimed))
    pending = _by_publisher(session, claimed)
    while pending:
        work = pending.pop(0)
        # ⚠️ Read the identifiers before entering the try, and use the copies afterwards.
        # ``session.rollback()`` expires the instance, so touching an attribute re-queries the
        # row — and if the row is gone, that raises ``ObjectDeletedError`` *from inside the
        # handler that exists to contain failures*, taking the rest of the batch with it. The
        # row being gone is not exotic: it is what a worker whose lease expired finds.
        work_id, article_id = work.work_id, work.article_id
        try:
            report += handle(session, work, network=network, lease=lease)
        except work_queue.StaleWork:
            # Another worker finished this row while our claim was expired. Nothing is wrong
            # with the article, so this must not read as a failure — counting it as one sends
            # someone looking for a bug in a record that was processed correctly.
            session.rollback()
            log.info("work %d was already completed by another worker", work_id)
            report += AcquireReport(stale=1)
        except Exception as exc:
            session.rollback()
            log.exception("article %s raised during acquire", article_id)
            _record_failure(session, work_id, exc)
            report += AcquireReport(failed=1)

        if pending:
            kept = work_queue.heartbeat(session, [row.work_id for row in pending], worker=worker)
            stolen = len(pending) - len(kept)
            if stolen:
                log.info(
                    "%d row(s) were reclaimed by another worker mid-batch; not fetched", stolen
                )
                report += AcquireReport(stale=stolen)
            pending = [row for row in pending if row.work_id in kept]
    return report


def _by_publisher(session: Session, claimed: Sequence[PipelineWork]) -> list[PipelineWork]:
    """The batch in round-robin order over publishers, so one publisher's pacing gap is spent
    on the others' requests rather than slept through (``pacing.round_robin``)."""
    ids = [row.article_id for row in claimed if row.article_id is not None]
    publisher_of: dict[int | None, int | None] = {
        article_id: source_id
        for article_id, source_id in session.execute(
            sa.select(CanonicalRecord.article_id, CanonicalRecord.source_id).where(
                CanonicalRecord.article_id.in_(ids)
            )
        )
    }
    session.commit()
    return round_robin(claimed, key=lambda row: publisher_of.get(row.article_id))


def _record_failure(session: Session, work_id: int, exc: Exception) -> None:
    """Write why a row failed, in a transaction of its own.

    ⚠️ The rollback above discards everything the handler did, and a message written inside
    that transaction would go with it — leaving the one failure class this guard exists to
    survive as the one that says nothing about itself. ``attempts`` alone reports that a row
    keeps failing and never what it fails on.

    Takes an id rather than the instance, because the instance is expired by the rollback and
    the row may be gone.

    Its own try/except, because a failure to record a failure is worth a log line rather than
    the rest of the batch.
    """
    try:
        row = session.get(PipelineWork, work_id)
        if row is not None:
            row.last_error = f"{type(exc).__name__}: {exc}"[:2000]
            session.commit()
    except Exception:
        session.rollback()
        log.exception("could not record the failure of work %d", work_id)
