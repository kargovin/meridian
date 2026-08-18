"""Development and test provisioning for the Platform's database.

Creates the role and the database, and revokes the default ``PUBLIC`` grant that would
otherwise let any role connect to the application's database. Idempotent.

Needs an administrative connection — the Platform's own role cannot create roles, which is
the point of it having one. Production roles and databases come from the cluster.

``python -m meridian_platform.bootstrap`` provisions the development database.
"""

import re
import sys

import sqlalchemy as sa

DEFAULT_ROLE = "platform"
DEFAULT_CONNECTION_LIMIT = 20

# Identifiers and passwords are interpolated: PostgreSQL does not accept bind parameters in
# CREATE ROLE or CREATE DATABASE, so anything reaching those statements must be checked here.
_SAFE = re.compile(r"\A[A-Za-z_][A-Za-z0-9_]*\Z")


def _check(name: str) -> str:
    if not _SAFE.match(name):
        raise ValueError(f"unsafe identifier: {name!r}")
    return name


def ensure_platform_database(
    admin_url: str | sa.engine.URL,
    database: str,
    role: str = DEFAULT_ROLE,
    password: str = DEFAULT_ROLE,
    protect_database: str | None = None,
    connection_limit: int = DEFAULT_CONNECTION_LIMIT,
) -> None:
    """Create ``role`` and ``database``, and close ``protect_database`` to everyone else.

    ``CREATE DATABASE`` cannot run inside a transaction, hence AUTOCOMMIT.
    """
    _check(role)
    _check(database)
    _check(password)

    engine = sa.create_engine(
        sa.engine.make_url(admin_url).set(database="postgres"), isolation_level="AUTOCOMMIT"
    )
    try:
        with engine.connect() as conn:
            role_exists = conn.execute(
                sa.text("SELECT 1 FROM pg_roles WHERE rolname = :name"), {"name": role}
            ).scalar()
            if not role_exists:
                conn.execute(
                    sa.text(
                        f"CREATE ROLE \"{role}\" LOGIN PASSWORD '{password}'"
                        f" CONNECTION LIMIT {int(connection_limit)}"
                    )
                )

            database_exists = conn.execute(
                sa.text("SELECT 1 FROM pg_database WHERE datname = :name"), {"name": database}
            ).scalar()
            if not database_exists:
                conn.execute(sa.text(f'CREATE DATABASE "{database}" OWNER "{role}"'))

            if protect_database is not None:
                # PostgreSQL grants CONNECT to PUBLIC on every database by default, so
                # revoking from one role achieves nothing. The owner keeps its own access.
                conn.execute(
                    sa.text(f'REVOKE CONNECT ON DATABASE "{_check(protect_database)}" FROM PUBLIC')
                )
    finally:
        engine.dispose()


def main() -> int:
    from meridian_config import load_app, load_platform

    app_url = sa.engine.make_url(str(load_app().database_url))
    platform_url = sa.engine.make_url(str(load_platform().database_url))
    if platform_url.database is None:
        raise ValueError("MERIDIAN_PLATFORM_DATABASE_URL names no database")

    ensure_platform_database(
        admin_url=app_url,
        database=platform_url.database,
        role=platform_url.username or DEFAULT_ROLE,
        password=platform_url.password or DEFAULT_ROLE,
        protect_database=app_url.database,
    )
    print(f"provisioned {platform_url.render_as_string(hide_password=True)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
