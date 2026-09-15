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
    assert sum(feeds.counts_by_source(app_session).values()) == 4


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
    entry = {
        "home_url": "https://times.example",
        "permitted_to_ingest": True,
        "determination": {"read_on": "2026-01-01", "basis": "b", "sources": ["https://x.test"]},
    }
    with pytest.raises(ValueError, match=r"entry 0.*name"):
        seeder.parse([entry])


def test_a_roster_must_be_a_list() -> None:
    with pytest.raises(ValueError, match="JSON array"):
        seeder.parse({"name": "Example Times"})


# --- the v1 roster ---------------------------------------------------------------------------

V1 = Path("seeds/v1.json")

# Named here, not derived from the file: the file is what is under test. A publisher moving
# between these sets is a rights determination changing, and that should fail a test until
# someone updates both.
V1_BODY_TEXT = {
    "Global Voices",
    "Waging Nonviolence",
    "Africa Is a Country",
    "openDemocracy",
    "European Commission",
    "World Health Organization",
    "NASA",
    "European Southern Observatory",
}
V1_HEADLINE_ONLY = {
    "NPR",
    "Inter Press Service",
    "The Conversation",
    "ProPublica",
    "Common Dreams",
    "KFF Health News",
}
# Refused by their terms. CBC's could not be fetched by a script and were read in a browser.
V1_NOT_PERMITTED = {
    "BBC",
    "The Guardian",
    "Associated Press",
    "Al Jazeera",
    "Reuters",
    "Deutsche Welle",
    "Sky News",
    "CBC News",
}


def test_the_v1_roster_seeds_to_exactly_the_intended_poll_set(app_session: Session) -> None:
    """The file can read correctly and still poll a forbidden publisher; only the poll set says.

    ``feeds.pollable()`` is the query discovery runs, so its answer is what gets polled — not
    what the file appears to say. A refused publisher must be *present* (the determination
    survives) and *absent from the poll set* (it is never touched).
    """
    entries = seeder.parse(json.loads(V1.read_text()))
    inserted, skipped = seeder.seed(app_session, entries)
    assert skipped == []
    assert set(inserted) == V1_BODY_TEXT | V1_HEADLINE_ONLY | V1_NOT_PERMITTED

    polled_publishers = {source.name for _feed, source in feeds.pollable(app_session)}
    assert polled_publishers == V1_BODY_TEXT | V1_HEADLINE_ONLY
    assert polled_publishers.isdisjoint(V1_NOT_PERMITTED)
    assert {s.name for s in sources.enabled(app_session)} == V1_BODY_TEXT | V1_HEADLINE_ONLY

    # Present, not deleted: the roster can say "we looked, and the answer was no".
    by_name = {s.name: s for s in sources.list_all(app_session)}
    assert set(by_name) >= V1_NOT_PERMITTED
    assert all(by_name[n].permitted_to_ingest is False for n in V1_NOT_PERMITTED)
    assert all(by_name[n].rights_level == RightsLevel.BODY_TEXT for n in V1_BODY_TEXT)
    assert all(by_name[n].rights_level == RightsLevel.HEADLINE_ONLY for n in V1_HEADLINE_ONLY)


def test_the_shipped_example_seeds_to_its_intended_poll_set(app_session: Session) -> None:
    """The example carries the two stops the v1 roster does not exercise together.

    Example Wire is permitted but switched off (``enabled: false``); Example Herald is not
    permitted at all. Only Example Times may be polled. Without this, a poll query that
    dropped the ``enabled`` gate would pass the v1 test, because nothing in v1 is disabled.
    """
    seeder.seed(app_session, seeder.parse(json.loads(EXAMPLE.read_text())))

    polled = {(source.name, feed.name) for feed, source in feeds.pollable(app_session)}
    assert polled == {("Example Times", "World"), ("Example Times", "UK")}
    assert {s.name for s in sources.list_all(app_session)} == {
        "Example Times",
        "Example Wire",
        "Example Herald",
    }


