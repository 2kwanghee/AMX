"""Engine and session wiring."""

from __future__ import annotations

from collections.abc import Iterator
from functools import lru_cache

from sqlalchemy import Engine, create_engine, func, select
from sqlalchemy.orm import Session, sessionmaker

from app.config import get_settings


def try_advisory_xact_lock(db: Session, key: int) -> bool:
    """Try to take a transaction-scoped advisory lock; True iff acquired.

    Transaction-scoped (``pg_try_advisory_xact_lock``): the lock auto-releases on
    commit or rollback, so there is no explicit unlock and no risk of a leaked
    lock on a pooled connection. Shared by the gRPC sweeper loop and the billing
    sweep, which pass distinct keys so their sweeps run independently.
    """
    return bool(db.scalar(select(func.pg_try_advisory_xact_lock(key))))


@lru_cache(maxsize=1)
def get_engine() -> Engine:
    return create_engine(get_settings().database_url, pool_pre_ping=True, future=True)


@lru_cache(maxsize=1)
def get_sessionmaker() -> sessionmaker[Session]:
    return sessionmaker(bind=get_engine(), autoflush=False, expire_on_commit=False)


def get_session() -> Iterator[Session]:
    with get_sessionmaker()() as session:
        yield session
