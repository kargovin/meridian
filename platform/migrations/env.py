"""Alembic environment for the Platform's database.

Wholly separate from the application's tree: its own versions directory, its own
``alembic_version`` table, its own database, and its own MetaData. Sharing any of the four
makes one tree's autogenerate propose dropping the other's tables, and ``upgrade head``
executes what autogenerate proposes.

The URL comes from ``MERIDIAN_PLATFORM_DATABASE_URL``, never from ``alembic.ini``.
"""

from logging.config import fileConfig
from typing import Any, cast

import sqlalchemy as sa
from alembic import context
from meridian_config import load_platform
from meridian_dbkit import StrEnumType
from sqlalchemy import pool

from meridian_platform.db import Base

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

settings = load_platform()

target_metadata = Base.metadata


def render_item(type_: str, obj: object, autogen_context: object) -> str | bool:
    """Render application column types as their plain SQL equivalent.

    A migration that imports application code stops working the day that code moves.
    """
    if type_ == "type" and isinstance(obj, StrEnumType):
        return f"sa.String(length={cast(sa.String, obj.impl_instance).length})"
    return False


#: Heterogeneous by nature — annotated so ``**COMPARE_OPTS`` type-checks at the call sites.
COMPARE_OPTS: dict[str, Any] = {
    "compare_type": True,
    "compare_server_default": True,
    "render_item": render_item,
}


def run_migrations_offline() -> None:
    context.configure(
        url=str(settings.database_url),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        **COMPARE_OPTS,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    supplied = config.attributes.get("connection")
    if supplied is not None:
        context.configure(connection=supplied, target_metadata=target_metadata, **COMPARE_OPTS)
        with context.begin_transaction():
            context.run_migrations()
        return

    engine = sa.create_engine(str(settings.database_url), poolclass=pool.NullPool)
    with engine.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata, **COMPARE_OPTS)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
