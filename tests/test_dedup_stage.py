"""The dedup stage (FR-I5): which arrivals collapse, into what, and which continue."""

import datetime as dt
import logging
import random
from types import EllipsisType
from typing import Any

import pytest
import sqlalchemy as sa
from meridian_contract import PipelineState, Stage, TerminalReason
from sqlalchemy.orm import Session

from meridian.db import work_queue
from meridian.db.models import AlternateCopy, CanonicalRecord, PipelineWork, Source
from meridian.db.simhash import simhash_from_db, simhash_to_db
from meridian.dedup.fingerprint import distance, fingerprint
from meridian.dedup.stage import DedupReport, handle, run_batch
from meridian.ingest.normalize import content_hash
from tests.factories import make_article, make_source, make_work

pytestmark = pytest.mark.postgres

H = 3
LEASE = dt.timedelta(minutes=5)
_VOCABULARY = [f"word{i}" for i in range(3000)]


def body(seed: int, words: int = 400) -> str:
    """A long body unrelated to every other seed's — about 32 bits from any of them."""
    rng = random.Random(seed)
    return " ".join(rng.choice(_VOCABULARY) for _ in range(words))


#: The same story re-hosted with a credit line, which changes a few shingles at one end.
STORY = body(1)
REHOSTED = STORY + " This article first appeared on Global Voices and is republished here."


def flip(value: int, bits: int) -> int:
    """``value`` with its ``bits`` highest bits inverted — the sign bit first, so every
    boundary test also crosses the signed storage of the column."""
    return value ^ sum(1 << (63 - i) for i in range(bits))


def held(
    session: Session,
    source: Source,
    guid: str,
    text: str | None,
    *,
    state: PipelineState = PipelineState.DEDUPED,
    simhash: int | EllipsisType | None = ...,
    **kw: Any,
) -> CanonicalRecord:
    """A record with a body. By default already past dedup, fingerprinted as the stage would."""
    if isinstance(simhash, EllipsisType):
        fp = fingerprint(text) if text is not None else None
        simhash = simhash_to_db(fp) if fp is not None else None
    return make_article(
        session,
        source,
        guid=guid,
        state=state,
        body_text=text,
        content_hash=content_hash(text) if text is not None else None,
        simhash=simhash if state is not PipelineState.ACQUIRED else None,
        **kw,
    )


def arrival(session: Session, source: Source, guid: str, text: str | None) -> PipelineWork:
    article = held(session, source, guid, text, state=PipelineState.ACQUIRED)
    return make_work(session, stage=Stage.DEDUP, article=article)


def surviving(session: Session) -> set[str]:
    return set(session.scalars(sa.select(CanonicalRecord.guid)))


def test_a_headline_only_article_passes_straight_through(app_session: Session) -> None:
    source = make_source(app_session)
    work = arrival(app_session, source, "headline", None)
    app_session.commit()

    report = handle(app_session, work, hamming_bits=H)

    article = app_session.scalars(sa.select(CanonicalRecord)).one()
    assert report == DedupReport(advanced=1, no_body=1)
    assert article.pipeline_state is PipelineState.DEDUPED
    assert article.simhash is None
    assert app_session.scalars(sa.select(PipelineWork.stage)).all() == [Stage.CLASSIFY]


def test_a_new_story_is_fingerprinted_and_continues(app_session: Session) -> None:
    source = make_source(app_session)
    held(app_session, make_source(app_session, "Other"), "unrelated", body(2))
    work = arrival(app_session, source, "new", STORY)
    app_session.commit()

    report = handle(app_session, work, hamming_bits=H)

    article = app_session.scalars(
        sa.select(CanonicalRecord).where(CanonicalRecord.guid == "new")
    ).one()
    assert report == DedupReport(advanced=1)
    assert article.pipeline_state is PipelineState.DEDUPED
    assert article.simhash is not None
    assert simhash_from_db(article.simhash) == fingerprint(STORY)
    assert app_session.scalars(sa.select(PipelineWork.stage)).all() == [Stage.CLASSIFY]


def test_an_identical_body_from_another_publisher_collapses(app_session: Session) -> None:
    origin = make_source(app_session, "Global Voices")
    other = make_source(app_session, "openDemocracy")
    representative = held(app_session, origin, "rep", STORY)
    work = arrival(app_session, other, "copy", STORY)
    app_session.commit()

    report = handle(app_session, work, hamming_bits=H)

    assert report == DedupReport(collapsed=1, exact=1)
    assert surviving(app_session) == {"rep"}
    copy = app_session.scalars(sa.select(AlternateCopy)).one()
    assert (copy.article_id, copy.source_id) == (representative.article_id, other.source_id)
    assert app_session.scalars(sa.select(PipelineWork)).all() == []


