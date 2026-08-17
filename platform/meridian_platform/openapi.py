"""Generate the published OpenAPI document.

``python -m meridian_platform.openapi`` rewrites it. A test regenerates and compares, so the
committed file cannot drift from the models.
"""

import json
from pathlib import Path

from meridian_config import PlatformSettings

DOCUMENT = Path(__file__).resolve().parent.parent / "openapi.json"


def render() -> str:
    """The document as it is written to disk. Keys are sorted so the bytes are stable."""
    from meridian_platform.main import create_app

    # The schema depends on routes and models, never on configuration.
    settings = PlatformSettings(  # type: ignore[call-arg]
        database_url="postgresql+psycopg://unused:unused@localhost/unused",  # type: ignore[arg-type]
        _env_file=None,
    )
    return json.dumps(create_app(settings).openapi(), indent=2, sort_keys=True) + "\n"


def write() -> Path:
    DOCUMENT.write_text(render())
    return DOCUMENT


if __name__ == "__main__":
    print(f"wrote {write()}")
