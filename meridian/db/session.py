"""Engine and session factory."""

import sqlalchemy as sa
from meridian_config import AppSettings
from sqlalchemy.orm import Session, sessionmaker


def create_engine(settings: AppSettings) -> sa.Engine:
    return sa.create_engine(str(settings.database_url), pool_pre_ping=True)


def session_factory(engine: sa.Engine) -> sessionmaker[Session]:
    """Sessions that keep their objects usable after commit.

    ``expire_on_commit=False`` matters for claim-and-commit: the claimed rows are read
    *after* the commit that claims them, and the default would re-query every attribute.
    """
    return sessionmaker(bind=engine, expire_on_commit=False)
