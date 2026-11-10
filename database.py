from pathlib import Path

from sqlalchemy import create_engine, event
from sqlalchemy.orm import declarative_base, sessionmaker


# Keep the existing database location. Do not move or recreate blueshield.db.
BASE_DIR = Path(__file__).resolve().parent
DATABASE_FILE = BASE_DIR / "blueshield.db"
DATABASE_URL = f"sqlite:///{DATABASE_FILE.as_posix()}"

SQLITE_BUSY_TIMEOUT_MS = 30000

engine = create_engine(
    DATABASE_URL,
    connect_args={
        "check_same_thread": False,
        "timeout": SQLITE_BUSY_TIMEOUT_MS / 1000,
    },
    pool_pre_ping=True,
)


@event.listens_for(engine, "connect")
def _configure_sqlite_connection(dbapi_connection, connection_record):
    """Give SQLite time to wait for competing writers and use safe WAL settings."""
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute(f"PRAGMA busy_timeout={SQLITE_BUSY_TIMEOUT_MS}")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA wal_autocheckpoint=1000")
    finally:
        cursor.close()


# Enable WAL once when the engine is initialized. WAL allows readers to continue
# while a writer commits; SQLite still serializes writers.
with engine.connect() as _connection:
    _connection.exec_driver_sql("PRAGMA journal_mode=WAL")


SessionLocal = sessionmaker(
    bind=engine,
    autoflush=False,
    autocommit=False,
    expire_on_commit=False,
)

Base = declarative_base()
