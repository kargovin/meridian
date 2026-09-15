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

⚠️ The file must say more than the registry stores. ``permitted_to_ingest`` defaults to true in
the database, so an omitted key and a considered "yes" are the same bytes on disk and a reader
cannot tell which publishers were actually looked at — the key is therefore required here, on
every publisher. Each publisher also carries a ``determination`` block naming what its rights
rest on (a URL, the date it was read, the clause); it is validated and then discarded, because
there is no column for it — the file is its record. Unknown keys are rejected rather than
ignored: a misspelled ``permitted_to_ingest`` that parsed clean would fall back to the default
and poll a publisher the file meant to refuse.
"""

import datetime as dt
import json
import re
import sys
import urllib.parse
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


_FEED_KEYS = frozenset({"name", "url", "discovery_method", "acquisition_tier", "enabled", "note"})
_PUBLISHER_KEYS = frozenset(
    {
        "name",
        "home_url",
        "permitted_to_ingest",
        "rights_level",
        "jurisdiction",
        "rate_limit_per_min",
        "feeds",
        "user_agent",
        "enabled",
        "determination",
    }
)
_DETERMINATION_KEYS = frozenset({"read_on", "basis", "sources", "conditions"})


_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _bool(item: dict[str, object], key: str, where: str, default: bool | None = None) -> bool:
    """A real JSON boolean, never coerced: ``"false"`` is truthy and ``0`` is not an answer."""
    if key not in item:
        if default is None:
            raise ValueError(f"{where} is missing '{key}'")
        return default
    value = item[key]
    if not isinstance(value, bool):
        raise ValueError(f"{where}: {key} must be true or false")
    return value


def _nonempty(item: dict[str, object], key: str, where: str) -> str:
    value = item.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{where}: {key} must be a non-empty string")
    return value


def _reject_unknown(item: dict[str, object], allowed: frozenset[str], where: str) -> None:
    unknown = sorted(set(item) - allowed)
    if unknown:
        raise ValueError(f"{where} has unknown key(s) {unknown}; allowed: {sorted(allowed)}")


def _check_determination(raw: object, where: str) -> None:
    """A determination must name what it rests on, or it cannot be re-checked — only re-decided.

    ``read_on`` is the date the cited pages were read; ``basis`` quotes or paraphrases the
    clause; ``sources`` are the URLs it was read from. None of this is stored.
    """
    if not isinstance(raw, dict):
        raise ValueError(f"{where}: determination is not an object")
    _reject_unknown(raw, _DETERMINATION_KEYS, f"{where} determination")
    for key in ("read_on", "basis", "sources"):
        if key not in raw:
            raise ValueError(f"{where} determination is missing '{key}'")
    read_on = raw["read_on"]
    if not isinstance(read_on, str) or not _ISO_DATE.match(read_on):
        raise ValueError(f"{where} determination: read_on must be a YYYY-MM-DD date")
    try:
        dt.date.fromisoformat(read_on)
    except ValueError as bad:
        raise ValueError(f"{where} determination: read_on {bad}") from bad
    basis = raw["basis"]
    if not isinstance(basis, str) or not basis.strip():
        raise ValueError(f"{where} determination: basis must be a non-empty string")
    sources = raw["sources"]
    if not isinstance(sources, list) or not sources or not all(_is_url(u) for u in sources):
        raise ValueError(f"{where} determination: sources must be a non-empty list of http(s) URLs")
    conditions = raw.get("conditions", [])
    if not isinstance(conditions, list) or not all(isinstance(c, str) for c in conditions):
        raise ValueError(f"{where} determination: conditions must be a list of strings")


def _is_url(value: object) -> bool:
    if not isinstance(value, str):
        return False
    parts = urllib.parse.urlsplit(value)
    return parts.scheme in ("http", "https") and bool(parts.netloc)


def _parse_feed(item: object, where: str) -> FeedEntry:
    if not isinstance(item, dict):
        raise ValueError(f"{where} is not an object")
    _reject_unknown(item, _FEED_KEYS, where)
    if "note" in item and not isinstance(item["note"], str):
        raise ValueError(f"{where}: note must be a string")
    name = _nonempty(item, "name", where)
    url = _nonempty(item, "url", where)
    if not _is_url(url):
        raise ValueError(f"{where}: url must be an http(s) URL")
    enabled = _bool(item, "enabled", where, default=True)
    try:
        return FeedEntry(
            name=name,
            url=url,
            discovery_method=DiscoveryMethod(item["discovery_method"]),
            acquisition_tier=AcquisitionTier(item["acquisition_tier"]),
            enabled=enabled,
        )
    except KeyError as missing:
        raise ValueError(f"{where} is missing {missing}") from missing
    except ValueError as bad:
        raise ValueError(f"{where}: {bad}") from bad


def parse(raw: object) -> list[SeedEntry]:
    """Read a roster, rejecting anything the registry could not hold or the file must state.

    Enum members are resolved here rather than at insert time so a typo in the file fails
    before the first row is written, instead of leaving a half-loaded registry. The same goes
    for the file's own rules: ``permitted_to_ingest`` is required (never defaulted — see the
    module docstring), a ``determination`` is required, and an unknown key is an error.
    """
    if not isinstance(raw, list):
        raise ValueError("a roster is a JSON array of publisher objects")
    entries = []
    seen_feed_urls: set[str] = set()
    for index, item in enumerate(raw):
        where = f"entry {index}"
        if not isinstance(item, dict):
            raise ValueError(f"{where} is not an object")
        _reject_unknown(item, _PUBLISHER_KEYS, where)
        raw_feeds = item.get("feeds", [])
        if not isinstance(raw_feeds, list):
            raise ValueError(f"{where}: feeds is not an array")
        permitted = _bool(item, "permitted_to_ingest", where)
        enabled = _bool(item, "enabled", where, default=True)
        if "determination" not in item:
            raise ValueError(f"{where} is missing 'determination'")
        _check_determination(item["determination"], where)
        name = _nonempty(item, "name", where)
        home_url = _nonempty(item, "home_url", where)
        if not _is_url(home_url):
            raise ValueError(f"{where}: home_url must be an http(s) URL")
        rate = item.get("rate_limit_per_min")
        if not isinstance(rate, int) or isinstance(rate, bool) or rate <= 0:
            raise ValueError(f"{where}: rate_limit_per_min must be a positive integer")
        parsed_feeds = tuple(_parse_feed(f, f"{where} feed {n}") for n, f in enumerate(raw_feeds))
        for url in (f.url for f in parsed_feeds):
            if url in seen_feed_urls:
                raise ValueError(f"{where}: feed url {url!r} appears twice in the roster")
            seen_feed_urls.add(url)
        try:
            entries.append(
                SeedEntry(
                    name=name,
                    home_url=home_url,
                    rights_level=RightsLevel(item["rights_level"]),
                    jurisdiction=_nonempty(item, "jurisdiction", where),
                    rate_limit_per_min=rate,
                    feeds=parsed_feeds,
                    user_agent=(
                        str(item["user_agent"]) if item.get("user_agent") is not None else None
                    ),
                    enabled=enabled,
                    permitted_to_ingest=permitted,
                )
            )
        except KeyError as missing:
            raise ValueError(f"{where} is missing {missing}") from missing
        except ValueError as bad:
            raise ValueError(f"{where}: {bad}") from bad
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
