"""Everything a job needs to reach a publisher, as one object every such job shares.

The three parts keep one promise between them — FR-I3's spacing per host — and keep it only if
every request goes through the same instances. Two pacers each honour ``rate_limit_per_min``
alone and break it together; two robots caches fetch ``robots.txt`` twice. Discovery and
acquire are two jobs on one scheduler's thread pool and can reach one host in the same second,
which is why this is built once in ``__main__`` and handed to both rather than built by each.
"""

from dataclasses import dataclass

from meridian.ingest.fetch import Fetcher
from meridian.ingest.pacing import Pacer
from meridian.ingest.robots import RobotsCache


@dataclass(frozen=True)
class Network:
    fetcher: Fetcher
    #: Fetches through ``fetcher`` and ``pacer``, so a ``robots.txt`` request counts against
    #: the publisher's budget like any other.
    robots: RobotsCache
    pacer: Pacer