def test_a_rehosting_with_a_credit_line_collapses_as_a_near_match(
    app_session: Session,
) -> None:
    """The case SimHash exists for: the SHA-256s share nothing, the fingerprints a few bits."""
    assert content_hash(STORY) != content_hash(REHOSTED)
    story, rehosted = fingerprint(STORY), fingerprint(REHOSTED)
    assert story is not None and rehosted is not None
    assert 0 < distance(story, rehosted) <= H
    origin = make_source(app_session, "Global Voices")
    other = make_source(app_session, "openDemocracy")
    held(app_session, origin, "rep", STORY)
    work = arrival(app_session, other, "copy", REHOSTED)
    app_session.commit()

    report = handle(app_session, work, hamming_bits=H)

    assert report == DedupReport(collapsed=1, near=1)
    assert surviving(app_session) == {"rep"}


@pytest.mark.parametrize(("bits", "collapses"), [(H, True), (H + 1, False)])
def test_the_distance_is_inclusive_and_crosses_the_sign_bit(
    app_session: Session, bits: int, collapses: bool
) -> None:
    """The stored fingerprint is the arrival's with the top bits flipped — the sign bit among
    them, so a comparison that mishandled the signed column would be off here."""
    fp = fingerprint(REHOSTED)
    assert fp is not None
    origin = make_source(app_session, "Global Voices")
    other = make_source(app_session, "openDemocracy")
    held(app_session, origin, "rep", STORY, simhash=simhash_to_db(flip(fp, bits)))
    work = arrival(app_session, other, "copy", REHOSTED)
    app_session.commit()

    report = handle(app_session, work, hamming_bits=H)

    assert report.collapsed == int(collapses)
    assert surviving(app_session) == ({"rep"} if collapses else {"rep", "copy"})


def test_the_distance_is_the_argument_not_a_constant(app_session: Session) -> None:
    fp = fingerprint(REHOSTED)
    assert fp is not None
    origin = make_source(app_session, "Global Voices")
    other = make_source(app_session, "openDemocracy")
    held(app_session, origin, "rep", STORY, simhash=simhash_to_db(flip(fp, 6)))
    work = arrival(app_session, other, "copy", REHOSTED)
    app_session.commit()

    assert handle(app_session, work, hamming_bits=6).collapsed == 1


def test_a_match_within_one_publisher_is_logged_and_not_collapsed(
    app_session: Session, caplog: pytest.LogCaptureFixture
) -> None:
    """Two bodies from one publisher that match are boilerplate, not a story run twice."""
    source = make_source(app_session, "ESO")
    representative = held(app_session, source, "rep", STORY)
    work = arrival(app_session, source, "twin", STORY)
    app_session.commit()
    twin_id = work.article_id

    with caplog.at_level(logging.INFO, logger="meridian.dedup.stage"):
        report = handle(app_session, work, hamming_bits=H)

    assert report == DedupReport(advanced=1, same_publisher=1)
    assert surviving(app_session) == {"rep", "twin"}
    assert app_session.scalars(sa.select(AlternateCopy)).all() == []
    assert (
        f"article {twin_id} matches {representative.article_id} from the same publisher"
        in caplog.text
    )


def test_a_same_publisher_twin_does_not_hide_a_copy_from_another(app_session: Session) -> None:
    """The same-publisher match is exact and the cross-publisher one only near; nearest-first
    over both would pick the twin and never collapse."""
    own = make_source(app_session, "openDemocracy")
    origin = make_source(app_session, "Global Voices")
    held(app_session, own, "twin", REHOSTED)
    representative = held(app_session, origin, "rep", STORY)
    work = arrival(app_session, own, "copy", REHOSTED)
    app_session.commit()

    report = handle(app_session, work, hamming_bits=H)

    assert report == DedupReport(collapsed=1, near=1)
    assert app_session.scalars(sa.select(AlternateCopy.article_id)).one() == (
        representative.article_id
    )


def test_a_body_too_short_to_fingerprint_still_matches_exactly(app_session: Session) -> None:
    origin = make_source(app_session, "Global Voices")
    other = make_source(app_session, "openDemocracy")
    held(app_session, origin, "rep", "Ceasefire")
    work = arrival(app_session, other, "copy", "Ceasefire")
    app_session.commit()

    assert handle(app_session, work, hamming_bits=H) == DedupReport(collapsed=1, exact=1)


