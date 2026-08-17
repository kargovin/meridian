"""The declarative base for the application's schema.

The base and its MetaData are per-deployable, never shared: this metadata must contain the
application's tables and nothing else, or Alembic autogenerate proposes creating another
deployable's tables in this database. The naming convention is shared, so both deployables
name their constraints identically.
"""

import sqlalchemy as sa
from meridian_dbkit import NAMING_CONVENTION
from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    metadata = sa.MetaData(naming_convention=NAMING_CONVENTION)
