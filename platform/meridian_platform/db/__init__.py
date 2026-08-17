"""The Platform's persistence.

Importing this package registers every table on ``Base.metadata``; the Alembic tree under
``platform/migrations`` reads that, so a new model must be re-exported here or autogenerate
proposes dropping it.
"""

from meridian_platform.db.base import Base
from meridian_platform.db.models import SummarizeJob, SummarizeJobItem
from meridian_platform.db.session import create_engine, session_factory

__all__ = [
    "Base",
    "SummarizeJob",
    "SummarizeJobItem",
    "create_engine",
    "session_factory",
]
