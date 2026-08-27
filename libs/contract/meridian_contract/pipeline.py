"""The pipeline stage sequence (RFC 2.2 §6.2).

``ARTICLE_CHAIN`` is the only declaration; every map below is derived from it, so they
cannot disagree. To add or reorder a stage, edit the chain and nothing else.

``Stage.SUMMARIZE`` is not in the chain — its subject is a cluster, not an article. It is
still a key in every map, mapping to ``None``.

Declarations only — no side effects. The helper that writes stage output, moves
``pipeline_state``, dequeues and enqueues the successor does so in one transaction and reads
these maps rather than restating the order.
----------------------------------
STATE_AFTER_STAGE - When stage X completes, what is the new article state?
when the stage handler finishes the work; it reads this to get the state to update back to db

STAGE_SUCCESSOR - When stage X completes, what stage comes next in memory?

STAGE_OWED_BY_STATE - If an article is sitting at state Y, what stage must run next?
scheduler/orch reads an entry in db and reads the pipeline_state and routes it appropriate
module based on answer returned by this
"""

from collections.abc import Mapping
from types import MappingProxyType

from .enums import PipelineState, Stage, TerminalReason

#: Where an article enters, before any stage has run.
ENTRY_STATE = PipelineState.DISCOVERED

#: (stage, the state an article is left in once that stage completes).
ARTICLE_CHAIN: tuple[tuple[Stage, PipelineState], ...] = (
    (Stage.ACQUIRE, PipelineState.ACQUIRED),
    (Stage.CLASSIFY, PipelineState.CLASSIFIED),
    (Stage.CLUSTER, PipelineState.CLUSTERED),
)

_state_after: dict[Stage, PipelineState | None] = dict.fromkeys(Stage)
_state_after.update(ARTICLE_CHAIN)
#: What ``pipeline_state`` becomes when this stage completes; ``None`` = it does not move.
# MappingProxyType creates an dyanamic and immutable copy of _state_after
STATE_AFTER_STAGE: Mapping[Stage, PipelineState | None] = MappingProxyType(_state_after)

_successor: dict[Stage, Stage | None] = dict.fromkeys(Stage)
_successor.update(
    (stage, ARTICLE_CHAIN[i + 1][0]) for i, (stage, _) in enumerate(ARTICLE_CHAIN[:-1])
)
#: What to enqueue when this stage completes; ``None`` = the chain ends here.
# MappingProxyType creates an dyanamic and immutable copy of _successor
STAGE_SUCCESSOR: Mapping[Stage, Stage | None] = MappingProxyType(_successor)

#: The state at which an article becomes readable: it has a topic and a cluster, which is
#: everything topic-browse needs. Declared here beside the chain rather than tested for in
#: the helper that acts on it, so what makes an article readable is data like the sequence
#: is. Deliberately not "the end of the chain" — a stage added after clustering would not
#: move the point at which a reader can see the article.
PROJECTABLE_STATE = PipelineState.CLUSTERED

_owed: dict[PipelineState, Stage | None] = dict.fromkeys(PipelineState)
_owed[ENTRY_STATE] = ARTICLE_CHAIN[0][0]
_owed.update((state, ARTICLE_CHAIN[i + 1][0]) for i, (_, state) in enumerate(ARTICLE_CHAIN[:-1]))
#: What an article in this state still owes; ``None`` = nothing.
# MappingProxyType creates an dyanamic and immutable copy of _successor
STAGE_OWED_BY_STATE: Mapping[PipelineState, Stage | None] = MappingProxyType(_owed)


def owed_stage(state: PipelineState, terminal_reason: TerminalReason | None = None) -> Stage | None:
    """Which stage an article in this state still owes; ``None`` if it owes nothing.

    Article work is a pure function of these two columns, which is what makes the work
    queue rebuildable (RFC §6.2). Summarize work is not derivable here — its subject is a
    cluster and derives from ``Cluster.distinct_source_count`` and
    ``Summary.input_fingerprint`` instead (MER-44).
    """
    if terminal_reason is not None:
        return None
    return STAGE_OWED_BY_STATE[state]
