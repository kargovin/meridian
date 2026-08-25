"""Reading and writing the source registry (RFC §5.1, §9).

The registry is not settings — it governs what the pipeline may do with each publisher, and
FR-I6 requires a change to take effect without a deploy. Everything that reads or writes a
``Source`` goes through here rather than issuing its own query, so that a rule gaining a
second condition gains it everywhere at once.

None of these commit. The caller owns the transaction — unlike ``work_queue.claim()``, which
commits by design because its lock must not outlive the claim.
"""

import datetime as dt
from collections.abc import Sequence

import sqlalchemy as sa
from meridian_contract import RightsLevel
from sqlalchemy.orm import Session

from meridian.db.models import CanonicalRecord, Source


class StaleWrite(Exception):
    """The row changed after the page that submitted this write was rendered.

    Every governing-field write is compare-and-set. Each control on the admin surface renders
    the row's current value, so submitting one written before an out-of-band change would write
    that stale value back — reverting a rights revocation or a stop-ingestion instruction with
    no error, from one operator with two tabs open. ``updated_at`` is the version token: it is
    trigger-maintained, so a change made through psql moves it too.
    """


def list_all(session: Session) -> Sequence[Source]:
    """Every source, enabled or not — the admin list."""
    return session.scalars(sa.select(Source).order_by(Source.name)).all()


def get(session: Session, source_id: int) -> Source | None:
    return session.get(Source, source_id)


def enabled(session: Session) -> Sequence[Source]:
    """The publishers discovery may poll — both gates, never one.

    ``permitted_to_ingest`` is the Legal determination and ``enabled`` the operational switch,
    and a caller that checked only the second would poll a publisher whose terms forbid it. The
    two are separate columns precisely because they are set by different people for different
    reasons, which is exactly the situation in which one of them gets forgotten at the call
    site — so they are joined here rather than left to each caller.

    Read per run, never cached: FR-I6 exists so a Legal or ToS problem can stop ingestion in
    minutes, and a cache adds its own lifetime to that number.
    """
    return session.scalars(
        sa.select(Source)
        .where(Source.enabled.is_(True), Source.permitted_to_ingest.is_(True))
        .order_by(Source.source_id)
    ).all()


def create(
    session: Session,
    *,
    name: str,
    home_url: str,
    rights_level: RightsLevel,
    jurisdiction: str,
    rate_limit_per_min: int,
    user_agent: str | None = None,
    enabled: bool = True,
    permitted_to_ingest: bool = True,
) -> Source:
    """Create a publisher. Its feeds are created separately — see ``meridian.db.feeds``."""
    source = Source(
        name=name,
        home_url=home_url,
        rights_level=rights_level,
        jurisdiction=jurisdiction,
        rate_limit_per_min=rate_limit_per_min,
        user_agent=user_agent,
        enabled=enabled,
        permitted_to_ingest=permitted_to_ingest,
    )
    session.add(source)
    session.flush()
    return source


def describe(
    session: Session,
    source_id: int,
    *,
    name: str,
    home_url: str,
    jurisdiction: str,
    rate_limit_per_min: int,
    user_agent: str | None,
) -> Source | None:
    """Write the descriptive fields, as the edit form submits them.

    ``enabled``, ``permitted_to_ingest`` and ``rights_level`` are deliberately absent. They are
    what an operator changes under pressure, and each has its own single-field setter. A
    full-row write that carried them would let a form rendered before an emergency change and
    submitted after it revert that change — reversing a stop-ingestion instruction with a 303
    and no error, from one operator with two tabs open.

    ``user_agent`` is here rather than on its own route because writing a stale one costs a
    retry, not a rights bypass: the harm the single-field routes exist to prevent is doing
    something we are not permitted to do, and no value of this field grants a permission.
    """
    source = session.get(Source, source_id)
    if source is None:
        return None
    source.name = name
    source.home_url = home_url
    source.jurisdiction = jurisdiction
    source.rate_limit_per_min = rate_limit_per_min
    source.user_agent = user_agent
    session.flush()
    return source


def _claim(session: Session, source_id: int, expected_updated_at: dt.datetime) -> Source | None:
    source = session.get(Source, source_id)
    if source is None:
        return None
    # SELECT ... FOR UPDATE, so the compare and the write are one step. Without the lock two
    # requests can both pass the check — the second blocks on the row only at flush time and
    # then applies on top, which is the lost update this whole mechanism exists to prevent.
    # The window is milliseconds rather than the minutes a stale page gives you, and two of
    # the three fields escape it by luck: rights_level is two-valued and both toggles post the
    # opposite of what they rendered, so the harmful direction means writing back the value
    # just read, which SQLAlchemy elides. That is luck, not safety, and it ends the day any of
    # them gains a member — acquisition_tier had three values and did not escape before it
    # moved to Feed, where the same lock protects it.
    session.refresh(source, with_for_update=True)
    if source.updated_at != expected_updated_at:
        raise StaleWrite(
            f"source {source_id} changed at {source.updated_at.isoformat()}, "
            f"after the page offering this write was rendered"
        )
    return source