def test_a_headline_only_publisher_never_has_a_full_feed_tier() -> None:
    """A feed that ships full text is not a licence to store it.

    ``1_full_feed`` writes the body from the feed at discovery. For a publisher whose terms
    forbid using the body, that stores what may not be held — so the tier must be extraction
    even where the feed carries the text, and the rights gate then never asks for it.
    """
    for entry in seeder.parse(json.loads(V1.read_text())):
        if entry.rights_level is RightsLevel.HEADLINE_ONLY:
            tiers = {f.acquisition_tier for f in entry.feeds}
            assert AcquisitionTier.FULL_FEED not in tiers, entry.name


def test_every_v1_publisher_states_permitted_to_ingest_explicitly() -> None:
    """The key, not the value: an omitted key and a considered "yes" are the same bytes."""
    for index, item in enumerate(json.loads(V1.read_text())):
        assert "permitted_to_ingest" in item, f"entry {index} ({item.get('name')})"
        assert isinstance(item["permitted_to_ingest"], bool)


# --- what the file must say -------------------------------------------------------------------


def _publisher(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "name": "Example Times",
        "home_url": "https://times.example",
        "permitted_to_ingest": True,
        "rights_level": "headline_only",
        "jurisdiction": "GB",
        "rate_limit_per_min": 5,
        "determination": {
            "read_on": "2026-09-04",
            "basis": "fictional",
            "sources": ["https://times.example/terms"],
        },
    }
    return {**base, **overrides}


def test_permitted_to_ingest_is_required_not_defaulted() -> None:
    entry = _publisher()
    del entry["permitted_to_ingest"]

    with pytest.raises(ValueError, match=r"entry 0 is missing 'permitted_to_ingest'"):
        seeder.parse([entry])


def test_a_misspelled_key_is_an_error_not_a_silent_default() -> None:
    """The failure this guards against: a typo parses clean and the publisher is polled.

    Before this check, ``permited_to_ingest: false`` was ignored and the entry came out with
    ``permitted_to_ingest == True`` — the file recording a refusal that the registry never saw.
    """
    entry = _publisher()
    del entry["permitted_to_ingest"]
    entry["permited_to_ingest"] = False

    with pytest.raises(ValueError, match=r"unknown key.*permited_to_ingest"):
        seeder.parse([entry])


def test_an_unknown_key_on_a_feed_names_the_feed() -> None:
    entry = _publisher(
        feeds=[
            {
                "name": "World",
                "url": "https://feeds.times.example/world.xml",
                "discovery_method": "rss",
                "acquisiton_tier": "3_extraction",
            }
        ]
    )

    with pytest.raises(ValueError, match=r"entry 0 feed 0 has unknown key.*acquisiton_tier"):
        seeder.parse([entry])


@pytest.mark.parametrize(
    ("determination", "message"),
    [
        (None, r"missing 'determination'"),
        ({"basis": "b", "sources": ["https://x.test/t"]}, r"missing 'read_on'"),
        ({"read_on": "yesterday", "basis": "b", "sources": ["https://x.test/t"]}, r"read_on"),
        ({"read_on": "2026-09-04", "basis": "  ", "sources": ["https://x.test/t"]}, r"basis"),
        ({"read_on": "2026-09-04", "basis": "b", "sources": []}, r"sources"),
        ({"read_on": "2026-09-04", "basis": "b", "sources": ["not a url"]}, r"sources"),
        (
            {"read_on": "2026-09-04", "basis": "b", "sources": ["https://x.test/t"], "why": "?"},
            r"unknown key",
        ),
    ],
)
def test_a_determination_must_cite_what_it_rests_on(
    determination: dict[str, object] | None, message: str
) -> None:
    """An unsourced determination cannot be re-checked — only re-decided, or trusted forever."""
    entry = _publisher()
    if determination is None:
        del entry["determination"]
    else:
        entry["determination"] = determination

    with pytest.raises(ValueError, match=message):
        seeder.parse([entry])


def test_permitted_to_ingest_must_be_a_boolean() -> None:
    with pytest.raises(ValueError, match=r"true or false"):
        seeder.parse([_publisher(permitted_to_ingest="no")])


