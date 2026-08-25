"""Loading a source roster (FR-I2)."""

import json
from pathlib import Path

import pytest
from meridian_contract import AcquisitionTier, DiscoveryMethod, RightsLevel
from sqlalchemy.orm import Session

from meridian.db import feeds, sources
from meridian.db import seed as seeder
from tests.factories import make_source

EXAMPLE = Path("seeds/sources.example.json")


def _feed(**kw: object) -> seeder.FeedEntry:
    fields: dict[str, object] = {
        "name": "World",
        "url": "https://feeds.times.example/world.xml",
        "discovery_method": DiscoveryMethod.RSS,
        "acquisition_tier": AcquisitionTier.FULL_FEED,
    }
    return seeder.FeedEntry(**{**fields, **kw})  # type: ignore[arg-type]


def _entry(**kw: object) -> seeder.SeedEntry:
    fields: dict[str, object] = {
        "name": "Example Times",
        "home_url": "https://times.example",
        "rights_level": RightsLevel.BODY_TEXT,
        "jurisdiction": "GB",
        "rate_limit_per_min": 20,
        "feeds": (_feed(),),
    }
    return seeder.SeedEntry(**{**fields, **kw})  # type: ignore[arg-type]


def test_the_shipped_example_parses(app_session: Session) -> None:
    """The file is the documentation of the format; a stale one teaches the wrong shape."""
    entries = seeder.parse(json.loads(EXAMPLE.read_text()))

    assert len(entries) == 3
    inserted, skipped = seeder.seed(app_session, entries)
    assert len(inserted) == 3
    assert skipped == []
    # The nested feeds are loaded with their publisher, not silently dropped: a roster that
    # parsed and seeded but created no feeds would leave a registry that polls nothing.
    assert sum(feeds.counts_by_source(app_session).values()) == 3


def test_seeding_twice_inserts_nothing_the_second_time(app_session: Session) -> None:
    entry = _entry()

    assert seeder.seed(app_session, [entry]) == (["Example Times"], [])
    assert seeder.seed(app_session, [entry]) == ([], ["Example Times"])
    assert len(sources.list_all(app_session)) == 1


def test_a_duplicate_within_one_file_is_inserted_once(app_session: Session) -> None:
    """The seen set is updated as it goes, not read once at the start."""
    inserted, skipped = seeder.seed(app_session, [_entry(), _entry(name="Same Site Again")])

    assert inserted == ["Example Times"]
    assert skipped == ["Same Site Again"]


def test_a_skipped_publisher_does_not_gain_the_file_s_feeds(app_session: Session) -> None:
    """The insert-never-update rule has to hold for the nested half too.

    A publisher already in the registry keeps the feeds it has. Adding the file's feeds to it
    would be an update wearing an insert's clothes — it re-adds a feed an operator deleted
    because its URL had rotted, and does it on every deploy.
    """
    existing = make_source(app_session, name="Example Times", home_url="https://times.example")

    seeder.seed(app_session, [_entry()])

    assert feeds.for_source(app_session, existing.source_id) == []


def test_seeding_never_re_enables_a_stopped_source(app_session: Session) -> None:
    """The property that makes this safe to run on every deploy.

    ``enabled`` and ``rights_level`` are what an operator changes under pressure. A seed that
    reconciled the registry towards the file would undo a Legal stop-ingestion order on the
    next deploy, silently and with no failing test anywhere.
    """
    existing = make_source(app_session, name="Example Times", home_url="https://times.example")
    sources.set_enabled(
        app_session, existing.source_id, value=False, expected_updated_at=existing.updated_at
    )
    app_session.flush()

    seeder.seed(app_session, [_entry(enabled=True)])

    after = sources.get(app_session, existing.source_id)
    assert after is not None
    assert after.enabled is False
    assert sources.enabled(app_session) == []


def test_seeding_never_restores_a_downgraded_rights_level(app_session: Session) -> None:
    """The same argument, for the field with the sharper consequence (FR-S5)."""
    existing = make_source(
        app_session,
        name="Example Times",
        home_url="https://times.example",
        rights_level=RightsLevel.HEADLINE_ONLY,
    )

    seeder.seed(app_session, [_entry(rights_level=RightsLevel.BODY_TEXT)])

    after = sources.get(app_session, existing.source_id)
    assert after is not None
    assert after.rights_level is RightsLevel.HEADLINE_ONLY


# ------------------------------------------------------------------ parsing


def test_a_bad_enum_value_fails_before_anything_is_written(app_session: Session) -> None:
    """Parsing resolves every enum up front.

    Resolved at insert time instead, a typo halfway down leaves the registry half-loaded and
    the operator re-running a command that is no longer idempotent in the way they expect.
    """
    roster = json.loads(EXAMPLE.read_text())
    roster[1]["rights_level"] = "carrier_pigeon"

    with pytest.raises(ValueError, match="entry 1"):
        seeder.parse(roster)

    assert sources.list_all(app_session) == []


def test_a_bad_enum_inside_a_feed_names_the_feed(app_session: Session) -> None:
    """A nested failure has to say which feed, not only which publisher.

    A roster entry can carry several feeds, so "entry 1" alone leaves the operator diffing
    them by eye.
    """
    roster = json.loads(EXAMPLE.read_text())
    roster[0]["feeds"][1]["discovery_method"] = "carrier_pigeon"

    with pytest.raises(ValueError, match=r"entry 0 feed 1"):
        seeder.parse(roster)

    assert sources.list_all(app_session) == []


def test_a_missing_field_names_the_entry() -> None:
    with pytest.raises(ValueError, match=r"entry 0.*name"):
        seeder.parse([{"home_url": "https://times.example"}])


def test_a_roster_must_be_a_list() -> None:
    with pytest.raises(ValueError, match="JSON array"):
        seeder.parse({"name": "Example Times"})
