"""Run the ingestion jobs: ``python -m meridian.ingest``."""

import datetime as dt
import logging
import signal
import threading
from types import FrameType

from apscheduler.schedulers.background import BackgroundScheduler
from meridian_config import load_app

from meridian.db.session import create_engine, session_factory
from meridian.ingest.fetch import HttpFetcher
from meridian.ingest.network import Network
from meridian.ingest.pacing import Pacer
from meridian.ingest.robots import RobotsCache
from meridian.ingest.scheduler import AcquireScheduler, DiscoveryScheduler


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    settings = load_app()
    engine = create_engine(settings)
    sessions = session_factory(engine)
    fetcher = HttpFetcher()
    # One pacer and one robots cache for the process: the per-host promise they keep is only
    # kept if every job that talks to publishers goes through the same instance.
    pacer = Pacer()
    network = Network(fetcher=fetcher, robots=RobotsCache(fetcher, pacer), pacer=pacer)

    # One scheduler, two jobs. Separate schedulers would mean two thread pools in a process
    # whose whole workload is one HTTP call at a time.
    scheduler = BackgroundScheduler()
    discovery = DiscoveryScheduler(sessions, network, scheduler=scheduler)
    acquire = AcquireScheduler(
        sessions,
        network,
        lease=dt.timedelta(seconds=settings.work_lease_seconds),
        scheduler=scheduler,
    )
    discovery.start()
    acquire.start()

    stop = threading.Event()

    def _stop(signum: int, frame: FrameType | None) -> None:
        stop.set()

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    stop.wait()

    scheduler.shutdown()
    fetcher.close()
    engine.dispose()


if __name__ == "__main__":
    main()
