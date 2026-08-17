"""Alembic environment.

The database URL comes from ``MERIDIAN_DATABASE_URL``, never from ``alembic.ini`` — one
source of truth for which database a process talks to.
"""

from logging.config import fileConfig

import sqlalchemy as sa
from alembic import context
from meridian_config import load as load_settings
from meridian_dbkit import StrEnumType
from sqlalchemy import pool

from meridian.db.models import Base

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

settings = load_settings()

target_metadata = Base.metadata


def render_item(type_: str, obj: object, autogen_context: object) -> str | bool:
    """Render application column types as their plain SQL equivalent.

    A migration that imports application code stops working the day that code moves.
    ``StrEnumType`` is a ``varchar`` on disk; the CHECK that constrains it is a separate
    constraint the migration already carries.
    """
    if type_ == "type" and isinstance(obj, StrEnumType):
        return f"sa.String(length={obj.impl_instance.length})"
    return False


#: ``compare_server_default`` catches a default drifting between the models and the
#: database, which a column-type comparison alone misses.
COMPARE_OPTS = {
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
    # A caller (the test suite) may supply its own connection so migrations run against a
    # database of its choosing rather than MERIDIAN_DATABASE_URL.
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
