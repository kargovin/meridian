"""The source registry (RFC §5.1). Governs what the pipeline may do with each publisher."""

import datetime as dt

import sqlalchemy as sa
from meridian_contract import AcquisitionTier, DiscoveryMethod, RightsLevel
from meridian_dbkit import StrEnumType, TZDateTime, enum_check
from sqlalchemy.orm import Mapped, mapped_column

from meridian.db.base import Base


class Source(Base):
    __tablename__ = "source"
    __table_args__ = (
        # One row per publisher. Two rows would each carry their own source_id, so the same
        # story arriving through both counts twice in a cluster's
        # ``1 + count(DISTINCT source_id)`` — promoting a single-publisher cluster past the
        # >=2-distinct-source gate (FR-S6) with no dedup error involved. Covering one
        # publisher by two discovery methods needs a different shape, not a second row.
        sa.UniqueConstraint("home_url"),
        enum_check("discovery_method", DiscoveryMethod),
        enum_check("acquisition_tier", AcquisitionTier),
        enum_check("rights_level", RightsLevel),
    )

    source_id: Mapped[int] = mapped_column(sa.BigInteger, sa.Identity(), primary_key=True)
    name: Mapped[str] = mapped_column(sa.Text)
    home_url: Mapped[str] = mapped_column(sa.Text)

    discovery_method: Mapped[DiscoveryMethod] = mapped_column(StrEnumType(DiscoveryMethod))
    acquisition_tier: Mapped[AcquisitionTier] = mapped_column(StrEnumType(AcquisitionTier))
    rights_level: Mapped[RightsLevel] = mapped_column(StrEnumType(RightsLevel))

    jurisdiction: Mapped[str] = mapped_column(sa.Text)

    #: FR-I6: disable a source without a deploy.
    enabled: Mapped[bool] = mapped_column(sa.Boolean, server_default=sa.true())

    #: FR-I3 per-domain politeness.
    rate_limit_per_min: Mapped[int] = mapped_column(sa.Integer)

    created_at: Mapped[dt.datetime] = mapped_column(TZDateTime, server_default=sa.func.now())
    #: Operational staleness only — has anyone touched this row, and when. NOT a history: the
    #: next unrelated edit overwrites it, so it cannot say when a particular field changed.
    #: Maintained by a database trigger rather than the ORM, because the emergency path is a
    #: psql session and a column that lies on exactly that path is worse than no column.
    updated_at: Mapped[dt.datetime] = mapped_column(TZDateTime, server_default=sa.func.now())
