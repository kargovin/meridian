"""The source registry's read and write paths (FR-I2, FR-I6, FR-S5)."""

from typing import Any

import pytest
import sqlalchemy as sa
from meridian_contract import AcquisitionTier, DiscoveryMethod, RightsLevel
from sqlalchemy.orm import Session, aliased

from meridian.db import sources
from meridian.db.models import CanonicalRecord
from tests.factories import make_article, make_source


def test_create_then_read_back(app_session: Session) -> None:
    created = sources.create(
        app_session,
        name="Example Times",
        home_url="https://times.example",
        discovery_method=DiscoveryMethod.RSS,
        acquisition_tier=AcquisitionTier.FULL_FEED,
        rights_level=RightsLevel.BODY_TEXT,
        jurisdiction="GB",
        rate_limit_per_min=20,
    )

    fetched = sources.get(app_session, created.source_id)

    assert fetched is not None
    assert fetched.name == "Example Times"
    assert fetched.enabled is True


def test_get_returns_none_for_an_unknown_id(app_session: Session) -> None:
    assert sources.get(app_session, 999_999) is None


def test_describe_writes_the_descriptive_fields(app_session: Session) -> None:
    source = make_source(app_session, name="Before")

    sources.describe(
        app_session,
        source.source_id,
        name="After",
        home_url="https://after.example",
        discovery_method=DiscoveryMethod.SITEMAP,
        jurisdiction="US",
        rate_limit_per_min=5,
    )

    fetched = sources.get(app_session, source.source_id)
    assert fetched is not None
    assert fetched.name == "After"
    assert fetched.discovery_method is DiscoveryMethod.SITEMAP
    assert fetched.jurisdiction == "US"
    assert fetched.rate_limit_per_min == 5


def test_describe_cannot_touch_the_governing_fields(app_session: Session) -> None:
    """They are what an operator changes under pressure; each has its own setter."""
    source = make_source(
        app_session,
        name="Before",
        enabled=False,
        rights_level=RightsLevel.HEADLINE_ONLY,
        acquisition_tier=AcquisitionTier.EXTRACTION,
    )

    sources.describe(
        app_session,
        source.source_id,
        name="After",
        home_url="https://after.example",
        discovery_method=DiscoveryMethod.SITEMAP,
        jurisdiction="US",
        rate_limit_per_min=5,
    )

    fetched = sources.get(app_session, source.source_id)
    assert fetched is not None
    assert fetched.enabled is False
    assert fetched.rights_level is RightsLevel.HEADLINE_ONLY
    assert fetched.acquisition_tier is AcquisitionTier.EXTRACTION


# ------------------------------------------------------------------ AC1 and AC3


def test_disabling_removes_a_source_from_what_discovery_polls(app_session: Session) -> None:
    """AC1: the read path discovery uses must reflect the toggle with nothing in between."""
    keep = make_source(app_session, name="Keep")
    stop = make_source(app_session, name="Stop")
    assert {s.source_id for s in sources.enabled(app_session)} == {keep.source_id, stop.source_id}

    sources.set_enabled(app_session, stop.source_id, value=False)

    assert {s.source_id for s in sources.enabled(app_session)} == {keep.source_id}


def test_a_disabled_source_is_still_administrable(app_session: Session) -> None:
    """Disabling stops ingestion, not editing — otherwise it could never be re-enabled."""
    source = make_source(app_session, name="Stopped", enabled=False)

    assert source.source_id in {s.source_id for s in sources.list_all(app_session)}
    assert sources.set_enabled(app_session, source.source_id, value=True) is not None
    assert {s.source_id for s in sources.enabled(app_session)} == {source.source_id}


def test_the_emergency_stop_does_not_revalidate_the_rest_of_the_row(
    app_session: Session,
) -> None:
    """AC1's falsifier in miniature.

    ``set_enabled`` exists apart from ``replace`` so that stopping ingestion cannot fail on a
    field nobody is trying to change. Routed through the full-row write, an emergency stop
    would depend on every other value being submitted and valid.
    """
    source = make_source(app_session, name="Awkward")

    assert sources.set_enabled(app_session, source.source_id, value=False) is not None

    fetched = sources.get(app_session, source.source_id)
    assert fetched is not None
    assert fetched.enabled is False
    assert fetched.name == "Awkward"


def test_tier_downgrade_takes_effect_on_the_next_read(app_session: Session) -> None:
    """AC3, by the same path as AC1."""
    source = make_source(app_session, acquisition_tier=AcquisitionTier.FULL_FEED)

    sources.set_acquisition_tier(app_session, source.source_id, tier=AcquisitionTier.EXTRACTION)

    fetched = sources.get(app_session, source.source_id)
    assert fetched is not None
    assert fetched.acquisition_tier is AcquisitionTier.EXTRACTION


