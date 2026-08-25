"""The runtime config plane (RFC §9).

Knobs that must change without a deploy. Stored as text against a declared key rather than as
typed columns: the value is what changes at runtime, the set of keys changes only when a story
adds one, and a key/value row keeps those two rates of change apart.

Not to be confused with ``meridian_config``, which is process bootstrap — a database URL cannot
live here, because reading this table requires one.
"""

import datetime as dt

import sqlalchemy as sa
from meridian_dbkit import TZDateTime
from sqlalchemy.orm import Mapped, mapped_column

from meridian.db.base import Base


class RuntimeConfig(Base):
    """One knob. See ``meridian.db.runtime_config`` for the declared keys and their bounds.

    Rows are seeded by the migration that declares a knob, so a key always has a row and every
    write is an update against a version token. A missing row is still readable — the reader
    falls back to the declared default — but it cannot be edited through the admin surface.
    """

    __tablename__ = "runtime_config"

    key: Mapped[str] = mapped_column(sa.Text, primary_key=True)

    #: Text for every knob regardless of its declared type. Parsing and bounds are the reader's,
    #: which is what lets a knob change type without a migration and keeps the validation in one
    #: place rather than split between a CHECK and the code.
    value: Mapped[str] = mapped_column(sa.Text)

    #: The version token for compare-and-set, trigger-maintained like the registry's, so a psql
    #: write moves it too.
    updated_at: Mapped[dt.datetime] = mapped_column(TZDateTime, server_default=sa.func.now())
