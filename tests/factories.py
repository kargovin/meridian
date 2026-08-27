"""Minimal builders for the schema tests. Only the columns a test needs to vary are arguments."""

import datetime as dt
import hashlib
import itertools
from typing import Any

from meridian_contract import (
    AcquisitionTier,
    DiscoveryMethod,
    PipelineState,
    RightsLevel,
    Stage,
    WithholdReason,
)
from sqlalchemy.orm import Session

from meridian.db.models import (
    AlternateCopy,
    CanonicalRecord,
    Cluster,
    ClusterMember,
    Feed,
    PipelineWork,
    Source,
    Summary,
)


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def make_source(app_session: Session, name: str = "Example News", **kw: Any) -> Source:
    """A publisher. Its feeds are separate — see ``make_feed``."""
    defaults: dict[str, Any] = {
        "name": name,
        "home_url": f"https://{name.lower().replace(' ', '')}.example",
        "rights_level": RightsLevel.BODY_TEXT,
        "jurisdiction": "GB",
        "rate_limit_per_min": 30,
    }
    source = Source(**{**defaults, **kw})
    app_session.add(source)
    app_session.flush()
    return source


_feed_seq = itertools.count(1)


def make_feed(app_session: Session, source: Source, **kw: Any) -> Feed:
    """A feed of one publisher. The URL is unique per call so two feeds can coexist."""
    n = next(_feed_seq)
    defaults: dict[str, Any] = {
        "source_id": source.source_id,
        "name": f"Feed {n}",
        "url": f"https://feeds.example/{n}.xml",
        "discovery_method": DiscoveryMethod.RSS,
        "acquisition_tier": AcquisitionTier.FULL_FEED,
    }
    feed = Feed(**{**defaults, **kw})
    app_session.add(feed)
    app_session.flush()
    return feed


def make_article(
    app_session: Session,
    source: Source,
    *,
    guid: str = "guid-1",
    url: str | None = None,
    state: PipelineState = PipelineState.DISCOVERED,
    title: str | None = None,
    **kw: Any,
) -> CanonicalRecord:
    """An article. ``title`` is a parameter because normalization decides on its text."""
    article = CanonicalRecord(
        source_id=source.source_id,
        guid=guid,
        url_canonical=url or f"https://example.test/{guid}",
        title=title if title is not None else f"Headline {guid}",
        first_seen_at=dt.datetime.now(dt.UTC),
        pipeline_state=state,
        **kw,
    )
    app_session.add(article)
    app_session.flush()
    return article


def make_alternate_copy(
    app_session: Session,
    article: CanonicalRecord,
    source: Source,
    *,
    guid: str | None = "alt-1",
    url: str = "https://other.test/alt-1",
) -> AlternateCopy:
    copy = AlternateCopy(
        article_id=article.article_id,
        source_id=source.source_id,
        guid=guid,
        url=url,
        seen_at=dt.datetime.now(dt.UTC),
    )
    app_session.add(copy)
    app_session.flush()
    return copy


def make_cluster(app_session: Session, **kw: Any) -> Cluster:
    cluster = Cluster(**kw)
    app_session.add(cluster)
    app_session.flush()
    return cluster


def make_member(app_session: Session, cluster: Cluster, article: CanonicalRecord) -> ClusterMember:
    """Put an article in a cluster. One row per article — the PK enforces it."""
    member = ClusterMember(cluster_id=cluster.cluster_id, article_id=article.article_id)
    app_session.add(member)
    app_session.flush()
    return member


def make_summary(
    app_session: Session,
    cluster: Cluster,
    *,
    text: str | None = None,
    withhold_reason: WithholdReason = WithholdReason.NONE,
    **kw: Any,
) -> Summary:
    """A cluster's summary, withheld or not.

    A withheld summary is a row, never an absent one, so ``withhold_reason`` alone decides
    ``withheld`` — passing them separately is the only way to build a row the CHECK rejects,
    and no test wants that by accident.
    """
    withheld = withhold_reason is not WithholdReason.NONE
    summary = Summary(
        cluster_id=cluster.cluster_id,
        text=None if withheld else text,
        withheld=withheld,
        withhold_reason=withhold_reason,
        **kw,
    )
    app_session.add(summary)
    app_session.flush()
    return summary


def make_work(
    app_session: Session,
    *,
    stage: Stage,
    article: CanonicalRecord | None = None,
    cluster: Cluster | None = None,
    **kw: Any,
) -> PipelineWork:
    work = PipelineWork(
        stage=stage,
        article_id=article.article_id if article else None,
        cluster_id=cluster.cluster_id if cluster else None,
        next_attempt_at=kw.pop("next_attempt_at", dt.datetime.now(dt.UTC)),
        **kw,
    )
    app_session.add(work)
    app_session.flush()
    return work