def test_the_targeted_setters_report_an_unknown_id(app_session: Session) -> None:
    assert sources.set_enabled(app_session, 999_999, value=False) is None
    assert (
        sources.set_acquisition_tier(app_session, 999_999, tier=AcquisitionTier.EXTRACTION) is None
    )


# ------------------------------------------------------------------ AC2


def _with_body_rights(session: Session) -> set[int]:
    return set(
        session.scalars(
            sa.select(CanonicalRecord.article_id).where(
                CanonicalRecord.article_id.in_(sources.article_ids_with_body_rights())
            )
        ).all()
    )


def test_rights_are_read_from_the_registry_not_from_the_article(app_session: Session) -> None:
    rights = make_source(app_session, name="Full Rights", rights_level=RightsLevel.BODY_TEXT)
    headlines = make_source(
        app_session, name="Headlines Only", rights_level=RightsLevel.HEADLINE_ONLY
    )
    permitted = make_article(app_session, rights, guid="a")
    excluded = make_article(app_session, headlines, guid="b")

    holders = _with_body_rights(app_session)

    assert permitted.article_id in holders
    assert excluded.article_id not in holders


def test_a_downgrade_applies_to_articles_already_ingested(app_session: Session) -> None:
    """AC2's falsifier, and the whole reason the column was removed (RFC §5.2, rev 20).

    A stored copy answers what was true at acquisition. Downgrade the source and every record
    ingested under the old level keeps asserting the old answer — no second hand-maintained
    field, no bug, and summarization proceeds on text we no longer hold rights to. Reading
    through the registry makes the downgrade retroactive by construction.
    """
    source = make_source(app_session, rights_level=RightsLevel.BODY_TEXT)
    article = make_article(app_session, source, guid="acquired-before-the-downgrade")
    assert article.article_id in _with_body_rights(app_session)

    sources.set_rights_level(app_session, source.source_id, level=RightsLevel.HEADLINE_ONLY)

    assert article.article_id not in _with_body_rights(app_session)


def test_the_downgrade_writes_nothing_to_the_article(app_session: Session) -> None:
    """The other half of AC2: no cascade, because there is nothing to cascade.

    Asserted against the database rather than the ORM — a cascade would be a write, and a
    write is what this must not do.
    """
    source = make_source(app_session, rights_level=RightsLevel.BODY_TEXT)
    article = make_article(app_session, source, guid="untouched")
    before = app_session.execute(
        sa.text("SELECT xmin FROM canonical_record WHERE article_id = :i"),
        {"i": article.article_id},
    ).scalar_one()

    sources.set_acquisition_tier(app_session, source.source_id, tier=AcquisitionTier.EXTRACTION)
    app_session.flush()

    after = app_session.execute(
        sa.text("SELECT xmin FROM canonical_record WHERE article_id = :i"),
        {"i": article.article_id},
    ).scalar_one()
    assert after == before


@pytest.mark.parametrize("negate", [False, True])
def test_the_rights_filter_survives_every_query_shape(app_session: Session, negate: bool) -> None:
    """The shapes a correlated EXISTS silently loses its correlation in.

    A predicate correlated to the outer row is correlated by inference, and the inference
    fails whenever the outer query selects from an alias, a subquery or a CTE instead of the
    bare table. It then reads true for every row — and negated, excludes nothing, which is the
    direction that matters: the caller asking is asking what it may not summarize.
    """
    permitted = make_article(
        app_session,
        make_source(app_session, name="Full", rights_level=RightsLevel.BODY_TEXT),
        guid="a",
    )
    excluded = make_article(
        app_session,
        make_source(app_session, name="Headlines", rights_level=RightsLevel.HEADLINE_ONLY),
        guid="b",
    )
    expected = {excluded.article_id} if negate else {permitted.article_id}

    def matching(column: Any) -> set[int]:
        predicate = column.in_(sources.article_ids_with_body_rights())
        return set(
            app_session.scalars(sa.select(column).where(~predicate if negate else predicate)).all()
        )

    alias = aliased(CanonicalRecord)
    sub = sa.select(CanonicalRecord.article_id).subquery()

    assert matching(CanonicalRecord.article_id) == expected, "bare table"
    assert matching(alias.article_id) == expected, "aliased"
    assert matching(sub.c.article_id) == expected, "outer FROM is a subquery"
