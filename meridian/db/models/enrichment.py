"""Classification output (RFC §5.1, FR-C1/C2/C3)."""

import sqlalchemy as sa
from meridian_contract import FallbackReason
from sqlalchemy.orm import Mapped, mapped_column

from meridian.db.base import Base
from meridian.db.types import StrEnumType, enum_check


class Classification(Base):
    """One row per article, overwritten in place.

    ``article_id`` is the primary key rather than a surrogate: re-running classify must
    upsert, not accumulate. ``model_version`` and ``taxonomy_version`` are audit fields —
    this table is not an experiment log.
    """

    __tablename__ = "classification"
    __table_args__ = (enum_check("fallback_reason", FallbackReason),)

    article_id: Mapped[int] = mapped_column(
        sa.ForeignKey("canonical_record.article_id", ondelete="CASCADE"), primary_key=True
    )

    #: Unconstrained text, unlike every other vocabulary here: a CHECK is evaluated against
    #: existing rows, so removing a topic in taxonomy v2 would break every v1 row. Nothing at
    #: this layer rejects an unknown topic — the classify stage validates against the PRD §6
    #: taxonomy.
    topic: Mapped[str] = mapped_column(sa.Text)

    #: Calibrated (temperature-scaled), not a raw softmax.
    confidence: Mapped[float] = mapped_column(sa.Float)

    fallback_reason: Mapped[FallbackReason] = mapped_column(
        StrEnumType(FallbackReason),
        server_default=FallbackReason.NONE.value,
    )

    taxonomy_version: Mapped[str] = mapped_column(sa.Text)
    model_version: Mapped[str] = mapped_column(sa.Text)
