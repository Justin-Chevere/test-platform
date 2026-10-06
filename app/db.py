from collections.abc import Iterator
from typing import Annotated, Any

from fastapi import Depends
from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import get_settings


def make_engine(url: str, **kwargs: Any) -> Engine:
    if not url.startswith("sqlite"):
        return create_engine(url, **kwargs)

    # check_same_thread: FastAPI runs sync endpoints in a thread pool.
    # timeout: the API and every worker are separate processes sharing one file. A write
    # that finds the file locked waits up to 30 s for its turn instead of failing at once.
    engine = create_engine(url, connect_args={"check_same_thread": False, "timeout": 30}, **kwargs)

    @event.listens_for(engine, "connect")
    def _configure(dbapi_connection: Any, _record: Any) -> None:
        # WAL lets readers work while a write is in progress, so the API keeps answering
        # while a worker saves results. SQLite leaves foreign keys unchecked by default.
        dbapi_connection.execute("PRAGMA journal_mode=WAL")
        dbapi_connection.execute("PRAGMA foreign_keys=ON")

    return engine


engine = make_engine(get_settings().database_url)
SessionLocal = sessionmaker(bind=engine, autoflush=False)


class Base(DeclarativeBase):
    pass


def get_db() -> Iterator[Session]:
    """One session per request, always closed afterwards."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


DbSession = Annotated[Session, Depends(get_db)]
