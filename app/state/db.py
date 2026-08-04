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


def init_db(engine=None, *, drop_first: bool = True) -> None:
    """Create the tables. DROPS THEM FIRST unless told not to.

    THE DOCSTRING USED TO SAY "drops first when running against an in-memory
    test database". It did not: it dropped always, against whatever engine it
    was handed. Nothing checked, and 480 test call sites depend on the drop for
    isolation -- so the default stays, and this sentence is the correction.

    THAT DEFAULT IS SHARP, and in this product it is sharper than usual. The
    audit chain is append-only and hash-linked precisely so that no row can be
    removed without the removal being obvious; drop_all removes every row and
    leaves nothing behind to notice, which is the one outcome the whole design
    exists to prevent. A single init_db() in a production path destroys the
    evidence and looks like a clean install.

    So a caller that means "make sure the tables exist" must say
    drop_first=False and get exactly that. deploy/entrypoint.sh does, and only
    when there is no database file at all.
    """
    target = engine or _engine
    if drop_first:
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