def test_a_body_too_short_to_fingerprint_continues_without_one(app_session: Session) -> None:
    source = make_source(app_session)
    work = arrival(app_session, source, "short", "Ceasefire")
    app_session.commit()

    assert handle(app_session, work, hamming_bits=H) == DedupReport(advanced=1)
    assert app_session.scalars(sa.select(CanonicalRecord.simhash)).one() is None


def test_a_record_not_yet_past_dedup_is_not_a_candidate(app_session: Session) -> None:
    """Candidates key on pipeline_state: an identical body still waiting for this stage is
    another arrival, not a representative."""
    origin = make_source(app_session, "Global Voices")
    other = make_source(app_session, "openDemocracy")
    held(app_session, origin, "waiting", STORY, state=PipelineState.ACQUIRED)
    work = arrival(app_session, other, "copy", STORY)
    app_session.commit()

    assert handle(app_session, work, hamming_bits=H) == DedupReport(advanced=1)


@pytest.mark.parametrize("state", [PipelineState.CLASSIFIED, PipelineState.CLUSTERED])
def test_a_record_further_down_the_chain_is_a_candidate(
    app_session: Session, state: PipelineState
) -> None:
    origin = make_source(app_session, "Global Voices")
    other = make_source(app_session, "openDemocracy")
    held(app_session, origin, "rep", STORY, state=state)
    work = arrival(app_session, other, "copy", STORY)
    app_session.commit()

    assert handle(app_session, work, hamming_bits=H).collapsed == 1


def test_a_terminated_record_is_not_a_candidate(app_session: Session) -> None:
    """Collapsing into a record that stopped for good would lose the story entirely."""
    origin = make_source(app_session, "Global Voices")
    other = make_source(app_session, "openDemocracy")
    held(app_session, origin, "rep", STORY, terminal_reason=TerminalReason.FAILED)
    work = arrival(app_session, other, "copy", STORY)
    app_session.commit()

    assert handle(app_session, work, hamming_bits=H) == DedupReport(advanced=1)


def test_the_nearest_representative_wins(app_session: Session) -> None:
    fp = fingerprint(REHOSTED)
    assert fp is not None
    publishers = [make_source(app_session, name) for name in ("A", "B", "C")]
    held(app_session, publishers[0], "far", body(3), simhash=simhash_to_db(flip(fp, 3)))
    near = held(app_session, publishers[1], "near", body(4), simhash=simhash_to_db(flip(fp, 1)))
    work = arrival(app_session, publishers[2], "copy", REHOSTED)
    app_session.commit()

    handle(app_session, work, hamming_bits=H)

    assert app_session.scalars(sa.select(AlternateCopy.article_id)).one() == near.article_id


def test_a_tie_goes_to_the_oldest_record(app_session: Session) -> None:
    """Inserted newest-id-last but compared under a plan that reads the table backwards, so a
    missing tie-break cannot pass by the heap's order."""
    fp = fingerprint(REHOSTED)
    assert fp is not None
    publishers = [make_source(app_session, name) for name in ("A", "B", "C")]
    oldest = held(app_session, publishers[0], "old", body(3), simhash=simhash_to_db(flip(fp, 2)))
    held(app_session, publishers[1], "new", body(4), simhash=simhash_to_db(flip(fp, 2)))
    work = arrival(app_session, publishers[2], "copy", REHOSTED)
    app_session.commit()
    app_session.execute(
        sa.update(CanonicalRecord)
        .where(CanonicalRecord.guid == "old")
        .values(title="touched, so the row moves to the end of the heap")
    )
    app_session.commit()

    handle(app_session, work, hamming_bits=H)

    assert app_session.scalars(sa.select(AlternateCopy.article_id)).one() == oldest.article_id


def test_an_exact_tie_goes_to_the_oldest_record(app_session: Session) -> None:
    publishers = [make_source(app_session, name) for name in ("A", "B", "C")]
    oldest = held(app_session, publishers[0], "old", STORY)
    held(app_session, publishers[1], "new", STORY)
    work = arrival(app_session, publishers[2], "copy", STORY)
    app_session.commit()

    handle(app_session, work, hamming_bits=H)

    assert app_session.scalars(sa.select(AlternateCopy.article_id)).one() == oldest.article_id


def test_two_copies_in_one_batch_find_each_other(app_session: Session) -> None:
    """Neither has passed dedup when the batch is claimed; the first is committed as passed
    before the second is compared, so the second collapses into it."""
    origin = make_source(app_session, "Global Voices")
    other = make_source(app_session, "openDemocracy")
    arrival(app_session, origin, "first", STORY)
    arrival(app_session, other, "second", REHOSTED)
    app_session.commit()

    report = run_batch(app_session, lease=LEASE, hamming_bits=H)

    assert report == DedupReport(claimed=2, advanced=1, collapsed=1, near=1)
    assert len(surviving(app_session)) == 1


