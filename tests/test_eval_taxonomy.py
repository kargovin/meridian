"""The v1 taxonomy, and the two boundaries it must not cross."""

from meridian_contract.api.classify import SUPPORTED_TAXONOMY_VERSIONS
from meridian_contract.taxonomy import REAL_TOPICS, TAXONOMY_VERSION, Topic


def test_taxonomy_matches_the_prd_label_set() -> None:
    """Nine members, flat (PRD §6). Adding or removing one is a governed, costed change:
    the labelled eval set and any fine-tuned classifier are bound to this label set."""
    assert {topic.value for topic in Topic} == {
        "world",
        "nation_politics",
        "business",
        "technology",
        "science",
        "health",
        "sports",
        "entertainment",
        "other",
    }


def test_real_topics_excludes_other_and_is_derived() -> None:
    """``Other`` is not a topic a reader browses, so it is not an assignment.

    Derived from ``Topic`` rather than listed, so a tenth member cannot be added to one and
    forgotten in the other.
    """
    assert Topic.OTHER not in REAL_TOPICS
    assert frozenset(Topic) - {Topic.OTHER} == REAL_TOPICS


def test_taxonomy_version_is_one_the_platform_serves() -> None:
    """The internal label set and the wire's version string must agree.

    Asserted rather than imported: making one module import the other would couple the
    internal vocabulary to the published contract in whichever direction the import ran.
    """
    assert TAXONOMY_VERSION in SUPPORTED_TAXONOMY_VERSIONS


def test_the_wire_does_not_publish_the_enum() -> None:
    """``Classification.topic`` stays ``str``.

    Typing it with ``Topic`` puts an enum constraint into the frozen OpenAPI document, and a
    generated client then acquires an exhaustive match over the label set — breaking on the
    first taxonomy change, which is the exact break ``taxonomy_version`` exists to prevent.
    """
    from meridian_contract.api.classify import Classification

    assert Classification.model_fields["topic"].annotation is str