def set_enabled(
    session: Session, source_id: int, *, value: bool, expected_updated_at: dt.datetime
) -> Source | None:
    """Stop or resume ingestion from one source (FR-I6).

    Separate from ``describe`` on purpose. This is the path a takedown or a ToS complaint takes,
    and it must not be able to fail because some unrelated field on the row is incomplete — an
    emergency stop that rejects the form is an emergency stop that did not happen.
    """
    source = _claim(session, source_id, expected_updated_at)
    if source is None:
        return None
    source.enabled = value
    session.flush()
    return source


def set_rights_level(
    session: Session, source_id: int, *, level: RightsLevel, expected_updated_at: dt.datetime
) -> Source | None:
    """Grant or revoke body-text rights (FR-S5), for the same reason as ``set_enabled``.

    Read at the point of use rather than copied onto articles, so this applies to records
    already ingested with nothing to cascade (RFC §5.2, rev 20).
    """
    source = _claim(session, source_id, expected_updated_at)
    if source is None:
        return None
    source.rights_level = level
    session.flush()
    return source


def set_permitted_to_ingest(
    session: Session, source_id: int, *, value: bool, expected_updated_at: dt.datetime
) -> Source | None:
    """Record whether we may ingest this publisher at all, for the same reason as ``set_enabled``.

    Separate from both ``enabled`` and ``rights_level`` on purpose. It is not a rights level: a
    terms-of-service exclusion is not a lower rung on the body_text/headline_only ladder, and
    folding one into ``headline_only`` leaves the pipeline politely still polling a publisher
    whose terms forbid the activity. It is not ``enabled`` either, because that is the
    operational switch and gets flipped back when the operational reason passes.
    """
    source = _claim(session, source_id, expected_updated_at)
    if source is None:
        return None
    source.permitted_to_ingest = value
    session.flush()
    return source


def _article_ids_by_rights(level: RightsLevel) -> sa.Select[tuple[int]]:
    return (
        sa.select(CanonicalRecord.article_id)
        .join(Source, Source.source_id == CanonicalRecord.source_id)
        .where(Source.rights_level == level)
    )


def article_ids_with_body_rights() -> sa.Select[tuple[int]]:
    """The FR-S5 set: articles whose source currently grants body-text rights.

    Compose with ``in_``::

        select(CanonicalRecord).where(
            CanonicalRecord.article_id.in_(article_ids_with_body_rights())
        )

    Rights live in one place — the registry — and are read here at the point of use. There is
    deliberately no copy on the article: rights are a relationship and relationships change, so
    a stored answer would report what was true at acquisition and keep reporting it after a
    downgrade (RFC §5.2, rev 20). Reading through the source means a downgrade takes effect for
    records already ingested, with nothing to cascade.

    Returning a self-contained ``Select`` rather than a bare predicate is the load-bearing
    part. A predicate that references the outer row must be correlated to it, and SQLAlchemy
    infers that correlation from the enclosing statement: when the caller selects from an
    alias, a subquery or a CTE instead of the bare table, the correlation is silently dropped
    and the predicate reads true for every row. Resolving the question inside a subquery of our
    own puts it out of the caller's reach.

    For the excluded set use ``article_ids_without_body_rights()``, never ``~...in_()`` — see
    there.
    """
    return _article_ids_by_rights(RightsLevel.BODY_TEXT)


def article_ids_without_body_rights() -> sa.Select[tuple[int]]:
    """The complement: articles their source does not grant body-text rights for.

    Published positively rather than left to ``NOT IN`` on the set above, for two reasons.

    ``NOT IN (subquery)`` is the one form PostgreSQL cannot pull up into an anti-join. It
    becomes a ``SubPlan``, hashed only while the id set fits ``work_mem`` and rescanned per row
    once it does not — a step, not a gradient. Measured on this schema at the default 4 MB:
    0.2 s at 300k articles, over ten seconds at 500k. ``IN`` over this complement returns the
    same rows in well under a second at 500k, because it is a hash semi-join.

    And the exclusion is the direction that matters, because a caller asking is asking what it
    may **not** summarize. ``NOT IN`` also returns nothing at all if the subquery yields a
    single NULL — which cannot happen while these joins are inner, but would the moment one
    became an outer join, and the rights filter would then fail open rather than closed.

    Exact rather than merely complementary: ``canonical_record.source_id`` is NOT NULL, the
    join is inner, and ``rights_level`` is two-valued under a CHECK, so every article falls in
    exactly one of the two sets.
    """
    return _article_ids_by_rights(RightsLevel.HEADLINE_ONLY)
