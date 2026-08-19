"""Reading and writing the source registry (RFC §5.1, §9).

The registry is not settings — it governs what the pipeline may do with each publisher, and
FR-I6 requires a change to take effect without a deploy. Everything that reads or writes a
``Source`` goes through here rather than issuing its own query, so that a rule gaining a
second condition gains it everywhere at once.

None of these commit. The caller owns the transaction — unlike ``work_queue.claim()``, which
commits by design because its lock must not outlive the claim.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from meridian_contract import AcquisitionTier, DiscoveryMethod, RightsLevel
from sqlalchemy.orm import Session

from meridian.db.models import CanonicalRecord, Source


def list_all(session: Session) -> Sequence[Source]:
    """Every source, enabled or not — the admin list."""
    return session.scalars(sa.select(Source).order_by(Source.name)).all()


def get(session: Session, source_id: int) -> Source | None:
    return session.get(Source, source_id)


def enabled(session: Session) -> Sequence[Source]:
    """The sources discovery may poll.

    Read per run, never cached: FR-I6 exists so a Legal or ToS problem can stop ingestion in
    minutes, and a cache adds its own lifetime to that number.
    """
    return session.scalars(
        sa.select(Source).where(Source.enabled.is_(True)).order_by(Source.source_id)
    ).all()


def create(
    session: Session,
    *,
    name: str,
    home_url: str,
    discovery_method: DiscoveryMethod,
    acquisition_tier: AcquisitionTier,
    rights_level: RightsLevel,
    jurisdiction: str,
    rate_limit_per_min: int,
    enabled: bool = True,
) -> Source:
    source = Source(
        name=name,
        home_url=home_url,
        discovery_method=discovery_method,
        acquisition_tier=acquisition_tier,
        rights_level=rights_level,
        jurisdiction=jurisdiction,
        rate_limit_per_min=rate_limit_per_min,
        enabled=enabled,
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
    discovery_method: DiscoveryMethod,
    jurisdiction: str,
    rate_limit_per_min: int,
) -> Source | None:
    """Write the descriptive fields, as the edit form submits them.

    ``enabled``, ``acquisition_tier`` and ``rights_level`` are deliberately absent. They are
    what an operator changes under pressure, and each has its own single-field setter. A
    full-row write that carried them would let a form rendered before an emergency change and
    submitted after it revert that change — reversing a stop-ingestion instruction with a 303
    and no error, from one operator with two tabs open.
    """
    source = session.get(Source, source_id)
    if source is None:
        return None
    source.name = name
    source.home_url = home_url
    source.discovery_method = discovery_method
    source.jurisdiction = jurisdiction
    source.rate_limit_per_min = rate_limit_per_min
    session.flush()
    return source


def set_enabled(session: Session, source_id: int, *, value: bool) -> Source | None:
    """Stop or resume ingestion from one source.

    Separate from ``replace`` on purpose. This is the path a takedown or a ToS complaint
    takes, and it must not be able to fail because some unrelated field on the row is
    incomplete — an emergency stop that rejects the form is an emergency stop that did not
    happen.
    """
    source = session.get(Source, source_id)
    if source is None:
        return None
    source.enabled = value
    session.flush()
    return source


def set_rights_level(session: Session, source_id: int, *, level: RightsLevel) -> Source | None:
    """Grant or revoke body-text rights (FR-S5), for the same reason as ``set_enabled``.

    Read at the point of use rather than copied onto articles, so this applies to records
    already ingested with nothing to cascade (RFC §5.2, rev 20).
    """
    source = session.get(Source, source_id)
    if source is None:
        return None
    source.rights_level = level
    session.flush()
    return source


def set_acquisition_tier(
    session: Session, source_id: int, *, tier: AcquisitionTier
) -> Source | None:
    """Change how a source's bodies are obtained, for the same reason as ``set_enabled``."""
    source = session.get(Source, source_id)
    if source is None:
        return None
    source.acquisition_tier = tier
    session.flush()
    return source


def article_ids_with_body_rights() -> sa.Select[tuple[int]]:
    """The FR-S5 set: articles whose source currently grants body-text rights.

    Compose with ``in_``::

        select(CanonicalRecord).where(
            CanonicalRecord.article_id.in_(article_ids_with_body_rights())
        )

    Rights live in one place — the registry — and are read here at the point of use. There is
    deliberately no copy on the article: rights are a relationship and relationships change,
    so a stored answer would report what was true at acquisition and keep reporting it after a
    downgrade (RFC §5.2, rev 20). Reading through the source means a downgrade takes effect for
    records already ingested, with nothing to cascade.

    Returning a self-contained ``Select`` rather than a bare predicate is the load-bearing
    part, and it is what mypy pins. A predicate that references the outer row must be
    correlated to it, and SQLAlchemy infers that correlation from the enclosing statement: when
    the caller selects from an alias, a subquery or a CTE instead of the bare table, the
    correlation is silently dropped and the predicate reads true for every row. Negated it then
    excludes nothing — the direction that matters, because a caller asking this is asking what
    it may not summarize. Resolving the question inside a subquery of our own puts it out of
    the caller's reach; how that subquery is written is not what makes it safe.

    ``NOT IN`` is safe against this because ``article_id`` is a primary key: a single NULL in
    the subquery would make ``NOT IN`` return nothing at all.
    """
    return (
        sa.select(CanonicalRecord.article_id)
        .join(Source, Source.source_id == CanonicalRecord.source_id)
        .where(Source.rights_level == RightsLevel.BODY_TEXT)
    )
