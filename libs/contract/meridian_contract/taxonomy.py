"""The v1 topic taxonomy (PRD §6).

Deliberately **not** in ``enums.py``. That module is the source of truth for the
``varchar`` + ``CHECK`` columns of the canonical data model, and ``topic`` is plain text
validated in the application so that a taxonomy-v2 *removal* stays possible: a CHECK is
evaluated against existing rows, so narrowing one fails while any row still holds the
dropped value. Wiring ``Topic`` to a column would take that away.

Nor is this the wire type. ``Classification.topic`` is ``str`` so a generated client does
not acquire an exhaustive match over a label set that ``taxonomy_version`` exists to let
us change without an API break.

What it *is*: the internal vocabulary — eval-set gold labels, and application-side
validation of a classifier's output.
"""

from enum import StrEnum


class Topic(StrEnum):
    """The v1 label set.

    ``OTHER`` is both the genuine catch-all and the low-confidence fallback (FR-C2). The
    two are indistinguishable in the output and are treated alike everywhere downstream:
    either way the reader does not find the article under a topic.
    """

    WORLD = "world"
    NATION_POLITICS = "nation_politics"
    BUSINESS = "business"
    TECHNOLOGY = "technology"
    SCIENCE = "science"
    HEALTH = "health"
    SPORTS = "sports"
    ENTERTAINMENT = "entertainment"
    OTHER = "other"


#: The taxonomy version these members constitute. Must appear in the classify contract's
#: ``SUPPORTED_TAXONOMY_VERSIONS``; a test asserts that rather than importing across the
#: internal/wire boundary in either direction.
TAXONOMY_VERSION = "v1"

#: The topics that count as *assigned* for KR3 coverage. Derived, never hand-listed, so it
#: cannot drift from ``Topic``.
REAL_TOPICS = frozenset(topic for topic in Topic if topic is not Topic.OTHER)