@pytest.mark.parametrize("value", ["false", 0, None, "no"])
def test_enabled_must_be_a_boolean_on_a_publisher(value: object) -> None:
    """``bool("false")`` is True. A stop spelled as a string would switch the publisher on."""
    with pytest.raises(ValueError, match=r"entry 0: enabled must be true or false"):
        seeder.parse([_publisher(enabled=value)])


def test_enabled_must_be_a_boolean_on_a_feed() -> None:
    feed = {
        "name": "World",
        "url": "https://feeds.times.example/world.xml",
        "discovery_method": "rss",
        "acquisition_tier": "3_extraction",
        "enabled": "false",
    }
    with pytest.raises(ValueError, match=r"entry 0 feed 0: enabled must be true or false"):
        seeder.parse([_publisher(feeds=[feed])])


def test_a_feed_note_must_be_a_string() -> None:
    feed = {
        "name": "World",
        "url": "https://feeds.times.example/world.xml",
        "discovery_method": "rss",
        "acquisition_tier": "3_extraction",
        "note": 7,
    }
    with pytest.raises(ValueError, match=r"entry 0 feed 0: note must be a string"):
        seeder.parse([_publisher(feeds=[feed])])


@pytest.mark.parametrize("conditions", ["attribute", [1], [None]])
def test_determination_conditions_must_be_a_list_of_strings(conditions: object) -> None:
    det: dict[str, object] = {
        "read_on": "2026-09-04",
        "basis": "b",
        "sources": ["https://x.test/t"],
    }
    det["conditions"] = conditions
    with pytest.raises(ValueError, match=r"conditions must be a list of strings"):
        seeder.parse([_publisher(determination=det)])


def test_a_determination_must_be_an_object() -> None:
    """A list here used to escape as a TypeError from indexing, naming no entry."""
    with pytest.raises(ValueError, match=r"entry 0: determination is not an object"):
        seeder.parse([_publisher(determination=[])])


@pytest.mark.parametrize(
    "read_on", ["20260904", "2026-9-4", "2026-W36", 20260904, "2026-09-04T00:00"]
)
def test_read_on_is_a_plain_iso_date(read_on: object) -> None:
    det = {"read_on": read_on, "basis": "b", "sources": ["https://x.test/t"]}
    with pytest.raises(ValueError, match=r"read_on must be a YYYY-MM-DD date"):
        seeder.parse([_publisher(determination=det)])


def test_a_source_url_needs_a_host() -> None:
    det = {"read_on": "2026-09-04", "basis": "b", "sources": ["https://"]}
    with pytest.raises(ValueError, match=r"http\(s\) URLs"):
        seeder.parse([_publisher(determination=det)])


@pytest.mark.parametrize(
    ("key", "value"),
    [("name", ""), ("name", None), ("home_url", ""), ("home_url", "times.example")],
)
def test_name_and_home_url_are_real(key: str, value: object) -> None:
    with pytest.raises(ValueError, match=rf"entry 0: {key}"):
        seeder.parse([_publisher(**{key: value})])


@pytest.mark.parametrize("rate", [0, -1, True, "5"])
def test_rate_limit_is_a_positive_integer_before_anything_is_written(rate: object) -> None:
    """The database CHECK would refuse it at flush, with an error naming no entry."""
    with pytest.raises(ValueError, match=r"entry 0: rate_limit_per_min must be a positive integer"):
        seeder.parse([_publisher(rate_limit_per_min=rate)])


def test_a_feed_url_may_appear_once_in_a_roster() -> None:
    """Two feeds with one URL die at insert on the unique index, half-loaded, naming nothing."""
    feed = {
        "name": "World",
        "url": "https://feeds.times.example/world.xml",
        "discovery_method": "rss",
        "acquisition_tier": "3_extraction",
    }
    other = _publisher(name="Example Herald", home_url="https://herald.example", feeds=[feed])
    with pytest.raises(ValueError, match=r"entry 1: feed url .* appears twice"):
        seeder.parse([_publisher(feeds=[feed]), other])
