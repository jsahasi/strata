"""Engine and session handling. SQLite by default; the URL is the seam to Postgres."""

import os
from contextlib import contextmanager

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.state.models import Base

DATABASE_URL = os.environ.get("STRATA_DATABASE_URL", "sqlite:///strata.db")

_engine = create_engine(DATABASE_URL, future=True)
_SessionFactory = sessionmaker(bind=_engine, future=True, expire_on_commit=False)


def get_engine():
    return _engine


def init_db(engine=None) -> None:
    """Create tables. Drops first when running against an in-memory test database."""
    target = engine or _engine
    Base.metadata.drop_all(target)
    Base.metadata.create_all(target)


@contextmanager
def session_scope() -> Session:
    session = _SessionFactory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
