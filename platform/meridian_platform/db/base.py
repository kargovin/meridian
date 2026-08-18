"""The declarative base for the Platform's schema.

Separate from the application's base and MetaData. One shared metadata collects both
deployables' tables, and each Alembic tree then proposes creating the other's.
"""

import sqlalchemy as sa
from meridian_dbkit import NAMING_CONVENTION
from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    metadata = sa.MetaData(naming_convention=NAMING_CONVENTION)
