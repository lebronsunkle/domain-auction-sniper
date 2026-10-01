"""Async SQLAlchemy engine + session factory."""

from __future__ import annotations

import re
from typing import AsyncIterator
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.config import get_settings


def _normalize_async_url(raw: str) -> tuple[str, dict]:
    """Coerce a runtime DATABASE_URL into an asyncpg-friendly form.

    Real-world DB hosts (Neon, Supabase, etc.) hand you URLs with
    `?sslmode=require` and the `postgresql://` scheme. asyncpg uses a
    different scheme (`postgresql+asyncpg://`) and doesn't recognize the
    `sslmode` query param -- it wants `ssl=true` as a connect kwarg.
    This function:
      1. forces the +asyncpg scheme
      2. strips psycopg-specific query params (sslmode, channel_binding, etc.)
      3. returns those psycopg params as a `connect_args` dict so the
         engine can pass `ssl=True` if SSL was requested
    """
    parsed = urlparse(raw)
    scheme = parsed.scheme

    # Force the asyncpg driver scheme.
    if scheme == "postgresql" or scheme == "postgres":
        scheme = "postgresql+asyncpg"
    elif scheme.startswith("postgresql+psycopg"):
        scheme = "postgresql+asyncpg"

    # Strip psycopg-only query params; remember if SSL was requested.
    query = parse_qs(parsed.query)
    sslmode = query.pop("sslmode", [None])[0]
    query.pop("channel_binding", None)  # Neon adds this; asyncpg can't read it
    new_query = urlencode(query, doseq=True)

    new_url = urlunparse(parsed._replace(scheme=scheme, query=new_query))

    connect_args: dict = {}
    if sslmode in {"require", "verify-ca", "verify-full"}:
        connect_args["ssl"] = True
    elif sslmode == "disable":
        connect_args["ssl"] = False

    # 2026-08-26 (finacredit post-mortem): pool_pre_ping only validates a
    # connection at CHECKOUT. A Neon connection that half-dies MID-QUERY
    # left the awaiting coroutine hung forever — the trigger worker froze
    # silently for hours and a $51 snipe never fired. These caps bound
    # every DB await: no query runs >60s, no connect attempt >15s.
    connect_args["command_timeout"] = 60.0
    connect_args["timeout"] = 15.0

    return new_url, connect_args


_settings = get_settings()
_url, _connect_args = _normalize_async_url(_settings.database_url)

engine = create_async_engine(
    _url,
    echo=False,
    pool_pre_ping=True,
    connect_args=_connect_args,
)
SessionLocal = async_sessionmaker(engine, expire_on_commit=False)


async def get_db() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency."""
    async with SessionLocal() as session:
        yield session
