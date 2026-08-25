"""Run the discovery heartbeat: ``python -m meridian.ingest``."""

import logging
import signal
import threading
from types import FrameType

from meridian_config import load_app

from meridian.db.session import create_engine, session_factory
from meridian.ingest.fetch import HttpFetcher
from meridian.ingest.scheduler import DiscoveryScheduler


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    engine = create_engine(load_app())
    fetcher = HttpFetcher()
    scheduler = DiscoveryScheduler(session_factory(engine), fetcher)
    scheduler.start()

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
