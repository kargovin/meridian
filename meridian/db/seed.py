"""Loading an initial source roster (FR-I2).

Run as ``python -m meridian.db.seed <path-to-roster.json>``.

A roster entry is a publisher with its feeds nested inside it, because that is the shape the
registry has: rights, jurisdiction and the rate limit are determinations about an outlet, while
a URL and how to read it are facts about one feed, and a publisher can have several.

⚠️ Seeding inserts and never updates. A publisher already in the registry is left exactly as it
is — including its ``enabled``, ``permitted_to_ingest`` and ``rights_level`` — and its feeds are
left alone with it. Those are the fields an operator changes under pressure, a Legal call or a
ToS complaint, and a seed that reconciled the registry towards a file would silently undo that
on the next deploy. The registry is authoritative once a row exists; the file only bootstraps.

Matching is on ``home_url``, which identifies a publisher more stably than its name. Nothing in
the schema enforces that uniqueness — ``home_url`` is not canonicalised, so a trailing slash or
a ``www.`` is a different value — and two rows for one publisher remain possible through the
admin surface. This module will not create the second one; that is the extent of the guarantee.
"""

import json
import sys
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

import sqlalchemy as sa
from meridian_contract import AcquisitionTier, DiscoveryMethod, RightsLevel
from sqlalchemy.orm import Session

from meridian.db import feeds as feeds_repo
from meridian.db import sources
from meridian.db.models import Source
from meridian.db.session import create_engine, session_factory


@dataclass(frozen=True)
class FeedEntry:
    name: str
    url: str
    discovery_method: DiscoveryMethod
    acquisition_tier: AcquisitionTier
    enabled: bool = True


@dataclass(frozen=True)
class SeedEntry:
    name: str
    home_url: str
    rights_level: RightsLevel
    jurisdiction: str
    rate_limit_per_min: int
    feeds: tuple[FeedEntry, ...] = ()
    user_agent: str | None = None
    enabled: bool = True
    permitted_to_ingest: bool = True


def _parse_feed(item: object, where: str) -> FeedEntry:
    if not isinstance(item, dict):
        raise ValueError(f"{where} is not an object")
    try:
        return FeedEntry(
            name=str(item["name"]),
            url=str(item["url"]),
            discovery_method=DiscoveryMethod(item["discovery_method"]),
            acquisition_tier=AcquisitionTier(item["acquisition_tier"]),
            enabled=bool(item.get("enabled", True)),
        )
    except KeyError as missing:
        raise ValueError(f"{where} is missing {missing}") from missing
    except ValueError as bad:
        raise ValueError(f"{where}: {bad}") from bad


def parse(raw: object) -> list[SeedEntry]:
    """Read a roster, rejecting anything the registry could not hold.

    Enum members are resolved here rather than at insert time so a typo in the file fails
    before the first row is written, instead of leaving a half-loaded registry.
    """
    if not isinstance(raw, list):
        raise ValueError("a roster is a JSON array of publisher objects")
    entries = []
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            raise ValueError(f"entry {index} is not an object")
        raw_feeds = item.get("feeds", [])
        if not isinstance(raw_feeds, list):
            raise ValueError(f"entry {index}: feeds is not an array")
        try:
            entries.append(
                SeedEntry(
                    name=str(item["name"]),
                    home_url=str(item["home_url"]),
                    rights_level=RightsLevel(item["rights_level"]),
                    jurisdiction=str(item["jurisdiction"]),
                    rate_limit_per_min=int(item["rate_limit_per_min"]),
                    feeds=tuple(
                        _parse_feed(f, f"entry {index} feed {n}") for n, f in enumerate(raw_feeds)
                    ),
                    user_agent=(
                        str(item["user_agent"]) if item.get("user_agent") is not None else None
                    ),
                    enabled=bool(item.get("enabled", True)),
                    permitted_to_ingest=bool(item.get("permitted_to_ingest", True)),
                )
            )
        except KeyError as missing:
            raise ValueError(f"entry {index} is missing {missing}") from missing
        except ValueError as bad:
            raise ValueError(f"entry {index}: {bad}") from bad
    return entries


def seed(session: Session, entries: Iterable[SeedEntry]) -> tuple[list[str], list[str]]:
    """Insert every publisher the registry does not already hold, with its feeds.

    Returns the names inserted and the names skipped, so a caller can report both rather than
    a single count that hides which half happened. A skipped publisher's feeds are skipped with
    it — reconciling them would be an update, and this never updates.
    """
    known = set(session.scalars(sa.select(Source.home_url)).all())
    inserted, skipped = [], []
    for entry in entries:
        if entry.home_url in known:
            skipped.append(entry.name)
            continue
        source = sources.create(
            session,
            name=entry.name,
            home_url=entry.home_url,
            rights_level=entry.rights_level,
            jurisdiction=entry.jurisdiction,
            rate_limit_per_min=entry.rate_limit_per_min,
            user_agent=entry.user_agent,
            enabled=entry.enabled,
            permitted_to_ingest=entry.permitted_to_ingest,
        )
        for feed in entry.feeds:
            feeds_repo.create(
                session,
                source_id=source.source_id,
                name=feed.name,
                url=feed.url,
                discovery_method=feed.discovery_method,
                acquisition_tier=feed.acquisition_tier,
                enabled=feed.enabled,
            )
        known.add(entry.home_url)
        inserted.append(entry.name)
    return inserted, skipped


def main(argv: Sequence[str]) -> int:
    if len(argv) != 1:
        print("usage: python -m meridian.db.seed <roster.json>", file=sys.stderr)
        return 2
    from meridian_config import load_app

    entries = parse(json.loads(Path(argv[0]).read_text()))
    with session_factory(create_engine(load_app()))() as session:
        inserted, skipped = seed(session, entries)
        session.commit()
    print(f"inserted {len(inserted)}: {', '.join(inserted) or '-'}")
    print(f"already present {len(skipped)}: {', '.join(skipped) or '-'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
