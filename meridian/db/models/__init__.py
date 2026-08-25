"""The canonical entities (RFC §5.1).

Importing this package registers every table on ``Base.metadata``; Alembic's ``env.py``
depends on that, so a new model must be re-exported here or autogenerate will propose
dropping it.
"""

from meridian.db.base import Base
from meridian.db.models.article import AlternateCopy, CanonicalRecord
from meridian.db.models.clustering import Cluster, ClusterMember, Summary
from meridian.db.models.config import RuntimeConfig
from meridian.db.models.enrichment import Classification
from meridian.db.models.polling import FeedPollState
from meridian.db.models.source import Feed, Source
from meridian.db.models.takedown import Takedown
from meridian.db.models.work import PipelineWork

__all__ = [
    "AlternateCopy",
    "Base",
    "CanonicalRecord",
    "Classification",
    "Cluster",
    "ClusterMember",
    "Feed",
    "FeedPollState",
    "PipelineWork",
    "RuntimeConfig",
    "Source",
    "Summary",
    "Takedown",
]
