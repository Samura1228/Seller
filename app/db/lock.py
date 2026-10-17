"""Single-runner lock. Railway briefly runs the new deployment next to the old one; two processes with the same
Telegram session would get it revoked (AuthKeyDuplicated). A Postgres session-level advisory lock is held for
the process lifetime and released automatically when the old process exits."""

import asyncio
import logging

import asyncpg

log = logging.getLogger(__name__)
LOCK_KEY = 7_311_822_019  # arbitrary, constant for this app


def _plain_dsn(url: str) -> str:
    return url.replace("postgresql+asyncpg://", "postgresql://", 1)


async def acquire_runner_lock(database_url: str, poll_seconds: float = 3.0) -> asyncpg.Connection:
    conn = await asyncpg.connect(_plain_dsn(database_url))
    waited = False
    while not await conn.fetchval("SELECT pg_try_advisory_lock($1)", LOCK_KEY):
        if not waited:
            log.info("Another instance is running; waiting for it to stop before starting Telegram clients")
            waited = True
        await asyncio.sleep(poll_seconds)
    log.info("Runner lock acquired")
    return conn
