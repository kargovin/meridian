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


def replace(
    session: Session,
    source_id: int,
    *,
    name: str,
    home_url: str,
    discovery_method: DiscoveryMethod,
    acquisition_tier: AcquisitionTier,
    rights_level: RightsLevel,
    jurisdiction: str,
    rate_limit_per_min: int,
    enabled: bool,
) -> Source | None:
    """Write every field, as the edit form submits them."""
    source = session.get(Source, source_id)
    if source is None:
        return None
    source.name = name
    source.home_url = home_url
    source.discovery_method = discovery_method
    source.acquisition_tier = acquisition_tier
    source.rights_level = rights_level
    source.jurisdiction = jurisdiction
    source.rate_limit_per_min = rate_limit_per_min
    source.enabled = enabled
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


def holds_body_rights() -> sa.ColumnElement[bool]:
    """The FR-S5 predicate, for a query selecting from ``canonical_record``.

    Rights live in one place — the registry — and are read here at the point of use. There is
    deliberately no copy on the article: rights are a relationship and relationships change,
    so a stored answer would report what was true at acquisition and keep reporting it after
    a downgrade (RFC §5.2, rev 20). Reading through the source means a downgrade takes effect
    for records already ingested, with nothing to cascade.

    A correlated EXISTS rather than a join, so it composes into a caller's own query without
    changing that query's shape or row count.
    """
    return (
        sa.select(sa.literal(1))
        .where(
            Source.source_id == CanonicalRecord.source_id,
            Source.rights_level == RightsLevel.BODY_TEXT,
        )
        .exists()
    )