def test_a_body_without_a_hash_fails_the_row_and_says_why(app_session: Session) -> None:
    """RFC §5.1's invariant broken upstream. Passing it as unique would hide the breach and
    skip exact matching for it."""
    source = make_source(app_session)
    article = make_article(
        app_session, source, guid="broken", state=PipelineState.ACQUIRED, body_text=STORY
    )
    make_work(app_session, stage=Stage.DEDUP, article=article)
    healthy = arrival(app_session, source, "healthy", None)
    app_session.commit()
    healthy_id = healthy.article_id

    report = run_batch(app_session, lease=LEASE, hamming_bits=H)

    assert report == DedupReport(claimed=2, advanced=1, failed=1, no_body=1)
    failed = app_session.scalars(
        sa.select(PipelineWork).where(PipelineWork.article_id == article.article_id)
    ).one()
    assert failed.last_error is not None
    assert "holds a body but no content_hash" in failed.last_error
    healthy_article = app_session.get(CanonicalRecord, healthy_id)
    assert healthy_article is not None
    assert healthy_article.pipeline_state is PipelineState.DEDUPED


def _broken(session: Session, *, attempts: int) -> int:
    """A row that raises on every attempt: a body with no hash. Returns its article id."""
    source = make_source(session)
    article = make_article(
        session, source, guid="broken", state=PipelineState.ACQUIRED, body_text=STORY
    )
    make_work(session, stage=Stage.DEDUP, article=article, attempts=attempts)
    session.commit()
    return article.article_id


def _row(session: Session, article_id: int) -> PipelineWork:
    session.expire_all()
    return session.scalars(
        sa.select(PipelineWork).where(PipelineWork.article_id == article_id)
    ).one()


def test_a_failed_row_is_given_back_with_backoff(app_session: Session) -> None:
    """⚠️ Not left claimed: a claimed row is taken again one lease later, with no backoff and
    no count of how often that has happened."""
    article_id = _broken(app_session, attempts=0)
    before = dt.datetime.now(dt.UTC)

    report = run_batch(app_session, lease=LEASE, hamming_bits=H)

    assert report == DedupReport(claimed=1, failed=1)
    row = _row(app_session, article_id)
    assert row.claimed_by is None
    assert row.dead_lettered_at is None
    assert row.next_attempt_at >= before + work_queue.backoff_after(1)
    assert row.last_error is not None and "no content_hash" in row.last_error


def test_a_row_that_fails_every_attempt_is_dead_lettered(app_session: Session) -> None:
    """``claim()`` never stops on its own. On the attempt MAX_ATTEMPTS names the row is given up,
    and the article is marked failed so the reconciler does not re-enqueue it."""
    article_id = _broken(app_session, attempts=work_queue.MAX_ATTEMPTS - 1)

    report = run_batch(app_session, lease=LEASE, hamming_bits=H)

    assert report == DedupReport(claimed=1, failed=1, dead_lettered=1)
    row = _row(app_session, article_id)
    assert row.dead_lettered_at is not None
    assert row.last_error is not None and "no content_hash" in row.last_error
    article = app_session.get(CanonicalRecord, article_id)
    assert article is not None and article.terminal_reason is TerminalReason.FAILED


def test_one_attempt_short_of_the_limit_is_not_dead_lettered(app_session: Session) -> None:
    article_id = _broken(app_session, attempts=work_queue.MAX_ATTEMPTS - 2)

    report = run_batch(app_session, lease=LEASE, hamming_bits=H)

    assert report == DedupReport(claimed=1, failed=1)
    assert _row(app_session, article_id).dead_lettered_at is None


def test_a_copy_the_discovery_race_let_back_in_is_dropped(app_session: Session) -> None:
    """The race discovery's probe cannot close: the copy is noted and also back in the table.
    The batch drops it, and it does not fail."""
    origin = make_source(app_session, "Global Voices")
    other = make_source(app_session, "openDemocracy")
    representative = held(app_session, origin, "rep", STORY)
    app_session.add(
        AlternateCopy(
            article_id=representative.article_id,
            source_id=other.source_id,
            guid="back",
            url="https://example.test/back",
        )
    )
    arrival(app_session, other, "back", STORY)
    app_session.commit()

    report = run_batch(app_session, lease=LEASE, hamming_bits=H)

    assert report == DedupReport(claimed=1, collapsed=1, exact=1, already_noted=1)
    assert surviving(app_session) == {"rep"}
    assert len(app_session.scalars(sa.select(AlternateCopy)).all()) == 1
