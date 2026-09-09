"""
Database engine/session setup.

Defaults to a local SQLite file (data.db) so the project runs with zero
external setup. Set DATABASE_URL to point at PostgreSQL in production
(e.g. postgresql://user:pass@host:5432/dbname) -- the ORM models in
models.py work against both engines unmodified, with one exception:
`EmployeeFaceGallery.embedding` is stored as JSON text on SQLite and should
be migrated to a native `vector(512)` column (pgvector extension) on
PostgreSQL for indexed nearest-neighbour search at scale, per Section 7.2
of the project documentation.
"""
import os
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, declarative_base

DATABASE_URL = os.environ.get("DATABASE_URL", "sqlite:///./data.db")

connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}
engine = create_engine(DATABASE_URL, connect_args=connect_args)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


def reset_engine():
    """Rebinds engine/SessionLocal to whatever DATABASE_URL currently is.

    Exists specifically for the test suite: earlier, tests forced a fresh
    DATABASE_URL to take effect by deleting every `app.*` module from
    sys.modules and re-importing them per test. That also reloaded heavy
    singletons (the YOLO model, and InsightFace once installed) on every
    single test, which exhausted memory in a resource-constrained
    environment. Since get_db() and init_db() both look up `engine` /
    `SessionLocal` as module globals at call time (not at import time),
    reassigning them here is sufficient -- no re-import needed, and vision
    model singletons in other modules are left untouched."""
    global engine, SessionLocal, DATABASE_URL
    DATABASE_URL = os.environ.get("DATABASE_URL", "sqlite:///./data.db")
    connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}
    engine = create_engine(DATABASE_URL, connect_args=connect_args)
    SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db():
    """Create all tables if they don't already exist. Called on app startup."""
    from sqlalchemy import event

    if DATABASE_URL.startswith("sqlite"):
        @event.listens_for(engine, "connect")
        def _set_sqlite_pragma(dbapi_connection, connection_record):
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

    import app.models  # noqa: F401 -- ensures models are registered on Base
    Base.metadata.create_all(bind=engine)
