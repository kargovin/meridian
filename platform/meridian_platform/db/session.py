"""Engine and session factory."""

import sqlalchemy as sa
from meridian_config import PlatformSettings
from sqlalchemy.orm import Session, sessionmaker


def create_engine(settings: PlatformSettings) -> sa.Engine:
    return sa.create_engine(str(settings.database_url), pool_pre_ping=True)


def session_factory(engine: sa.Engine) -> sessionmaker[Session]:
    """``expire_on_commit=False``: claimed rows are read after the commit that claims them."""
    return sessionmaker(bind=engine, expire_on_commit=False)
