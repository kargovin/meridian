"""Enumerated vocabularies of the canonical data model (RFC 2.2 §5.1).

Source of truth for the ``varchar`` + ``CHECK`` columns in ``meridian.db``. Not native
PostgreSQL enums: a native value can never be removed, and Alembic does not autogenerate
enum changes.

``topic`` is deliberately absent — it is plain text validated in the application, because a
CHECK is evaluated against existing rows and would block a taxonomy-v2 removal.
"""

from enum import StrEnum


class DiscoveryMethod(StrEnum):
    """How a source is polled for new URLs (FR-I1)."""

    RSS = "rss"
    SITEMAP = "sitemap"
    WEBSUB = "websub"
    SECTION_SCRAPE = "section_scrape"


class AcquisitionTier(StrEnum):
    """How a feed's article bodies are obtained.

    ``0_unavailable`` means no body is obtainable from this feed by any route we run — the
    article page is gated behind a login, say — so its records carry a headline and lede only.
    It is a fact about the feed, not a rights determination: the publisher may well permit body
    use, and ``RightsLevel`` stays whatever the terms say.
    """

    UNAVAILABLE = "0_unavailable"
    FULL_FEED = "1_full_feed"
    PUBLISHER_API = "2_publisher_api"
    EXTRACTION = "3_extraction"


class RightsLevel(StrEnum):
    """What we may hold from a source, and therefore publish. Gates FR-S5.

    ``headline_only`` means the body is never stored, whatever the feed ships and whether or
    not the page could be fetched — not merely that it is excluded from summarization.
    """

    BODY_TEXT = "body_text"
    HEADLINE_ONLY = "headline_only"


class BodyProvenance(StrEnum):
    """Where one record's body actually came from — the per-article outcome of the tier."""

    TIER1_FEED = "tier1_feed"
    TIER2_API = "tier2_api"
    TIER3_EXTRACTED = "tier3_extracted"


class PipelineState(StrEnum):
    """Furthest completed stage for an article; the replay anchor (RFC §9).

    Ends at ``clustered``: summarization is per-cluster, not per-article.
    """

    DISCOVERED = "discovered"
    ACQUIRED = "acquired"
    DEDUPED = "deduped"
    CLASSIFIED = "classified"
    CLUSTERED = "clustered"


class TerminalReason(StrEnum):
    """Why an article stopped for good. NULL in the column means still live.

    A language drop is terminal, not an error — errors are retried, drops must not be.
    """

    FAILED = "failed"
    DROPPED_LANGUAGE = "dropped_language"


class FallbackReason(StrEnum):
    """Why a classification is not a confident model opinion.

    ``service_unavailable`` is retryable; ``low_confidence`` is not.
    """

    NONE = "none"
    LOW_CONFIDENCE = "low_confidence"
    SERVICE_UNAVAILABLE = "service_unavailable"


class WithholdReason(StrEnum):
    """Why a cluster has no summary text.

    ``insufficient_sources`` is the only transient one — it resolves when a second distinct
    source arrives.
    """

    NONE = "none"
    BELOW_FAITHFULNESS_BAR = "below_faithfulness_bar"
    RIGHTS_EXCLUDED = "rights_excluded"
    INSUFFICIENT_SOURCES = "insufficient_sources"


class WindowStatus(StrEnum):
    """Whether a cluster is still inside the FR-K2 recency window."""

    OPEN = "open"
    EVICTED = "evicted"


class TakedownScope(StrEnum):
    """Whether a takedown removed the representative record or one collapsed copy."""

    REPRESENTATIVE = "representative"
    ALTERNATE_COPY = "alternate_copy"


class Stage(StrEnum):
    """A unit of pipeline work.

    ``SUMMARIZE``'s subject is a cluster; the others act on an article.
    """

    ACQUIRE = "acquire"
    DEDUP = "dedup"
    CLASSIFY = "classify"
    CLUSTER = "cluster"
    SUMMARIZE = "summarize"
