"""The stage sequence declarations (RFC §6.2)."""

import pytest
from meridian_contract import (
    ARTICLE_CHAIN,
    ENTRY_STATE,
    STAGE_OWED_BY_STATE,
    STAGE_SUCCESSOR,
    STATE_AFTER_STAGE,
    PipelineState,
    Stage,
    TerminalReason,
    owed_stage,
)


def test_every_stage_declares_a_successor() -> None:
    """Adding a stage without placing it in the chain must fail here, not in production.

    A missing key would raise KeyError inside a handler that has already committed its work.
    """
    assert set(STAGE_SUCCESSOR) == set(Stage)
    assert set(STATE_AFTER_STAGE) == set(Stage)


def test_every_state_declares_what_it_owes() -> None:
    assert set(STAGE_OWED_BY_STATE) == set(PipelineState)


def test_chain_terminates() -> None:
    seen: list[Stage] = []
    stage: Stage | None = ARTICLE_CHAIN[0][0]
    while stage is not None:
        assert stage not in seen, f"cycle through {stage}"
        seen.append(stage)
        stage = STAGE_SUCCESSOR[stage]
    assert seen == [Stage.ACQUIRE, Stage.CLASSIFY, Stage.CLUSTER]


def test_summarize_is_outside_the_article_chain() -> None:
    """Its subject is a cluster, so it neither follows a stage nor moves an article's state."""
    assert STAGE_SUCCESSOR[Stage.SUMMARIZE] is None
    assert STATE_AFTER_STAGE[Stage.SUMMARIZE] is None
    assert Stage.SUMMARIZE not in [stage for stage, _ in ARTICLE_CHAIN]


def test_owed_and_state_after_are_consistent() -> None:
    """The two derived maps must agree: finishing a stage leaves you owing the next one."""
    for stage, state in ARTICLE_CHAIN:
        assert STAGE_OWED_BY_STATE[state] == STAGE_SUCCESSOR[stage]


def test_owed_stage_walks_the_chain() -> None:
    assert owed_stage(ENTRY_STATE) is Stage.ACQUIRE
    assert owed_stage(PipelineState.ACQUIRED) is Stage.CLASSIFY
    assert owed_stage(PipelineState.CLASSIFIED) is Stage.CLUSTER
    assert owed_stage(PipelineState.CLUSTERED) is None


def test_terminal_articles_owe_nothing() -> None:
    for reason in TerminalReason:
        assert owed_stage(ENTRY_STATE, reason) is None


def test_maps_are_read_only() -> None:
    """Module state — a stray write would reorder the pipeline for the whole process."""
    with pytest.raises(TypeError):
        STAGE_SUCCESSOR[Stage.CLUSTER] = Stage.SUMMARIZE  # type: ignore[index]
