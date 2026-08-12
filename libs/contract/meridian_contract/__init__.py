"""Vocabularies and pipeline sequence shared by both deployables.

Dependency-free by design: the Platform service imports this and must never be able to
reach ``meridian.db``.
"""

from .enums import (
    AcquisitionTier,
    BodyProvenance,
    DiscoveryMethod,
    FallbackReason,
    PipelineState,
    RightsLevel,
    Stage,
    TakedownScope,
    TerminalReason,
    WindowStatus,
    WithholdReason,
)
from .pipeline import (
    ARTICLE_CHAIN,
    ENTRY_STATE,
    STAGE_OWED_BY_STATE,
    STAGE_SUCCESSOR,
    STATE_AFTER_STAGE,
    owed_stage,
)

__all__ = [
    "ARTICLE_CHAIN",
    "ENTRY_STATE",
    "STAGE_OWED_BY_STATE",
    "STAGE_SUCCESSOR",
    "STATE_AFTER_STAGE",
    "AcquisitionTier",
    "BodyProvenance",
    "DiscoveryMethod",
    "FallbackReason",
    "PipelineState",
    "RightsLevel",
    "Stage",
    "TakedownScope",
    "TerminalReason",
    "WindowStatus",
    "WithholdReason",
    "owed_stage",
]
