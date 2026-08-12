"""The source registry (RFC §5.1). Governs what the pipeline may do with each publisher."""

import sqlalchemy as sa
from meridian_contract import AcquisitionTier, DiscoveryMethod, RightsLevel
from sqlalchemy.orm import Mapped, mapped_column

from meridian.db.base import Base
from meridian.db.types import StrEnumType, enum_check


class Source(Base):
    __tablename__ = "source"
    __table_args__ = (
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
