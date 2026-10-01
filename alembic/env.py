"""Alembic migration environment.

Reads the DATABASE_URL from the app's config (which loads .env), then runs
migrations against that database. Supports both sync and async drivers —
alembic itself doesn't run async, so we coerce the URL to the sync driver
(psycopg2) for migrations even though the app uses asyncpg at runtime.
"""

from __future__ import annotations

import sys
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy import create_engine, pool

# Make app/ importable.
REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from app.config import get_settings  # noqa: E402
from app.models import Base  # noqa: E402  — registers all models
from app.models import auction, watchlist, purchase, audit_log, settings  # noqa: F401,E402

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Use the app's DATABASE_URL — but coerce to the sync psycopg2 driver because
# alembic doesn't run async migrations. The runtime app keeps using asyncpg.
#
# Real-world hosts (Neon, Supabase) hand you `postgresql://...?sslmode=require`
# or `postgresql+asyncpg://...?sslmode=require`. We need
# `postgresql+psycopg2://...?sslmode=require` here. psycopg2 understands
# sslmode natively so we leave it in.
runtime_url = get_settings().database_url
if "+asyncpg" in runtime_url:
    sync_url = runtime_url.replace("postgresql+asyncpg://", "postgresql+psycopg2://", 1)
elif runtime_url.startswith("postgresql+psycopg"):
    sync_url = runtime_url
elif runtime_url.startswith("postgresql://") or runtime_url.startswith("postgres://"):
    # Force psycopg2 explicitly so SQLAlchemy doesn't try the default driver
    # which might be asyncpg in some environments.
    sync_url = runtime_url.replace("postgres://", "postgresql://", 1).replace(
        "postgresql://", "postgresql+psycopg2://", 1
    )
else:
    sync_url = runtime_url


def _redact(url: str) -> str:
    """Mask the password in a DB URL for safe logging."""
    import re as _re

    return _re.sub(r"://([^:]+):([^@]+)@", r"://\1:***@", url)


# Validate the URL up front. SQLAlchemy's parse error is intentionally vague
# (it strips the URL out of the exception to avoid leaking creds), which makes
# debugging impossible. So we parse here, catch the failure, and re-raise with
# our own redacted URL embedded in the message — the traceback DOES survive
# stdout/logging redirection, so this is the most reliable diagnostic channel.
import sys as _sys

_sys.stderr.write(
    f"[alembic env.py] runtime_url length={len(runtime_url)} "
    f"starts_with={runtime_url[:30]!r} "
    f"ends_with={runtime_url[-30:]!r} "
    f"count_scheme={runtime_url.count('://')} count_at={runtime_url.count('@')} "
    f"has_newline={chr(10) in runtime_url or chr(13) in runtime_url} "
    f"has_space={' ' in runtime_url}\n"
)
_sys.stderr.write(f"[alembic env.py] sync_url_redacted={_redact(sync_url)}\n")
_sys.stderr.flush()

try:
    from sqlalchemy.engine.url import make_url as _make_url

    _make_url(sync_url)
except Exception as _e:
    raise RuntimeError(
        f"DATABASE_URL parse failed: "
        f"runtime_url_length={len(runtime_url)}, "
        f"runtime_url_starts={runtime_url[:30]!r}, "
        f"runtime_url_ends={runtime_url[-30:]!r}, "
        f"sync_url_redacted={_redact(sync_url)}, "
        f"underlying_error={type(_e).__name__}: {_e}"
    ) from _e

# NOTE: We intentionally do NOT call config.set_main_option("sqlalchemy.url", sync_url)
# because alembic.ini is parsed by ConfigParser, which interprets `%` chars in the
# value as interpolation markers — Neon/Supabase passwords frequently contain
# URL-encoded bytes (%XX) and that triggers ConfigParser InterpolationSyntaxError
# or silently mangles the URL. We instead pass sync_url directly to create_engine
# below, bypassing the ini layer entirely.

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """Run migrations without a DB connection — emits SQL to stdout."""
    context.configure(
        url=sync_url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations with a live DB connection."""
    connectable = create_engine(sync_url, poolclass=pool.NullPool)
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
