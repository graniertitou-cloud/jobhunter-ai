"""
Safe migration runner.

Handles three cases:
1. Fresh database: runs `alembic upgrade head` to create all tables.
2. Legacy database (tables already exist from Base.metadata.create_all, but no
   alembic_version table): stamps the database with the baseline revision so
   Alembic treats it as already up to date, then applies any newer migrations.
3. Database already managed by Alembic: runs `alembic upgrade head` normally.

Run with: python migrate.py
"""
import logging
import os
import sys

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect

logging.basicConfig(level=logging.INFO, format="[migrate] %(message)s")
logger = logging.getLogger(__name__)

BASELINE_REVISION = "9e94c247962a"


def main() -> None:
    database_url = os.getenv("DATABASE_URL", "sqlite:///jobs.db")
    if database_url.startswith("postgres://"):
        database_url = database_url.replace("postgres://", "postgresql://", 1)

    engine = create_engine(database_url)
    inspector = inspect(engine)
    tables = set(inspector.get_table_names())

    alembic_cfg = Config("alembic.ini")
    alembic_cfg.set_main_option("sqlalchemy.url", database_url)

    has_alembic = "alembic_version" in tables
    has_app_tables = any(t in tables for t in ("users", "profiles", "saved_jobs"))

    if not has_alembic and has_app_tables:
        # Legacy database: assume it matches the baseline and stamp it.
        logger.info("Legacy database detected, stamping baseline revision %s", BASELINE_REVISION)
        command.stamp(alembic_cfg, BASELINE_REVISION)

    logger.info("Running 'alembic upgrade head'")
    command.upgrade(alembic_cfg, "head")
    logger.info("Migrations complete")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        logger.error("Migration failed: %s", exc)
        sys.exit(1)
